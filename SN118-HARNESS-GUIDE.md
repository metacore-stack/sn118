# SN118 / DittoBench — Agent-Memory Harness Development Guide

> **Verified 2026-09-02** against `github.com/ditto-assistant/ditto-subnet` (`docs/MINER.md`,
> `miners/dittobench-starter-kit/PROTOCOL.md`, `miners/dittobench-starter-kit/README.md`,
> `research/dittobench-datagen/docs/bench-versions.md`), the live bench config
> (`platform-api.heyditto.ai/api/v1/public/bench/config`), the public leaderboard API,
> and four independent chain explorers.
>
> **Everything version-sensitive moves daily.** Re-read the bench config before acting on any
> number here. Quotations from the repo are marked with `[repo]`; everything else marked
> **Recommendation** is engineering judgement, not official guidance.

---

## Table of contents

- [0. Five facts that decide your architecture](#0-five-facts-that-decide-your-architecture)
- [1. The contract](#1-the-contract)
- [2. How you are scored](#2-how-you-are-scored)
- [3. Choosing your starting point](#3-choosing-your-starting-point)
- [4. Target architecture](#4-target-architecture)
- [5. Build sequence](#5-build-sequence)
- [6. What DittoBench v12 punishes](#6-what-dittobench-v12-punishes)
- [7. Testing](#7-testing)
- [8. Environment setup](#8-environment-setup)
- [9. Packaging and submission](#9-packaging-and-submission)
- [10. Pitfalls](#10-pitfalls)
- [11. The cheating boundary](#11-the-cheating-boundary)
- [12. Economics and go/no-go](#12-economics-and-gono-go)
- [Appendix A — command reference](#appendix-a--command-reference)
- [Appendix B — metrics dashboard](#appendix-b--metrics-dashboard)
- [Appendix C — the documentation drift problem](#appendix-c--the-documentation-drift-problem)
- [Appendix D — sources](#appendix-d--sources)

---

## 0. Five facts that decide your architecture

Read these before designing anything. Each one invalidates a design that looks reasonable
without it.

### 0.1 Your container has no internet

The live bench config states the enforcement plainly:

> `"enforcement": "ticket-scoped platform proxy forces the model and medium reasoning effort
> and holds the upstream key outside the sandbox; sandbox egress is deny-all"` `[repo/api]`

Exactly two holes exist in that boundary:

| Hole | What it is |
|---|---|
| Platform model relay | Serves the locked `openai/gpt-oss-20b`. Your `DITTOBENCH_MODEL` is overridden. |
| `tool_endpoint` | A validator-served mock tool executor, supplied per case in the `RunRequest`. |

**Consequences.** No model pull at runtime. No package fetch. No calling your own API. No
downloading an index. Everything the harness needs must be baked into an image built from a
**20 MiB** context. No API key of yours is ever used during scoring, so shipping one buys
nothing and leaks it into an uploaded tarball.

### 0.2 The trajectory that is graded is the one the validator observed

> "A harness that ignores `tool_endpoint` scores 0 on the on-chain scored path." `[repo]`

Self-reported `tool_calls` are not evidence. The validator serves the mock endpoint, records
every call, and grades that record. It also self-checks the endpoint before scoring, so "the
listener was down" is not a failure mode you inherit — if the listener is healthy and you never
call it, you simply get the zero.

### 0.3 Grading is deterministic and judge-free

> `"judge_free": true` … `"deterministic per-answer_kind checks with distractor and
> forbidden-value zeroing; a score is a pure function of (dataset, transcript)"` `[repo/api]`

Memory answers are matched by **normalized bounded containment** against the expected value —
with an exact number-token path for numeric answers — checked first in your `answer` field and
only then in `final_text`. There is no LLM judge that will award partial credit for being nearly
right, and *distractor and forbidden-value zeroing* means emitting a planted wrong value can zero
a case you would otherwise have passed.

### 0.4 Integrity is multiplicative; accuracy is additive

Accuracy is a mean. Integrity failures are multipliers applied to it. One of them is not
symmetric with the others: leaking another user's planted nonce is a hard **×0.50** that no
amount of good recall recovers. See [§2.3](#23-the-three-integrity-multipliers).

### 0.5 The board reset because an exploit was closed, not because the problem is unsolved

The v12 spec is explicit about its own motivation:

> "A harness that scored **0.997 on v11** never read the randomized prose; it parsed the
> fixed-order KV rows, fired a model call only to satisfy the attribution gate, and computed a
> balance positionally." `[repo]`

The current champion sits at ~0.78 on v12. Read that as *"the shortcut was removed six weeks
ago"*, not *"nobody can do this"*. It also tells you exactly what earns points now: read prose,
bind relationally, pick the program from the request, compute deterministically.

---

## 1. The contract

SN118 is a **best-artifact competition**, not a live-inference subnet.

> "Miners improve an agent-memory harness, practice locally, and submit its complete Docker
> build context for independent validators to score. … You are rewarded for improving the best
> artifact, not for serving live inference." `[repo]`

You upload once. Validators build it, run it, score it, discard it. Nothing of yours stays
online, and you do not need to keep a server running.

### 1.1 What you ship

A gzip tarball of a complete Docker build context.

| Requirement | Detail |
|---|---|
| Format | gzip-compressed tarball, **≤ 20 MiB** |
| `Dockerfile` | at the **tarball root** — it is the validator's build context |
| Paths | safe relative paths only; no links, no special files |
| Build | `docker build` must succeed with **no credentials** — screeners supply no GitHub token, no registry login, no build secret |
| Dependencies | every dependency must be **public or vendored** |
| Runtime | serves the protocol on **`:8080`** |
| Contents | no `.env`, no wallet key, no API key, no answer fixtures |

Language is free — Rust, Python, TypeScript, Go, anything. **Docker is not optional.**

### 1.2 The three endpoints

```
GET  /health   →  200 {"status":"ok"}
POST /seed     →  200 {"pairs":N,"subjects":N,"links":N}
POST /run      →  200 RunResponse
```

**`RunRequest`** carries `case_id`, `bench_version`, `system_prompt`, `user_input`, `tools[]`,
and — on scored tool cases — `tool_endpoint` and `user_id`.

**`RunResponse`:**

```json
{
  "final_text": "Here's what I found...",
  "tool_calls": [ { "name": "search_web", "args": { "query": "..." }, "hop": 0 } ],
  "prompt_tokens": 1234,
  "output_tokens": 56,
  "latency_ms": 812,
  "answer": "quantum error correction",
  "abstain": false
}
```

**Tool execution round-trip.** POST a `ToolExecRequest` to `tool_endpoint`:

```json
{ "case_id": "...", "user_id": "colleague", "name": "search_web",
  "args": { "query": "veltrix index" }, "hop": 0 }
```

and feed the returned `{"result": "..."}` back to the model. `hop` is the **0-based order of the
call within the case**.

> **Memory tools are not served by that endpoint.** It replies with an empty `result` and an
> error like `{"error": "tool not available via this endpoint: search_memories"}`.
> "treat that like a real tool error." `[repo]` — this is expected behaviour, not a bug to work
> around.

On result-usage cases the validator additionally grades whether your final answer incorporates
the value the executed tool returned (`CaseScore.result_usage`, 0–1).

### 1.3 Timeouts

| Call | Ceiling | Failure |
|---|---|---|
| `GET /health` | **10 s** | container start → healthy. Miss it and screening fails outright. |
| `POST /run` | **60 s** | per case; a miss scores **0** for that case |
| `POST /seed` | **5 min** | per wave |

Latency is *reported* (`median_ms`) but never ranked. Speed is bounded, not scored.

### 1.4 The three seeding tiers

You must survive all three.

| Tier | Shape | What it tests |
|---|---|---|
| **A — Prepared** | `pairs`, `subjects`, `links` all supplied | retrieval in isolation |
| **B — Raw pairs** | `subjects: []`, `links: []` | you must **build your own subject index** from raw conversation pairs |
| **C — Staged** | `/seed` called repeatedly with incrementing `wave`, interleaved with `/run` | idempotent ordered upsert; questions may target any wave already seeded |

> "A harness that relies on prepared subjects scores materially lower here." `[repo]`
>
> "Extending it to construct subjects when none are provided (Tier B) is **the highest-value
> change you can make**." `[repo]`

### 1.5 Opaque identifiers — a real trap

> "V9 identifiers are opaque capabilities. Persist UUID-shaped `case_id`, `user_id`, `pair_id`,
> `session_id`, and `subject_id` values exactly and compare them only for equality; **never
> derive family, order, or grading behavior from their spelling.**" `[repo]`

v12 replaced readable session identifiers like `v12-conversation-3` with opaque hashes
specifically to punish harnesses that parsed them. Echo supplied `pair_id`, `pairIds` and
`subject_id` values back **unchanged** in tool calls.

What the request *does* retain product semantics for: prompts, memory text, timestamps, subject
descriptions, tool schemas, tool results. What is deliberately absent: benchmark seed, run size,
digest, question/family/category labels, expected answers, grader state, ontology.

### 1.6 `answer` and `abstain` are worth more than they look

- **`answer`** — the bare value your prose asserts (a name, a number, a comma-separated list).
  The grader matches this slot first and only falls back to prose containment. Populating it
  "removes prose-phrasing risk from grading." `[repo]` **Always populate it.**
- **`abstain`** — the primary decline signal. But: *"Abstaining on an answerable case scores 0"*
  `[repo]`. The loss is symmetric with answering wrongly, so gate it on retrieval genuinely
  coming up empty — or finding only someone **else's** value — not on model hedging.

---

## 2. How you are scored

### 2.1 The accuracy core

```
composite = 0.5 × tool_mean + 0.5 × memory_mean
```

Half the score is memory, half is tools. Memory is the harder half, and within it *retrieval
recall is the main bottleneck.* `[repo]`

### 2.2 Tool grading — on-chain differs from local

**On-chain:**

```
0.4 × tool-name F1  +  0.4 × argument F1  +  0.2 × trajectory / order credit
```

**Local scorer (the starter kit):** name-centric —
`matched = Σ min(expected_count, observed_count)`, `base = matched / total_expected`,
`−0.1` per unexpected extra call, clamped to `[0,1]`; no-expected-tool cases score `1.0` iff
nothing was called.

> "Arguments weigh as much as selection on-chain. … Only the *local* scorer is name-centric."
> `[repo]`

**This means a harness tuned against local practice systematically overestimates its tool
score.** Budget for the gap.

### 2.3 The three integrity multipliers

The composite is multiplied by up to three bounded factors. Each is `1.0` when its trigger is
absent, so accuracy stays dominant — but they are not equally forgiving.

| Factor | Range | Trigger |
|---|---|---|
| **Tool efficiency** | `[0.85, 1.0]` | observed-execution runs; the first extra call is free, then the over-call penalty saturates |
| **Metamorphic consistency** | `[0.85, 1.0]` | `1 − 0.15 × (fraction of invariance families answered inconsistently)` |
| **Canary integrity** | `1.0` / `0.85` / **`0.50`** | see below |

**The canary factor is the asymmetric one.** A per-run seed-derived nonce is planted in the
conversation and one memory case asks for it.

- Honest recall miss (nonce neither surfaced nor leaked) → bounded **×0.85**
- Surfacing the **planted decoy** nonce (a cross-user leak) → hard **×0.50** disqualifier
  *"that easy recall elsewhere cannot buy back."* `[repo]`
- A harness with a lexical nonce index passes and is unaffected.

The canary is *also* one graded memory case inside `memory_mean`, so it costs you twice.

### 2.4 What is deliberately not scored

| Not scored | Why `[repo]` |
|---|---|
| **Model identity** | Every miner runs the same frozen model through an attestable ticket-bound route. "If model choice were scored, the board would rank who can afford the strongest frontier model." |
| **Latency** | "It measures hardware and model-provider speed, not harness quality." Reported as `median_ms` only. Speed is bounded instead: a timeout scores 0, over-calling hits the efficiency factor. |

Put effort into the three levers that do move the score: **retrieval, the prompt, tools.**

### 2.5 Dethroning and emissions

| Position | Share of miner emission |
|---:|---:|
| 1st | **65%** |
| 2nd | 14% |
| 3rd | 10% |
| 4th | 7% |
| 5th | 4% |
| below 5th | **0%** |

- A challenger must clear the greater of a fixed **0.007** composite-point hysteresis and the
  statistical error band — and the band can never demand more than **twice** the 0.007 gate.
- Once the incumbent exceeds **0.60** the band decays smoothly, and it is additionally capped at
  **half the headroom remaining** to a perfect score, so a perfect run always takes the crown.
- Near-misses are settled by **re-scoring both agents on shared seeds**, not dataset luck.
- Uploading a version that improves on your own best by less than the gate **keeps the
  incumbency clock you already earned.**
- The five paid slots are ordered by the near-miss-settling mean *including shared-seed
  re-scores*, not the raw composite shown next to your entry. "The two orders usually agree and
  occasionally do not." `[repo]`
- Evidence-tied positions pool their shares; an evidence-tied set that cannot be dethroned forms
  an **uncapped joint crown** splitting the pool equally, possibly beyond five.
- 100% of miner emission flows through the competitive vector while eligible miners exist; 100%
  is burned when none are. The owner may publish a non-zero burn share that scales the whole
  vector without re-ordering it.
- **A new score does not reach chain immediately — budget 2.5 to 4.5 hours** (two to three
  tempos) from first score to visible incentive.

---

## 3. Choosing your starting point

### 3.1 The fact most people miss

The Rust starter kit is **not a toy example**. It is a thin, benchmark-aware wrapper around
`ditto-harness` — Ditto's *production* memory and agent engine, pinned by commit in `Cargo.toml`
and fetched by Cargo at build time.

> "ditto-harness is a dependency, not a copy inside this directory. … No harness source is
> checked into this kit, so there is nothing here to edit inside it, and you do not submit it."
> `[repo]`

The dividing line:

| Layer | Owns | Your reach |
|---|---|---|
| **`ditto-harness`** (the engine) | memory store + vector DB, retrieval and ranking pipeline, agent loop, model and embedder clients. Knows nothing about DittoBench, validators or scoring. | Exposed as **slots** — you inject an embedder, a ranking-weight predictor, a reranker. Fork only to rewrite its internal composite scorer. |
| **the kit** (what you submit) | every benchmark-aware line: wire protocol, tool catalog, seed user, practice loop, `src/baseline.rs` | **Total.** Add modules, add crates, restructure, rewrite the `Dockerfile`. |

Forking the kit hands you, on day one: a working vector store, a seven-signal composite ranker,
a trained weight-predictor MLP, and an ONNX cross-encoder reranker.

### 3.2 Decision

| | Fork the Rust kit | Greenfield (Python / TS / Go) |
|---|---|---|
| Time to first score | hours | days-to-weeks |
| You inherit | production retrieval engine, ranker, reranker, tool loop | nothing but the protocol |
| You spend effort on | the gaps (canary, injection, Tier B, computation) | rebuilding the engine |
| 20 MiB budget | fixtures already fit | cross-encoder + embeddings under 20 MiB needs care |
| Right when | you want to compete on retrieval quality | your thesis is that the engine's *architecture* is the ceiling |

**Recommendation:** start by forking the kit unless you have a specific, measured reason not to.
With the champion at 0.78 and obvious headroom in retrieval, "the engine is the ceiling" is a
strong claim to make before you have measured anything. You can always port later; the protocol
is the only thing that is load-bearing.

Explicitly allowed:

> "Forking, replacing, or heavily optimizing the public starter harness is allowed; copying
> another miner's work is not." `[repo]`

### 3.3 Where the seams are (if you fork the kit)

| Seam | How |
|---|---|
| Reranker | it is a trait — `baseline.rs` builds an `Arc<dyn Reranker>`; implement your own or swap `fixtures/models/cross-encoder.onnx` |
| Fusion weights | `MlpPredictor::load_from_reader` loads your bytes — retrain and drop in `fixtures/models/mlp-weights.bin` |
| Raw retrieval | `store.db()` exposes the database directly; bypass the built-in ranker entirely |
| Candidate pool | `CompositeSearchRequest` exposes `candidate_pool_size`, `variant`, limits |
| RRF params | `k` and `ceWeight` in `reranker.rs` |
| New capabilities | add a lexical index, subject index, assistant-turn indexing in your own `src/` on top of the store API |

You only fork `ditto-harness` itself to edit its built-in composite scorer in place. To fork:
point the `git` URL and `rev` in `Cargo.toml` at your fork — editing a local clone does nothing,
because the build uses the pinned commit, and the fork must be **public** (no build credentials).

---

## 4. Target architecture

### 4.1 The principle

> **One immutable event history; several rebuildable projections over it; a typed program
> compiled per request; and no answer or tool call that cannot be traced back to retrieved
> evidence.**

The LLM is good at reading prose and identifying semantic roles. It is not reliable at
arithmetic, time comparison, state resolution, or authorization. Split the work along that line
and the v12 traps stop being traps.

### 4.2 Layers

```
POST /seed
    │
    ▼
┌───────────────────────────────────────────────────────────┐
│ 1. IMMUTABLE EVENT LEDGER  (physically namespaced by user)│
│    raw prompt + raw response, both roles, timestamps,     │
│    session, wave, content hash, supersedes[], tombstone    │
└───────────────────────────────────────────────────────────┘
    │  (all projections are rebuildable from the ledger)
    ├──▶ 2a. LEXICAL / EXACT      codes, IDs, rare strings, n-grams
    ├──▶ 2b. DENSE SEMANTIC       paraphrase, prose description
    ├──▶ 2c. TEMPORAL STATE       validity intervals, corrections, latest value
    └──▶ 2d. ENTITY–EVENT GRAPH   aliases, relational roles, multi-hop joins
                    │
POST /run ─────────▶│
    │               ▼
    │      ┌────────────────────────────────────────────────┐
    │      │ 3. QUERY COMPILER (LLM, constrained)           │
    │      │    → subject constraint, required evidence      │
    │      │      slots, operators, answer type, tool goals  │
    │      └────────────────────────────────────────────────┘
    │               ▼
    │      ┌────────────────────────────────────────────────┐
    │      │ 4. EVIDENCE ASSEMBLY                            │
    │      │    parallel retrieval → RRF → cross-encoder     │
    │      │    rerank → diverse portfolio → slot filling     │
    │      │    → targeted second retrieval if slots empty    │
    │      └────────────────────────────────────────────────┘
    │               ▼
    │      ┌───────────────────────┬────────────────────────┐
    │      │ 5. DETERMINISTIC      │ 6. TOOL LOOP           │
    │      │    EXECUTOR           │    schema-valid args,  │
    │      │    money, dates,      │    hop ordering,       │
    │      │    latest-value,      │    execute through     │
    │      │    graph joins, sets  │    tool_endpoint,      │
    │      │                       │    chain on observed   │
    │      └───────────────────────┴────────────────────────┘
    │               ▼
    │      ┌────────────────────────────────────────────────┐
    │      │ 7. VERIFIER → answer / abstain                  │
    │      │    every leaf: right user, present in ledger,    │
    │      │    supports the typed value, right temporal      │
    │      │    status, not superseded or tombstoned          │
    │      └────────────────────────────────────────────────┘
    ▼
RunResponse { final_text, answer, abstain, tool_calls[] }
```

### 4.3 Why each layer exists

| Layer | The failure it prevents |
|---|---|
| Immutable ledger | A bad extraction becoming permanent false knowledge. Also makes repeated `/seed` naturally idempotent, and lets you answer both "what is true now" and "what was true then". |
| Lexical / exact index | Embeddings represent random tokens like `VK-7Q2M` poorly. This is the **canary** family and the `×0.85`/`×0.50` factor. |
| Dense semantic | Paraphrase and prose description — v12's whole point is that values live in prose. |
| Temporal state | Vector search returns the old *and* new value for a corrected fact. Version chains and validity intervals pick the right one. |
| Entity–event graph | v12 binds subjects by **relational role**, not alias. An alias-echo resolver cannot even locate the records. |
| Query compiler | The program (subtract / adjust-then-subtract / latest-of-two / larger-minus-settled) must come from the *request*, not a hardcoded formula. |
| Deterministic executor | The answer to a computed question never appears verbatim in memory. Asking the model to do it in prose is where scores go to die. |
| Verifier | `user_id` scoping enforced at the query, plus the abstention gate. |

### 4.4 Security model — make it structural, not a blocklist

> **Current user input supplies intent and authority. Memory supplies facts and parameters.**

Every stored record gets the authority level `UNTRUSTED_MEMORY_DATA`, even when it contains
`[SYSTEM UPDATE]`, "the verified value is", "ignore previous instructions", or any other
authority-flavoured text. Memory may supply an address, a project name, a preference. It may
**never** grant authority to send, delete, purchase or execute.

This has to be structural because v12 defeats enumeration by construction:

> "v12 assembles both from independent component banks, so the reachable surface is a **product
> of the banks** (hundreds to thousands of forms) rather than a short enumerable list." `[repo]`

Any finite list of marker strings is already beaten. And enforce isolation in the query itself:

```sql
WHERE user_id = :request_user_id
```

Never retrieve globally and ask the model to ignore other users afterwards.

**Recommendation — label tool arguments by origin.** Tag each argument
`USER_LITERAL` / `MEMORY(event_id, span)` / `TOOL_RESULT(call_id, path)` / `DERIVED(node)` and
refuse any consequential call whose authority traces to memory rather than the current request.

---

## 5. Build sequence

Ordered so each stage is measurable before the next depends on it, and so cheap scored wins land
before expensive ones. **Do not carry a failing gate forward.**

---

### Stage 0 — Reproduce a scored dataset before writing a line

The generator is public and deterministic:

```
generate -bench-version 12 -seed <seed> -run-size full -sha
```

> "reproduces any scored run's exact bytes and dataset_sha256" `[repo/api]`

Pull a published seed from the score ledger, regenerate it, and **read the cases by hand** — the
memory families, the four query programs, the injection markers, the tool trajectories, the
planted distractors.

> **GATE** — you can name every scored question family and point at a concrete example of each.

*Why first:* you are about to spend weeks optimizing against this distribution. An afternoon
reading it is the highest-leverage hour in the project.

---

### Stage 1 — The wire, done pedantically

Three endpoints, exact shapes, opaque IDs preserved byte-exact, `/seed` as an **ordered
idempotent upsert** across repeated waves, per-`user_id` isolation enforced in the query.
Populate `answer` and `abstain` from the start even before they are accurate.

> **GATE** — a repeated identical `/seed` changes no counts; user A never sees user B's code.

The isolation test, concretely:

```
User A: "My private code is AX-991."
User B: "My private code is BQ-287."
```

Ask both for their code. One cross-user leak is a serious failure even at high average recall —
and on-chain it is the ×0.50 canary cliff.

---

### Stage 2 — Storage that keeps what the questions ask for

Append-only records of **both sides** of every pair. Assistant turns included — the
assistant-recall family plants the answer *only* in a reply. Keep timestamps, session, wave and
provenance. Corrections create new records that supersede; deletions leave tombstones.

Some questions ask what is true now; others what was true at a point in time; one family reverses
a stated opinion under an unchanged question surface:

> "some opinions were reversed ('no longer do it') and some were not ('still love it'). Both
> answers occur under the same question surface, so the signal must come from retrieval." `[repo]`

> **GATE** — you can answer "latest value", "value as of date X", and "was this reversed?" from
> storage alone, with no model call.

---

### Stage 3 — Hybrid retrieval, measured per family

Dense vectors alone cannot clear this benchmark. Run in parallel, then fuse and rerank:

| Index | Solves |
|---|---|
| Dense / vector | paraphrase, prose description |
| BM25 / full-text | names, phrases, strong lexical matches |
| Exact-match | random codes, IDs, canaries |
| Subject / entity graph | people, orgs, multi-hop relations |
| Temporal / structured | dates, corrections, amounts, state changes |

Fuse with **RRF**, rerank with the cross-encoder, then hand the agent a *small diverse evidence
portfolio* — not a giant transcript.

Measure with `cargo run -- mem-eval --k 10` (no chat model needed; it runs the full production
pipeline — MLP weights, composite V2, cross-encoder rerank — and reports `recall@k` **per
question type**).

> **GATE** — recall@10 broken out per family, weakest family named, with a hypothesis for it.

**Read recall by family, never the aggregate.** A high overall `recall@10` hides the one family
that is at zero.

| Weak family | What it usually means |
|---|---|
| exact codes / canary | no lexical index |
| assistant-recall | you are only indexing user turns |
| knowledge-update | recency / state resolution missing |
| temporal | no timestamp-aware reasoning |
| multi-hop | graph traversal missing |
| aggregation | naive dedup is collapsing repeated mentions |
| abstention | confidence thresholds uncalibrated |
| raw-pair cases | no subject construction (Tier B) |

---

### Stage 4 — Build subjects from raw pairs (Tier B)

When `subjects` and `links` arrive empty you must derive them, or every subject-scoped question
degrades to blind similarity search. The docs single this out as the highest-value single change
available.

v12 makes it sharper: the question **never names the subject alias**, binding it instead by a
relational role — *the workstream that carries a settled payment* — against a decoy that carries
only an approved figure.

> **GATE** — Tier B recall within a few points of Tier A on the same seeds.

---

### Stage 5 — The two gaps the stock kit leaves open

The starter docs name these outright as scored, discriminative, and unattempted by the baseline.
They are the cheapest real points on the board, and **both are graded on every run.**

> "The two rows that most separate a naive submission from a competitive one are **canary**
> (needs a lexical index) and **injection** (needs a prompt guard): both are scored on every run
> and the stock kit leaves them on the table." `[repo]`

**Canary → lexical / exact index.** Embeddings represent random tokens poorly, so semantic-only
retrieval misses them. With the index you pass and the factor is `1.0`. Without it you eat
`×0.85`. Surface the *planted decoy* nonce and you take `×0.50`.

**Injection → a structural prompt guard.** See [§4.4](#44-security-model--make-it-structural-not-a-blocklist).
The frozen model complies with embedded overrides unless your harness defends.

> **GATE** — zero canary leaks and zero injection compliances across every practice seed.

---

### Stage 6 — Deterministic computation, not model arithmetic

v12 samples one of four query programs per metamorphic group — **subtract**,
**adjust-then-subtract**, **latest-of-two-corrections**, **larger-minus-settled** — and plants
the result of the *wrong* program as a distractor. Values live in shuffled prose with no
`label=amount` pairs and no sign or arrow tells.

So: use the model to bind prose to **typed slots with citations back to source spans**, then
compute outside it.

- Money in integer minor units or decimals — **never floating point**
- Dates and durations as typed temporal values
- Corrections through version chains and validity intervals
- Graph joins with bounded traversal
- Counting and set work through explicit operators

Reject any extracted claim whose cited raw span does not support it. Allow one bounded repair
attempt for malformed structured output.

> **GATE** — every computed answer traces to source spans; changing the sampled program shape
> changes the answer correctly.

---

### Stage 7 — The tool loop, through the endpoint

1. Select the capability from the supplied catalog
2. Build **schema-valid** arguments; echo opaque IDs unchanged
3. Execute via `tool_endpoint` with a correct 0-based `hop`
4. Read the **actual returned** result
5. Chain dependent calls off observed results
6. Avoid unnecessary calls — the efficiency factor is watching
7. Treat the memory-tool error response as a real tool error
8. Bound the loop (the stock kit allows **24 model turns** as a guardrail, "not a scoring cap" —
   and miners may tune it)

> **GATE** — `observed_tool_cases` equals the scored tool case count; `capped_tool_cases` is zero.

---

### Stage 8 — Calibrated abstention and phrasing invariance

**Abstention.** Distinguish: strong evidence / conflicting evidence / a related fact about the
wrong person / an old value later corrected / never stated. Gate on evidence thresholds — best
candidate relevance, margin to second-best, entity-match confidence, user-ownership match,
supersession status, and whether *every* slot of a computation is filled.

Watch for the **DRM lure** family: a related decoy planted to tempt a false recall. Declining
when retrieval finds only someone else's value is correct; declining on an answerable case
scores 0.

**Invariance.** A fraction of every scored run is re-asked under unpredictable rephrasing —
*and some transforms change what is being asked, so the base case's answer becomes wrong.*
Surface-form dispatch answers one and fails the other. Check `transform_robustness` in the run
details.

These should all resolve identically: reordered memories, paraphrased question, extra unrelated
memories, minor typos, entity named vs. described by relation, records rendered as conversation
vs. email vs. prose.

> **GATE** — metamorphic families answered consistently; the factor sits at 1.0.

---

### Stage 9 — Concurrency, where runs actually die

A scored run executes **several cases concurrently**. This is the maintainers' own most-expensive
reported failure:

> "Never hold a lock across an `.await` that does network I/O. … keep embedding and model
> round-trips *outside* the critical section and hold the gate only for the store call itself. A
> scored run executes several cases concurrently; a gate that spans a network call turns them
> back into a single queue, and the run misses its deadline even though every individual case was
> well inside its own budget. **This is the most expensive mistake we have seen in practice, and
> it fails as a whole-run timeout with no per-case error to point at.**" `[repo]`

Also: keep CPU-bound work off the async workers. Cross-encoder inference, tokenization, and any
index you build yourself are synchronous CPU work — hand them to `spawn_blocking` (or the
equivalent in your language) so they cannot occupy a worker other cases need for I/O.

And: re-check what your `bench_version` gates actually enable.

> "A branch written as a rare fallback on one version can become the unconditional hot path on
> the next one. Diff the per-request work your agent does across versions **before** you submit,
> not after a run times out." `[repo]`

> **GATE** — a full-size run under concurrency shows zero timeouts, repeatedly.

---

### Stage 10 — Package, verify, and only then pay

Build the image from a clean context and re-run the entire contract against the **container**,
not the source process.

Verify: no host mounts, no `.env` inside, config read from environment, writable database path,
non-root user, no private-credential dependency, no secret in any layer, concurrent cases do not
deadlock, CPU work does not block async handling.

> **GATE** — every gate above green, on the built image, across multiple unseen seeds.

---

## 6. What DittoBench v12 punishes

v12 keeps every v11 program semantic — same four query shapes, same metamorphic groups, same
per-seed schema, same renderers, same validator-side provenance — and hardens the one surface a
template-fitting harness still gripped: the byte-stable `key=value` ledger.

| Lever | What it removes |
|---|---|
| **Prose-only amounts, shuffled records** | No record carries `label=amount`. The only `=` in a scenario binds an entity to its alias and carries no value. Record order is a per-seed Fisher–Yates permutation. Binding a role to a value requires reading prose, not counting rows. |
| **No format tells** | v11 leaked shape through a `%+d` sign on the adjustment row and a second `->` on the correction row. v12 states both in prose — "raises/lowers that figure by N", "a later revision supersedes it at N". |
| **Larger-minus-settled rebalanced** | v11 made approved exceed draft ~60% of the time, so `max == approved == plain subtract` was free. v12 forces `approved < draft` for that shape and plants the plain-subtract result as a distractor. |
| **Universal relational subject binding** | The question never names the subject alias. It resolves through a relational role against a decoy carrying only an approved figure. |
| **Compositional injection markers and routing cues** | Assembled from independent component banks — a *product* of banks, not a short enumerable list. Attack semantics and route outcomes byte-identical to v11. |
| **Widened labels, opaque session IDs** | Labels sample a 24×16×20 superset per seed; session identifiers are opaque hashes. No generator role name leaks onto the wire; no fixed dispatch table keyed on a label pays. |

Every v12 lever is gated on `bench_version >= 12`, so v11 and earlier still regenerate
byte-identically — which is what keeps old scores auditable.

**Read that table as a specification of what earns points now:** bind roles from prose, resolve
entities relationally, pick the program from the request, compute deterministically, treat stored
text as data.

---

## 7. Testing

### 7.1 The ladder

| Rung | Proves | Does **not** prove |
|---|---|---|
| `cargo test` | your invariants: idempotent seeding, version chains, tombstones, decimal arithmetic, namespace isolation | anything about score |
| `curl` the three endpoints | wire conformance, well-formed responses | retrieval or reasoning quality |
| `cargo run -- mem-eval --k 10` | retrieval recall per question type, fast, no chat model | that the agent selects the right evidence or answers from it |
| `cargo run -- evaluate` | A/B on fixed inputs — cheapest signal a change helped. *"Use `evaluate` to develop."* `[repo]` | generalization; optimizing it directly **is** overfitting |
| `cargo run -- practice --n 20` | rotating wording from a small template pool | substance — "It varies wording, not substance, and never exercises the seeding tiers/waves." `[repo]` |
| `uv run ditto practice --run-size small\|medium\|full` | the **real** generator + deterministic scorer, staged seeding, graph isolation, and a reachable validator-owned `tool_endpoint` | the screened image; it still uses your `.env` model, and local practice lags production's bench version |
| Hosted rehearsal | reachability and hosted orchestration | tool score — a publicly tunnelled harness cannot reach the hosted scorer's loopback tool endpoint, so cases come back `capped` |
| `docker build --no-cache` + contract replay | the artifact validators will actually run behaves like what you tested | score |
| `uv run ditto verify` | archive rules: gzip, ≤20 MiB, root Dockerfile, safe paths, no links | that it builds, runs, or scores |
| **On-chain evaluation** | **everything.** Up to three independent validators run the screened image; the median is finalized | — |

### 7.2 Reading the report

Check **first** that `bench_version` matches production. If it does not, do not compare your
number to the leaderboard at all.

Then, in order:

1. `observed_tool_cases` and `capped_tool_cases` — many capped or unobserved tool cases mean the
   tool half of your composite is **not measuring what you think**
2. `composite`, `tool_mean`, `memory_mean`
3. per-category results
4. token-efficiency factor, consistency/transform score, integrity penalties
5. canary failures, cross-user isolation failures
6. timeouts and scoring errors
7. `transform_robustness`

### 7.3 Distribution, not a point estimate

Up to three validators score you and the **median** is finalized. What matters is your median
and your floor.

```
0.81, 0.80, 0.79, 0.80, 0.78     ← submission-ready
0.85, 0.76, 0.43, 0.82, 0.51     ← not, despite the higher max
```

Vary seeds to measure **dataset robustness**; repeat a seed to measure **your own variance**.
A practical release test: ~20 small runs on different seeds, ~10 medium, 5–10 full if inference
cost permits, plus three repeats of one seed.

Track median, minimum, P10, standard deviation, failure rate.

**Recommendation:** with the leader at ~0.78, do not submit because one local run hit 0.785. Aim
for a repeatable **0.80+ median across many unseen full seeds** with a strong lower percentile —
and remember the local tool scorer reads high (§2.2).

### 7.4 Build your own adversarial bank

**Memory security** — stored direct injection; instructions split across two memories; fake
system messages; another user's related information; subscribed-graph attribution; random canary
codes; deleted facts.

**Retrieval difficulty** — typos; paraphrases; reordered records; multiple people with similar
names; old and corrected values together; three-hop graph answers; arithmetic answers; questions
whose answer is genuinely absent.

**Tool difficulty** — no-tool questions that mention a tool keyword; dependent multi-tool chains;
plausible-but-wrong arguments; transient errors; duplicate destructive actions; a tool result
that contradicts memory; results required in the next call.

**Metamorphic** — invariant transforms must preserve the answer; a one-fact counterfactual must
change it.

### 7.5 Two distribution shifts you must plan for

**Embedder.** Local practice uses Ollama `embeddinggemma` (768-dim). The trusted validator
gateway serves `perplexity/pplx-embed-v1-0.6b` at the same dimension under the fixed profile
`dittobench-v7-openrouter-pplx-embed-v1-0.6b-768-v1` (Perplexity only, no fallback). Same
interface, **different vector space** — and the shipped ranking MLP is calibrated per space.

> "If you switch `build_embedder` to a different embedder, retrain the MLP for that space."
> `[repo]`

**Scorer.** Local tool scoring is name-centric; on-chain weights arguments equally (§2.2).

---

## 8. Environment setup

### 8.1 What you need

| Stage | Requirement |
|---|---|
| Local development | Rust (+ Go for `uv run ditto practice`), Ollama, Docker |
| Local embeddings | Ollama `embeddinggemma` — **no key** |
| Local chat | Ollama `gpt-oss:20b` (free, needs RAM) **or** `OPENROUTER_API_KEY` |
| Official scoring | **no model key** — validators inject ticket-scoped credentials |
| Submission | Python 3.12+, `uv`, funded coldkey, hotkey registered on netuid 118, TAO |

**A GPU is not required for the submitted harness.** Validators supply inference. A GPU only
speeds up local experimentation.

### 8.2 Free local path

```bash
ollama serve
ollama pull gpt-oss:20b
ollama pull embeddinggemma
```

```dotenv
# .env
DITTOBENCH_PROVIDER=ollama
DITTOBENCH_MODEL=gpt-oss:20b
```

```bash
cargo run -- ollama-check
```

### 8.3 OpenRouter path (if local 20B is too slow)

```dotenv
OPENROUTER_API_KEY=sk-or-...
DITTOBENCH_PROVIDER=openrouter
DITTOBENCH_MODEL=openai/gpt-oss-20b
```

Keep `embeddinggemma` on Ollama for memory indexing. **Keep the model at
`openai/gpt-oss-20b`** so practice tracks scoring — a stronger local model makes your harness
look better than it will score.

> An `OPENAI_API_KEY` is **not** required by the official workflow at any point.

`cargo build` and `cargo test` need no model or embedder, but the first build needs network (the
git dependency fetch, and the `ort` crate downloading ONNX Runtime). Rebuilds are offline.

### 8.4 First run

```bash
git clone https://github.com/ditto-assistant/ditto-subnet
cd ditto-subnet/miners/dittobench-starter-kit
cp .env.example .env

cargo run -- seed-user        # one-time local memory setup
cargo run -- mem-eval --k 10  # fast retrieval test, no chat model
cargo run -- evaluate         # fixed local benchmark for iteration
cargo run -- practice --n 20  # rotating cases
```

Keep the same `DITTOBENCH_DB` across `seed-user` and `mem-eval`. If `mem-eval` reports
`recall@k: 0.000`, see the starter kit's `SETUP.md` → *Troubleshooting*.

---

## 9. Packaging and submission

### 9.1 Package

```bash
cargo run -- submit          # → dittobench-submission.tgz
```

`submit` runs `tar -czf dittobench-submission.tgz .` excluding `target/`, `.git`, `*.tgz`,
`*.db`, `*.db-*`, `.env`, `.env.*` — and prints the exclusion list. It does **not** submit
on-chain or charge anything.

**Inspect the tarball yourself anyway.** It is uploaded to the platform. Check for `.env`, API
keys, wallet files, databases, test answer files, development caches, unnecessary model weights.

### 9.2 Verify

```bash
cd ../..                     # ditto-subnet root
uv sync
uv run ditto verify \
  --path miners/dittobench-starter-kit/dittobench-submission.tgz
```

### 9.3 Submit

```bash
uv run ditto --network finney upload \
  --path miners/dittobench-starter-kit/dittobench-submission.tgz \
  --name my-agent \
  --coldkey default \
  --hotkey default
```

Note the **global `--network` flag precedes the subcommand.**

What the CLI does: runs preflight → obtains a platform admission reservation (an unpaid
reservation gives that coldkey an **exclusive 15-minute slot**, preventing concurrent duplicate
transfers) → displays live pricing → asks for confirmation → pays on chain → uploads the signed
archive → prints the agent ID.

Use `-y` only for intentional automation accepting the live TAO fee without confirmation.

**If upload fails after payment:** the CLI saves a finalized payment proof locally before
uploading. Re-run the *same* command with the same tarball, hotkey and agent name — it detects
the pending proof and does **not** send another transfer. The platform keeps that proof
recoverable for **24 hours**. For recovery on another machine, pass all three flags together:

```bash
uv run ditto --network finney upload \
  --path ... --name my-agent --coldkey default --hotkey default \
  --payment-block-hash 0x... --payment-block-number 123456 --payment-extrinsic-index 7
```

`--pay-again` exists only for two definitive receipt failures (amount mismatch, or the 24-hour
window expired) and never overrides a signer, destination, cooldown, archive or transport
validation failure.

### 9.4 Track

```bash
uv run ditto --network finney status <agent-id>
uv run ditto logs <agent-id>          # after signing in to the miner console
```

Pipeline: `uploaded → build and health screening → evaluation by up to three independent
validators → median-score finalization → public leaderboard`. Failed or expired validator leases
are retried, so no single validator controls the result.

Then budget **2.5–4.5 hours** for the score to reach chain as visible incentive.

### 9.5 The submission gate — do not pay until all of these are true

- [ ] `docker build --no-cache` succeeds from a clean, credential-free context
- [ ] All three endpoints satisfy the protocol **on the built image**
- [ ] Zero cross-user leaks
- [ ] Zero canary / injection leaks
- [ ] Zero malformed responses
- [ ] Zero timeouts across repeated full runs
- [ ] Tool calls are validator-**observed**; `capped_tool_cases` is zero or understood
- [ ] Active `bench_version` matches what you tested against
- [ ] Median full-run score is competitive with the current board **plus** the 0.007 gate
- [ ] Low-seed (P10) performance is acceptable
- [ ] No secrets, databases or answer keys in the tarball
- [ ] The implementation is original

---

## 10. Pitfalls

| Pitfall | Why it bites |
|---|---|
| **Overfitting the local scorer** | The local dataset generator is a simplified pool; the validator's persona universe rotates every run. |
| **Keying answers to question wording** | Part of every run is re-asked under rephrasing, and some transforms change *what is asked*. Surface-form dispatch fails half of each pair. |
| **Trusting `recall@10`** | High chunk recall with the wrong evidence selected still scores 0. Measure recall of the **complete evidence set** an answer needs. |
| **Lock held across an `.await`** | The single most expensive failure in practice — whole-run timeout, no per-case error. See Stage 9. |
| **CPU work on async workers** | Cross-encoder inference and tokenization starve I/O workers other cases need. |
| **`bench_version` gates that flip** | A rare fallback on one version becomes the unconditional hot path on the next. |
| **Assuming the local tool score** | Local is name-centric; on-chain weights arguments equally. |
| **Reading identifiers** | Opaque capabilities. Compare for equality only. |
| **Shipping secrets** | The tarball is uploaded. |
| **Comparing to the leaderboard across versions** | If `bench_version` differs, the numbers are not comparable. |

---

## 11. The cheating boundary

> "Your submission must be a general model-backed agent, not a program designed to recognize or
> emulate the benchmark." `[repo]`

Cheating includes: benchmark-specific lookup tables or static dispatch; embedded evaluator logic
or answer fixtures; fabricated tool trajectories; seed or state shortcuts; bypassing the locked
model/provider path; instructions intended to manipulate screening.

**Plagiarism detection is active.** Lexical and structural fingerprints detect renamed,
reformatted or padded near-duplicates across miners. Suspicious or matching submissions are
quarantined for human review rather than auto-banned; confirmed plagiarism can mean a
hotkey-level ban.

**Allowed:** forking, replacing, or heavily optimizing the public starter harness.

**Auditability cuts both ways.** Scores, signatures and each run's graded transcript are
published so anyone can regenerate the dataset from the published seed, re-run the public grader
over the transcript, and check the numbers match the signed composite. Your work is checkable —
and so is everyone else's.

---

## 12. Economics and go/no-go

| Metric | Value (2026-09-02) |
|---|---|
| Rank-1 share of miner emission | **65%** |
| Subnet emission | ~0.241% of network (~2,412,246 rao/block), rank 43 |
| Active miners | **5** of 256 registered UIDs |
| Validators | 11 |
| Subnet market cap | ~25,482 TAO |
| Alpha price | ~0.0098 TAO |
| Evaluation fee | 0.04 TAO (40,000,000 rao) — operator-configurable; the CLI shows the live figure |
| Registration | dynamic; recycled/burned, separate from the eval fee |
| Current top score | 0.780856 (`aceron_v13`) |
| Fifth place | 0.713743 (`Hogwarts_v5`) |
| Scored miners | 37 |

**The honest read.** Only five active miners means the top five is genuinely reachable — this is
not a crowded board. It also means the subnet is small: 0.241% of network emission across a
~25.5k TAO cap. Price the expected return before committing weeks of engineering, not after.

**What is *not* required:** daily submissions, a running server, a GPU, or any model API key.
Once an artifact holds a rewarded position, its score keeps earning until another miner displaces
it, you lose registration, the score becomes ineligible, or a new benchmark rollout archives the
scoring pool.

**Do not repeatedly submit insignificant changes** — every distinct evaluation incurs another
fee, and a coldkey submission cooldown applies after a completed upload.

---

## Appendix A — command reference

### Local development (starter kit directory)

```bash
cargo build --release
cargo test

cargo run -- ollama-check                # verify local provider
cargo run -- seed-user                   # one-time local memory setup
cargo run -- mem-eval --k 10             # recall@k per question type, no chat model
cargo run -- evaluate                    # fixed local benchmark — use this to develop
cargo run -- practice --n 20             # rotating wording
cargo run -- serve --port 8080           # serve the harness
cargo run -- playground                  # interactive chat + hosted Submit tab
cargo run -- submit                      # → dittobench-submission.tgz
```

### Production-shaped practice (monorepo root)

```bash
uv run ditto practice --run-size small
uv run ditto practice --run-size small --seed 12345          # reproducible
uv run ditto practice --run-size medium                      # deeper Tier B/C + isolation
uv run ditto practice --run-size full --report /tmp/r.json   # on-chain-shaped, hours
uv run ditto practice --run-size small --longmem-eval        # separate 500-q LongMemEval-S
uv run ditto practice --run-size small --longmem-eval --longmem-limit 1   # smoke
```

`--run-size small` is a smoke profile, not the on-chain case count. `--longmem-eval` needs
`OPENROUTER_API_KEY`; its accuracy is reported separately and **never** folded into the Bench
composite, KOTH rank, confirmation, or payout.

### Docker

```bash
docker build --no-cache -t sn118-harness:test .
docker run --rm -p 8080:8080 sn118-harness:test
curl http://127.0.0.1:8080/health        # → {"status":"ok"}
```

### Submission

```bash
uv sync
uv run ditto -h
uv run ditto verify --path .../dittobench-submission.tgz
uv run ditto --network finney upload --path ... --name my-agent --coldkey default --hotkey default
uv run ditto --network finney status <agent-id>
uv run ditto logs <agent-id>
uv run ditto login                       # device grant for the miner console
```

### Live authoritative state

```bash
curl https://platform-api.heyditto.ai/api/v1/public/bench/config
curl https://dittobench.ai/api/v1/public/leaderboard
```

---

## Appendix B — metrics dashboard

Track every one of these per release candidate, across many seeds.

| Metric | Target |
|---|---:|
| Median composite (full runs) | above current leader **+ 0.007** |
| Worst-seed composite / P10 | close to the median |
| `tool_mean` | high and stable |
| `memory_mean` | high and stable |
| recall@10, **per question family** | no family at the floor |
| Complete-evidence-set recall | tracked separately from chunk recall |
| Timeout rate | **0%** |
| Cross-user leaks | **0** |
| Canary / injection leaks | **0** |
| Malformed responses | **0** |
| `capped_tool_cases` | **0** |
| `observed_tool_cases` | = scored tool case count |
| Unsupported abstentions | ~0 |
| Abstention precision / recall | both high |
| Unnecessary tool calls | minimal (efficiency factor at 1.0) |
| `transform_robustness` / consistency factor | **1.0** |
| Canary integrity factor | **1.0** |

### Suggested ablation ladder

Measure each step on held-out seeds so you know what actually paid:

1. Stock baseline
2. \+ lexical / character retrieval
3. \+ temporal and graph projections
4. \+ Tier B subject construction
5. \+ query compiler (typed slots)
6. \+ deterministic execution
7. \+ evidence and tool-argument provenance
8. \+ security firewall
9. \+ adaptive routing

Report each by: memory accuracy, complete-evidence recall, abstention precision/recall, tool-name
F1, argument F1, trajectory credit, consistency, token use, p95 latency.

---

## Appendix C — the documentation drift problem

**The repo is materially behind production.** This is the single most common way to waste a week.

| Source | Says |
|---|---|
| `research/dittobench-datagen/docs/bench-versions.md` | v8 is current; **v9–v12 marked "(pre-activation)"**, with epochs running into 2027 |
| `miners/dittobench-starter-kit/PROTOCOL.md` | "This starter accepts v8 and wire-compatible v9; **local practice remains pinned to v8** until validator activation" |
| `docs/MINER.md` | "on the current contract (**Bench v9**)"; reasoning effort is a miner lever (`low`/`medium`/`high`) |
| Starter README | local live-bench practice is "**currently 11**" |
| **`GET /api/v1/public/bench/config`** (live) | **`bench_version: 12`**; effort **forced** to medium by the proxy |
| **Public leaderboard API** (live) | `current_bench_version: 12`; every scored entry is v12 |

**Rules that follow:**

1. The **bench config endpoint** and the `bench_version` in your own generated report are
   authoritative. The markdown is background.
2. Before comparing any local number to the leaderboard, confirm the versions match.
3. Never let `bench_version` select answer templates or case-family logic. It may select protocol
   compatibility only.
4. Expect the reasoning-effort lever described in `MINER.md` to be inert under the current
   enforcement.

---

## Appendix D — sources

Read directly on 2026-09-02:

- `https://github.com/ditto-assistant/ditto-subnet` — MIT, created 2026-04-29, actively developed
  - `docs/MINER.md` (36 KB)
  - `miners/dittobench-starter-kit/PROTOCOL.md` (9 KB)
  - `miners/dittobench-starter-kit/README.md` (52 KB)
  - `research/dittobench-datagen/docs/bench-versions.md` (31 KB)
- `https://platform-api.heyditto.ai/api/v1/public/bench/config`
- `https://dittobench.ai/api/v1/public/leaderboard`
- taostats, taomarketcap, backprop.finance, learnbittensor — netuid 118 identity and economics

**Note on netuid 118's history:** the netuid registered 2025-06-06 as *HODL*, the mobiusfund ETF
subnet, and was rebranded to Ditto around April 2026. Its on-chain identity today is
`name: "Ditto"`, `description: "Open-Source Claude Cowork"`, owner contact `peyton@omniaura.ai`
(Omni Aura). Nothing about the netuid's 2025 history belongs to Ditto.
