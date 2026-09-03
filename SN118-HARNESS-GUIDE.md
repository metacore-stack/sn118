# SN118 / DittoBench — Agent-Memory Harness Development Guide

> **Verified 2026-09-02** against `github.com/ditto-assistant/ditto-subnet` (`docs/MINER.md`,
> `miners/dittobench-starter-kit/PROTOCOL.md`, `miners/dittobench-starter-kit/README.md`,
> `research/dittobench-datagen/docs/bench-versions.md`), the live bench config
> (`platform-api.heyditto.ai/api/v1/public/bench/config`), the public leaderboard API,
> and four independent chain explorers.
>
> **Re-verified against live sources 2026-09-02 21:55–21:57 UTC.** Section 12, Appendix B and
> the leaderboard snapshot were materially wrong before that pass; see Appendix E for what
> changed.
>
> **Everything version-sensitive moves daily.** Re-read the bench config before acting on any
> number here.
>
> **Provenance markers.** Every non-obvious claim carries one:
>
> | Marker | Means |
> |---|---|
> | `[repo]` | Quoted verbatim from the repository at the cited path. |
> | `[live]` | Read from the platform API or chain during the verification pass. Reproducible now. |
> | `[inferred]` | Reasoning from marked inputs. The arithmetic is shown. |
> | `[unverified]` | No reachable source either way. Stated as a known hole, not a conclusion. |
> | **Recommendation** | Engineering judgement, not official guidance. |
>
> An unmarked sentence is exposition. If it asserts a number, that is a bug — report it.

---

## Table of contents

- [0. Five facts that decide your architecture](#0-five-facts-that-decide-your-architecture)
- [1. The contract](#1-the-contract)
- [2. How you are scored](#2-how-you-are-scored)  ·  [2.3 integrity multipliers](#23-the-integrity-multipliers)  ·  [2.5 dethroning](#25-dethroning-and-emissions)
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
- [Appendix E — what the verification pass changed](#appendix-e--what-the-verification-pass-changed)

---

## 0. Five facts that decide your architecture

Read these before designing anything. Each one invalidates a design that looks reasonable
without it.

### 0.1 Your container has no internet

The live bench config states the enforcement plainly:

> `"enforcement": "ticket-scoped platform proxy forces the model and medium reasoning effort
> and holds the upstream key outside the sandbox; sandbox egress is deny-all"` `[repo/api]`

**Three** holes exist in that boundary. Earlier revisions of this guide said two and omitted the
embedding gateway; that error is worth understanding, because it makes you ship a model you are
already being handed.

| Hole | What it is | How you reach it |
|---|---|---|
| Platform model relay | Serves the locked `openai/gpt-oss-20b`. Your `DITTOBENCH_MODEL` is overridden. | `DITTOBENCH_PROVIDER=platform`, URL injected as `DITTOBENCH_INFERENCE_BASE_URL` |
| Embedding gateway | Serves the validator's embedder. See [§7.5](#75-two-distribution-shifts-you-must-plan-for). | URL injected over `OLLAMA_BASE_URL`, **Ollama** wire format (`/api/embed`) |
| `tool_endpoint` | A validator-served mock tool executor, supplied per case in the `RunRequest`. | Per-case field in the `RunRequest` |

**The model relay contract, in full.** This is the single most important thing the protocol docs
do not state; it lives only in `miners/dittobench-starter-kit/src/baseline.rs`. `[repo]`

```
DITTOBENCH_PROVIDER=platform          # legacy alias: chutes
DITTOBENCH_INFERENCE_BASE_URL=...     # injected by the validator
Authorization: Bearer ticket          # literal string, not a secret
```

Speak **OpenAI-compatible chat completions** against that base URL. The reference kit pins
`temperature: 0.0` and `seed: 42` for determinism. `RunRequest.inference_base_url` also exists on
the wire, but the scorer no longer mints one and leaves the field empty — read the environment,
not the request. `[repo]`

> **If you are writing a greenfield harness, this paragraph is the whole ballgame.** A harness that
> does not implement this has no model access at all and scores only what it can do without an
> LLM. See [§3.2](#32-decision).

**The embedding gateway matters more than it looks.** Any dense-retrieval design must embed the
*query* at `/run` time, not just the corpus at `/seed` time. That is a network call on the hot
path, inside the 60 s budget, under concurrency. Budget for it. And note the wire format: a
harness that assumes OpenAI-shaped `/v1/embeddings` gets nothing. The starter kit's `.env.example`
pre-empts the obvious guess — "`DITTOBENCH_EMBED_PROVIDER`, `DITTOBENCH_EMBED_MODEL`, and
`DITTOBENCH_EMBED_BASE_URL` are not kit settings." `[repo]`

**Consequences.** No model pull at runtime. No package fetch. No calling your own API. No
downloading an index. No API key of yours is ever used during scoring, so shipping one buys
nothing and leaks it into an uploaded tarball.

**What the 20 MiB cap does *not* constrain.** It caps the build *context*, not the image. `cargo
fetch`, `pip install` and the `ort` crate's ONNX Runtime download all happen during `docker
build`, so an arbitrarily large public dependency tree is fine. And the cap is not close to
binding: the starter kit's entire shipped model payload measures **5.32 MiB** — cross-encoder
4,476,244 B + vocab 231,508 B + MLP weights 867,972 B — leaving ~14 MiB unspent. `[repo]` The four
projections in [§4.2](#42-layers) are code and schema, not weights. Size your artifact against the
runtime box in [Stage 10](#stage-10--package-verify-and-only-then-pay), not against this number.

### 0.2 The trajectory that is graded is the one the validator observed

> "A harness that ignores `tool_endpoint` scores 0 on the on-chain scored path." `[repo]`

Self-reported `tool_calls` are not evidence. The validator serves the mock endpoint, records
every call, and grades that record. It also self-checks the endpoint before scoring, so "the
listener was down" is not a failure mode you inherit — if the listener is healthy and you never
call it, you simply get the zero.

**The repo contradicts itself on the severity, and you should know which model you are debugging
with.** `PROTOCOL.md` says *scores 0*; the generator's own wire contract says such cases are
*capped*, and the telemetry field is literally named `capped_tool_cases`; the kit README adds a
third phrasing scoped to v8 ("an unobserved observable case scores 0"). `[repo]` Neither reading
changes the instruction — route every call through `tool_endpoint` — but it changes triage. Under
"scores 0" a nonzero `capped_tool_cases` is catastrophic; under "capped" it is a recoverable
ceiling on specific categories. Treat the harsher reading as your planning assumption and the
milder one as your debugging hypothesis. The distinction is **`[unverified]`** for v12.

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
amount of good recall recovers. See [§2.3](#23-the-integrity-multipliers).

### 0.5 The board reset because an exploit was closed, not because the problem is unsolved

The v12 spec is explicit about its own motivation:

> "A harness that scored **0.997 on v11** never read the randomized prose; it parsed the
> fixed-order KV rows, fired a model call only to satisfy the attribution gate, and computed a
> balance positionally." `[repo]`

The current champion sits at ~0.78 on v12. Read that as *"the shortcut was recently removed"*,
not *"nobody can do this"*. It also tells you exactly what earns points now: read prose, bind
relationally, pick the program from the request, compute deterministically.

**How recently is `[unverified]`, and it matters more than it sounds.** No reachable source gives
a v12 activation date: the repo's version table still labels v9–v12 "(pre-activation)" with
nominal epochs in 2027, the score ledger that would carry the history is authenticated (see
[Stage 0](#stage-0--reproduce-a-scored-dataset-before-writing-a-line)), and the leaderboard's
oldest visible timestamp reflects entry refreshes rather than rollout. Since a scoring pool is
archived on benchmark rollover, pool lifetime is the denominator of your entire business case —
see [§12](#12-economics-and-gono-go). You cannot currently get that number.

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
| Runtime | serves the protocol on **`0.0.0.0:8080`** — bind the wildcard, not `127.0.0.1` |
| Contents | no `.env`, no wallet key, no API key, no answer fixtures |

Language is free — Rust, Python, TypeScript, Go, anything. **Docker is not optional.**

**Two silent screening failures worth one line each.** A Flask / FastAPI / Express harness left on
its `127.0.0.1` default is unreachable from outside the container and fails the 10 s `/health`
gate outright — a paid failure with a one-word cause. And the request body limit: a full-size
`/seed` body is large enough that the reference kit sets **256 MB**. `[repo]` Framework defaults
are nowhere near — `express.json()` is 100 KB, axum's default is 2 MB, Starlette patterns cap low.
Get this wrong and you return 413 on the first full `/seed`, seed zero pairs, and score ~0 on
`memory_mean`, with no per-case error pointing at the cause.

**Dockerfile constraints the starter kit encodes and you inherit.** `[repo]` The base images are
digest-pinned and ONNX Runtime needs **glibc ≥ 2.38**, so swapping to a more familiar
`bookworm-slim` produces a *link* failure that surfaces during screening, after payment. The build
uses `--locked`, so a `Cargo.lock` left stale by adding a dependency fails fast. Rust ≥ 1.85. If
you rewrite the `Dockerfile` — [§3.1](#31-the-fact-most-people-miss) says you may — re-check all
four.

### 1.2 The three endpoints

```
GET  /health   →  200 {"status":"ok"}
POST /seed     →  200 {"pairs":N,"subjects":N,"links":N}
POST /run      →  200 RunResponse
```

The reference kit's `/health` also returns a `capabilities` array (`case_scoped_inference_v1`),
which looks like the negotiation signal for the per-case relay path the generator describes as
"restored". `[repo]` Whether omitting it has any effect today is **`[unverified]`** — but
`{"status":"ok"}` is not the complete health contract the reference implementation ships.

**`RunRequest`** carries `case_id`, `bench_version`, `system_prompt`, `user_input`, `tools[]`,
and — on scored tool cases — `tool_endpoint` and `user_id`.

The full `SeedRequest` shape — what `pairs`, `subjects` and `links` actually contain field by
field — and the `tools[]` schema you must build arguments against are **not reproduced in this
guide and are not in `PROTOCOL.md` either**. Read them off `research/dittobench-datagen/protocol`
and off a real request captured from local practice before you design your ingest.

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

**But do not stop there — the corollary is a scored family.** The memory tools in the supplied
catalog are *yours to implement*. `tool_endpoint` declining them is the validator telling you it
is not your backend, not that the call is a no-op. There is a class of write-then-read
**`LifecycleCases`** in which one wave instructs a mutation (`save_memory`, `update_memory`,
`delete_memory`) and a **later wave asks a question that is only answerable if the write
landed**. `[repo]` A harness that treats the error as "absorb and move on" passes the instruction
case superficially and then silently fails the read case.

Two consequences for the architecture in [§4](#4-target-architecture): the ledger cannot be
populated exclusively from `POST /seed`, and the write path has to land somewhere under `/tmp`,
because everything else is read-only — see [Stage 10](#stage-10--package-verify-and-only-then-pay).

On result-usage cases the validator additionally grades whether your final answer incorporates
the value the executed tool returned (`CaseScore.result_usage`, 0–1).

`RunResponse` also accepts an advisory `confidence` field, scored for calibration but never folded
into the composite. `[repo]` Omitting it is safe. Emitting it is free — if you build the
abstention machinery [Stage 8](#stage-8--calibrated-abstention-and-phrasing-invariance) demands
you already have the number — and it gives you a per-case calibration signal in the report to tune
the abstention gate against.

### 1.3 Timeouts

| Call | Ceiling | Failure |
|---|---|---|
| `GET /health` | **10 s** | container start → healthy. Miss it and screening fails outright. |
| `POST /run` | **60 s** | per case; a miss scores **0** for that case |
| `POST /seed` | **5 min** | per wave |
| **whole run** | **`[unverified]`** | every case passes, the run still misses its deadline, and you get no per-case error |

**That fourth row is the one that will kill you, and its value is published nowhere** — not in
`PROTOCOL.md`, `MINER.md`, the starter README, or the bench config. Its existence is not in doubt:
the repo quote in [Stage 9](#stage-9--concurrency-where-runs-actually-die) describes exactly this
failure. The scale is knowable even though the limit is not. `[live]` Every leaderboard entry carries
`n: 351` alongside `median_ms`; `[inferred]` reading `n` as the per-run case count and the
champion's `median_ms` of ~7.5 s as per-case latency, a serial harness needs roughly **44
minutes** before any of your own work. Concurrency is not an optimization
here; it is the difference between finishing and not.

Latency is *reported* (`median_ms`) but never ranked. Speed is bounded, not scored — but a
whole-run timeout is unbounded damage, so track p95 `/run` as your leading indicator against the
60 s ceiling.

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

**Argument F1 is ~20% of your whole composite** — `0.4 × arg F1 × 0.5` — and it is the most
mechanically winnable number on the board. `[inferred]` Note what the on-chain formula does *not*
reward: provenance. Labelling where an argument came from (the **Recommendation** in
[§4.4](#44-security-model--make-it-structural-not-a-blocklist)) is a security control, not an
accuracy one; it constrains *where* a value came from and says nothing about how it is
serialized. What moves arg F1 is dull and testable — build an argument canonicalizer, per
[Stage 7](#stage-7--the-tool-loop-through-the-endpoint).

**Where does `result_usage` sit?** The three weights above sum to 1.0, and
[§1.2](#12-the-three-endpoints) says `result_usage` is graded "additionally". Both statements are
verbatim repo text and the guide cannot reconcile them for you: whether it folds into the 0.2
trajectory term, replaces a term on those cases, or sits outside `tool_mean` entirely is
**`[unverified]`**. It matters because you cannot price the work in Stage 7 steps 4–5 without
knowing the weight. Measure it: run a case set with and without result integration and diff the
reported `tool_mean`.

### 2.3 The integrity multipliers

The composite is multiplied by bounded factors. Each is `1.0` when its trigger is absent, so
accuracy stays dominant — but they are not equally forgiving, and there are **four**, not three.

| Factor | Range | Trigger |
|---|---|---|
| **Tool efficiency** | `[0.85, 1.0]` | observed-execution runs; the first extra call is free, then the over-call penalty saturates |
| **Metamorphic consistency** | `[0.85, 1.0]` | `1 − 0.15 × (fraction of invariance families answered inconsistently)` |
| **Canary integrity** | `1.0` / `0.85` / **`0.50`** | see below |
| **Conversational sanity** | conjunction | greeting non-leak, declarative acknowledgement, behaviour-change application |

> **The bounds in the first three rows may be two versions stale, and this is the largest
> single uncertainty in the document.** `[unverified]` They are quoted correctly from the repo,
> but a second public source indicates v7 moved the canary leak to **×0.25** and widened both
> bounded factors to a 40% maximum penalty — floors of **0.60**, not 0.85. Production is v12.
> `MINER.md` declines to publish formulas, the datagen version doc does not give them, and the
> bench config returns nulls for integrity factors. If the floors are really 0.60, the query
> compiler stops being a risk to manage and becomes the largest scoring exposure in your design
> — see [Stage 6](#stage-6--deterministic-computation-not-model-arithmetic).

**Conversational sanity is a scored surface this guide previously omitted entirely, and the
architecture in [§4](#4-target-architecture) actively pushes against it.** `[repo]` It is a
conjunction by construction, so passing the greeting slice with a canned "Got it!" cannot dilute
failures elsewhere. The three behaviours:

- **Greeting non-leak** — a harness built to aggressively retrieve on every turn answers "hi"
  with the user's stored facts, potentially including the canary. Two failures for one reflex.
- **Declarative acknowledgement** — the user *states* a fact. The correct action is to
  acknowledge and store, not to answer a question that was not asked.
- **Behaviour-change application** — the user changes a standing instruction; it must take effect.

Nothing in the [§4.2](#42-layers) pipeline or the [§5](#5-build-sequence) sequence has a path for
a non-question turn at all. Add a turn classifier ahead of retrieval. `[inferred]` Given the
champion's `memory_mean` of 0.7271, this is plausibly a real slice of the remaining headroom.

**A fifth factor exists but is currently switched off.** `[live]` The live config carries a
token-efficiency curve — `minimum_factor: 0.85`, **`maximum_factor: 1.1`**, `bonus_cap: 0.05`,
`curve_version: 4`, `n_min: 8` — with `active: false`, and every leaderboard entry's
`efficiency_factor` is null. Note the direction: it is a *bonus* channel as well as a penalty, the
only way to exceed 1.0, and it can switch on without notice. Poll `efficiency.active` before you
tune token use for its own sake.

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

Put effort into the levers that do move the score — but weight them by where the headroom
actually is, not evenly.

`[live]` The champion sits at `tool_mean 0.9572` and `memory_mean 0.7271`. That is **0.1365
composite points of memory headroom against 0.0214 of tool headroom — a factor of 6.4** — and
every one of the current top five shows the same shape. Tools are close to solved on this board;
memory is not.

And do **not** read this section as "ignore the integrity factors." They are bounded, but the
bound is large relative to the thing that decides the crown: a single metamorphic factor at 0.85
costs 0.117 composite on a 0.78 base, which is **sixteen times** the 0.007 dethroning gate. The
factors are not where you *find* points; they are where you *lose* the ones you found.

### 2.5 Dethroning and emissions

| Position | Share of miner emission |
|---:|---:|
| 1st | **65%** |
| 2nd | 14% |
| 3rd | 10% |
| 4th | 7% |
| 5th | 4% |
| below 5th | **0%** |

- **Do not compute this yourself — it is published.** `[live]` The leaderboard's
  `emissions.champion_defense.required_score` is the live number a challenger must beat. At the
  verification pass it read **0.7924**, against a champion at 0.7809 — a required lead of 0.0115,
  not the 0.007 a naive reading gives. Poll the field.
- A challenger must clear the greater of a fixed **0.007** composite-point hysteresis and the
  statistical error band — and the band can never demand more than **twice** the 0.007 gate.
  Confirmed live: `margin_lead: 0.007`, `statistical_lead: 0.014`, `required_lead: 0.0097`.
- Once the incumbent exceeds **0.60** the band decays smoothly, and it is additionally capped at
  **half the headroom remaining** to a perfect score, so a perfect run always takes the crown.
- Near-misses are settled by **re-scoring both agents on shared seeds**, not dataset luck.
- Uploading a version that improves on your own best by less than the gate **keeps the
  incumbency clock you already earned.**
- The five paid slots are ordered by the near-miss-settling mean *including shared-seed
  re-scores*, not the raw composite shown next to your entry. "The two orders usually agree and
  occasionally do not." `[repo]`
- Evidence-tied positions pool their shares; an evidence-tied set that cannot be dethroned forms
  an **uncapped joint crown** splitting the pool equally, possibly beyond five. `[live]` **Not yet
  active** — `tie_weighting_active: false`, gated behind `tie_weighting_required_protocol: 20`,
  which the network has not reached. Plan as if it does not exist.
- **Confirmation is a paired re-score, so being better is not enough — you must be measurably
  better.** `[live]` The method is `paired` over `shared_seed_count: 15` with
  `paired_standard_error: 0.0171`. `[inferred]` Modelling your confirmation probability as
  `Φ((true_lead − required_lead) / SE)`: a harness at the naive 0.7879 target confirms **~44%** of
  the time; at 0.80, **~71%**; at 0.805, **~80%**; at 0.81, **~87%**. For 90% you need a true
  median near **0.8125**. Every failed attempt costs another evaluation fee and a cooldown.
- 100% of miner emission flows through the competitive vector while eligible miners exist; 100%
  is burned when none are. The owner may publish a non-zero burn share that scales the whole
  vector without re-ordering it.
- **A new score does not reach chain immediately — budget 2.5 to 4.5 hours** (two to three
  tempos) from first score to visible incentive.

**The ranked number is not a median of three validators.** `[live]` That is the per-wave quorum
(`score_count: 3 / score_quorum: 3`), and it describes screening one submission. The figure you
are ranked and paid on is `official_composite`, whose `aggregate_method` is **`continual_mean`
over up to 32 retained waves**. Three consequences the median model does not predict:

1. Your score is **continuously re-sampled after submission** and drifts for days without you
   resubmitting. A leaderboard entry moved 0.7537 → 0.7502 overnight during verification, with no
   new upload.
2. A single lucky run cannot take or hold the crown. Gate your release on a distribution whose
   **10th percentile** clears the incumbent's continual mean — not a median that clears its
   displayed score.
3. **Each entry carries two different numbers** and they are not interchangeable. `composite` is
   the raw/latest figure; `official_composite` is the continual mean, and it is the one that
   ranks and pays. At verification the champion showed `composite: 0.804983` beside
   `official_composite: 0.7808562`. Quoting the wrong one moves your target by 0.024.

The fields that actually govern standing: `official_composite`, `composite_stderr`,
`retained_sample_count`, `confirmation_seed_depth`.

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
| 20 MiB budget | fixtures already fit | *not the real constraint* — see [§0.1](#01-your-container-has-no-internet) |
| Model + embedder access | already wired | **you must implement it yourself** — see below |
| Right when | you want to compete on retrieval quality | your thesis is that the engine's *architecture* is the ceiling |

> **The greenfield column carries a hazard that is easy to miss.** The model relay and embedding
> gateway contracts in [§0.1](#01-your-container-has-no-internet) exist only in the starter kit's
> `baseline.rs` — they are in neither `PROTOCOL.md` nor `MINER.md`. A greenfield harness that does
> not reimplement them has **no model access and no embeddings at all**, and scores only what it
> can do without an LLM. Read `baseline.rs` before you write a line of Python.

**Recommendation:** start by forking the kit unless you have a specific, measured reason not to.
You can always port later; the protocol is the only thing that is load-bearing.

Two corrections to the usual arguments for this, because the reasoning matters more than the
conclusion. The case is **not** the 20 MiB budget — that is not binding (5.32 MiB of 20 used, and
the cap is on the build context, not the image). The real case is two things: you inherit the
trained MLP weights and the seven-signal composite ranker, which are *calibrated artifacts* you
would otherwise have to reproduce; and time-to-score option value is high because the revenue
horizon is short — see [§12](#12-economics-and-gono-go). Read the "hours" in the first row as
*hours to first **local** score*: the only thing producing a real score is a paid on-chain
evaluation, which [§9.5](#95-the-submission-gate--do-not-pay-until-all-of-these-are-true) tells
you not to run until you can beat the board.

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

The generator is public and deterministic. **You must build it first — it is a Go binary and the
guide previously failed to say so.** `[repo]`

```bash
git clone https://github.com/ditto-assistant/ditto-subnet
cd ditto-subnet/research/dittobench-datagen
go build ./cmd/generate          # Go 1.23+, standard library only, no external deps
```

> **Do not follow the upstream `go install` path.** `[live]` It resolves to an *archived*
> standalone repository with no v12 support at all — `BenchVersionV12` occurs zero times there —
> so it silently produces a generator that rejects `-bench-version 12`. The datagen README is
> stale in both copies: it says generation supports "v2 through the pre-activation v10 contract"
> and its SHA-256 vector table stops at v10, so you are told twice that v12 is unavailable while
> the monorepo code supports it.

Then generate:

```bash
./generate -bench-version 12 -seed 123456789 -run-size full -sha
```

> "reproduces any scored run's exact bytes and dataset_sha256" `[repo/api]`

**You do not need a seed from a scored run, and you cannot get one.** `[live]` The score ledger is
at `ledger_path` in the bench config (`/api/v1/scoring/scores`) and it is validator-authenticated
— it returns `401 {"message":"validator authentication failed"}` to a miner. Both
`public_transcript_url_template` and `public_mirror_url_template` are currently `null`, so no
scored run's seed or transcript is retrievable by you. (This also qualifies the auditability claim
in [§11](#11-the-cheating-boundary): the mechanism is designed, but the public endpoints are not
currently serving.)

None of that matters, because the generator is deterministic and **any seed samples the same
distribution**. Use the canonical public full-profile seed `123456789`, which the datagen README
documents with CI-asserted SHA-256 vectors; or run `generate -bench-version 12 -run-size small`,
which prints a fresh random seed on stderr. Generate several and read across them.

Now **read the cases by hand** — the memory families, the four query programs, the injection
markers, the tool trajectories, the planted distractors.

> **GATE** — you can name every scored question family and point at a concrete example of each,
> from datasets you generated yourself.

Note the gate says *name the families you can observe*, not *recover the generator's labels*:
[§1.5](#15-opaque-identifiers--a-real-trap) says question/family/category labels are deliberately
absent from the wire. You are building your own taxonomy from the cases, which is the point.

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

**The repair attempt fixes the easy failure. Budget for the hard one.** `[inferred]` Schema-
*invalid* output is cheap to handle — at a realistic 3–10% malformed rate, one independent repair
drives the residual to ~0.1–1%. The dangerous case is schema-**valid and semantically wrong**: the
compiler emitting `operators: ["subtract"]` where the request implies *larger-minus-settled*. That
failure is silent, it produces a confident wrong answer, and it is the **only component in the
whole design that manufactures metamorphic inconsistency** — because the consistency factor
re-asks the same question paraphrased, and a stochastic compiler that flips program between a base
case and its twin fails exactly the thing being measured.

Price it with the formula in [§2.3](#23-the-integrity-multipliers): a 10% program-flip rate gives
`1 − 0.15 × 0.10 = 0.985`, i.e. **−0.012 composite** on a 0.80 base — 1.7× the dethroning gate. If
the floors are really 0.60 rather than 0.85, roughly triple that.

So give the compiler a consistency mechanism, not just a repair path:

- **Cache the compiled program by semantic key**, not by question string — normalized entities,
  operators and answer type — so paraphrases of one question resolve to one program by
  construction.
- **Self-consistency sample** the compile step (3 draws, majority program, deterministic
  tie-break) on any request whose first compile is low-confidence.
- **Assert program stability** in testing: compile each question and its paraphrases, and fail the
  build on disagreement. This is cheap — it needs no scoring run.

> **GATE** — every computed answer traces to source spans; changing the sampled program shape
> changes the answer correctly; and paraphrases of one question compile to one program.

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
   and miners may tune it). Note the interaction with the whole-run deadline in
   [§1.3](#13-timeouts): 24 turns at gpt-oss-20b medium-effort latency can exceed the 60 s case
   ceiling on its own, so the guardrail is not a safe default — tune it *down* against measured
   p95, not up.

**Build an argument canonicalizer.** `[inferred]` Argument F1 is ~20% of the composite
([§2.2](#22-tool-grading--on-chain-differs-from-local)) and it is lost to serialization, not to
reasoning. One normalization pass, with a golden-file test per tool schema:

- **byte-exact echo** of every opaque identifier — round-trip them through your JSON layer and
  assert equality; a serializer that re-types or reorders silently costs you the case
- **numeric type discipline** — `3418` vs `"3418"` vs `"3,418"` are three different answers
- **unit and currency normalization**, decided once and applied everywhere
- **canonical key ordering**, and **omit** optional keys the schema does not require rather than
  emitting `null`
- **whitespace and casing** normalization on free-text arguments

Measure name F1 and argument F1 *separately* in local practice. A combined tool score hides which
of the two is failing, and they have completely different fixes.

> **GATE** — `observed_tool_cases` equals the scored tool case count; `capped_tool_cases` is zero
> or understood ([§0.2](#02-the-trajectory-that-is-graded-is-the-one-the-validator-observed));
> argument F1 tracked as its own number.

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

Size the problem before you tune it. `[live]` Every leaderboard entry reports `n: 351` and a
`median_ms` (~7.5 s for the champion). `[inferred]` If `n` is the per-run case count, that is
about **44 minutes of serial latency** before any of your own work — confirm it against your own
`--run-size full` report rather than taking the reading on trust. The whole-run deadline is unpublished ([§1.3](#13-timeouts)), so treat aggregate
throughput, not per-case latency, as the quantity you are engineering.

> **GATE** — a full-size run under concurrency shows zero timeouts, repeatedly.

---

### Stage 10 — Package, verify, and only then pay

Build the image from a clean context and re-run the entire contract against the **container**,
not the source process.

**Know the box you are shipping into.** `[repo]` Neither the protocol docs nor earlier revisions
of this guide stated it, and "writable database path, non-root user" badly undersells it:

| Constraint | Consequence |
|---|---|
| **Root filesystem is read-only** | Every path except `/tmp` fails with `EROFS` at runtime |
| **Only `/tmp` is writable, and it is a tmpfs** | Anything you build there consumes the RAM allowance and does **not** persist between runs |
| **Runs as uid 65532** | Non-root is enforced, not advisory |

What that catches, concretely: your SQLite/Turso file, any lexical or BM25 index you build during
`/seed`, tokenizer and HuggingFace caches (`HF_HOME`, `TRANSFORMERS_CACHE`, `~/.cache`), ONNX
Runtime's temp extraction, and any log file. All of it must be redirected under `/tmp`. Note the
starter kit's own code default is `./dittobench.db` relative to `/app` — which would fail; only
the Dockerfile's `ENV DITTOBENCH_DB=/tmp/dittobench.db` saves it. A greenfield harness that writes
anywhere else dies on the first `/seed`.

Verify: no host mounts, no `.env` inside, config read from environment, **every write path under
`/tmp`**, non-root user, no private-credential dependency, no secret in any layer, concurrent
cases do not deadlock, CPU work does not block async handling.

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
| `cargo run -- evaluate` | A/B on fixed inputs — cheapest signal a change helped. *"Use `evaluate` to develop."* `[repo]` | **anything about v12** — it generates **bench_version 9** datasets; also generalization |
| `cargo run -- practice --n 20` | rotating wording from a small template pool | **anything about v12** (same v9 path); substance — "It varies wording, not substance, and never exercises the seeding tiers/waves." `[repo]` |
| `uv run ditto practice --run-size small\|medium\|full` | the **real** generator + deterministic scorer, staged seeding, graph isolation, and a reachable validator-owned `tool_endpoint` | the screened image; it still uses your `.env` model, and local practice lags production's bench version |
| Hosted rehearsal | reachability and hosted orchestration | tool score — a publicly tunnelled harness cannot reach the hosted scorer's loopback tool endpoint, so cases come back `capped` |
| `docker build --no-cache` + contract replay | the artifact validators will actually run behaves like what you tested | score |
| `uv run ditto verify` | archive rules: gzip, ≤20 MiB, root Dockerfile, safe paths, no links | that it builds, runs, or scores |
| **On-chain evaluation** | **everything.** Up to three validators run the screened image per wave; the board then aggregates a `continual_mean` over retained waves ([§2.5](#25-dethroning-and-emissions)) | — |

> **Your tightest feedback loop is blind to the thing you are being scored on.** `[repo]`
> `evaluate` and `practice` run **bench_version 9** — `protocol.rs` pins the generated version
> even though `MAX_SUPPORTED_BENCH_VERSION` is 12, so the kit *accepts* v12 requests while
> *generating* v9 ones. Every v12 lever in [§6](#6-what-dittobench-v12-punishes) — prose-only
> amounts, Fisher–Yates shuffling, removed format tells, relational subject binding, the
> rebalanced larger-minus-settled shape, compositional injection banks — is **absent** from what
> you are iterating against. Nothing errors; you simply optimize the wrong distribution, and a
> change that helps on v9 can be neutral or harmful on v12.
>
> Use `evaluate` for what it is genuinely good at — regression-checking your own invariants and
> refactors — and do all v12 tuning against `uv run ditto practice` or datasets you generate
> yourself in [Stage 0](#stage-0--reproduce-a-scored-dataset-before-writing-a-line). Confirm the
> `bench_version` in every report before you believe a number.

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

Up to three validators score you per wave; the published figure is a `continual_mean` over
retained waves ([§2.5](#25-dethroning-and-emissions)). What matters is your median and your
floor — and because confirmation is a **paired** re-score with SE ≈ 0.017, your floor matters more
than your median.

```
0.81, 0.80, 0.79, 0.80, 0.78     ← submission-ready
0.85, 0.76, 0.43, 0.82, 0.51     ← not, despite the higher max
```

Vary seeds to measure **dataset robustness**; repeat a seed to measure **your own variance**.
A practical release test: ~20 small runs on different seeds, ~10 medium, 5–10 full, plus three
repeats of one seed.

**"If inference cost permits" is now answerable, and the answer is that it always permits.**
`[live]` The leaderboard publishes `average_run_cost_microusd` per entry; across the current top
five it ranges 192,866–417,574, i.e. **$0.19–$0.42 of inference per full run**. Ten full practice
runs is a few dollars, not a budget decision. This inverts the usual advice in
[§8](#8-environment-setup): the "free" local Ollama path trades days of wall clock to save a
handful of dollars. Use the hosted key and run more seeds.

Track median, minimum, P10, standard deviation, failure rate.

**Recommendation:** do not submit because one local run cleared the champion. Derive the target
from the live field, not from a rule of thumb:

```
target  =  emissions.champion_defense.required_score  +  1.28 × your own paired SE
```

`[live]` At the verification pass `required_score` was **0.7924** and the platform's paired SE was
0.0171, which puts a 90%-confidence target near **0.8125** — not the 0.80 an earlier revision of
this guide recommended, and not the 0.7879 that "leader + 0.007" gives. Aim there, across many
unseen full seeds, with a strong lower percentile — and remember the local tool scorer reads high
([§2.2](#22-tool-grading--on-chain-differs-from-local)) and that `evaluate`/`practice` are on v9
([§7.1](#71-the-ladder)).

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

**Recommendation — take this path, not the free one.** `[inferred]` On the measured per-run cost
in [§7.3](#73-distribution-not-a-point-estimate), a full 32-seed release sweep is single-digit
dollars. Running the 20B locally to avoid that trades 1.5–4.5 days of wall clock for roughly $5.
Use Ollama for `mem-eval` and unit work, where no chat model is needed at all, and put the hosted
key on everything that runs the agent loop.

Note also that one documented command hard-requires the key: `--longmem-eval`
([Appendix A](#appendix-a--command-reference)) will not run on the Ollama-only path.

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

**Registration, which this guide previously assumed you had already solved.** `[repo]` You need a
hotkey registered on netuid 118 before any of the above. `upload` will now do it for you with
`--register` (and `--no-register` restores the old failing pre-check), which changes the flow
above — read the prompts rather than assuming the steps. Two things to budget for that
[§12](#12-economics-and-gono-go)'s table understates: registration TAO is **burned and
non-refundable** even if you never submit or you score zero, and it is **usually larger than the
0.04 TAO evaluation fee** the table foregrounds. The live cost is dynamic and was
**`[unverified]`** at the verification pass — quote it from the CLI before you commit.

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
| **Developing against `evaluate`/`practice`** | They generate **v9**. Every v12 lever you are scored on is absent. §7.1. |
| **Writing outside `/tmp`** | Root filesystem is read-only at scoring time. Stage 10. |
| **Default request body limits** | A full `/seed` is large; the kit sets 256 MB. A 413 costs the whole memory half. §1.1. |
| **Binding `127.0.0.1`** | Unreachable from outside the container; fails the 10 s `/health` gate. §1.1. |
| **Greenfield without reading `baseline.rs`** | The model relay and embedder contracts are documented nowhere else. §0.1. |
| **Quoting `composite` instead of `official_composite`** | Two different numbers per entry; only one ranks and pays. §2.5. |
| **Treating memory-tool errors as no-ops** | Write-then-read `LifecycleCases` need the write to land. §1.2. |
| **Retrieving on every turn** | Greetings leak stored facts and fail the conversational-sanity factor. §2.3. |

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

**Auditability cuts both ways — in design.** Scores, signatures and each run's graded transcript
are published so anyone can regenerate the dataset from the published seed, re-run the public
grader over the transcript, and check the numbers match the signed composite. Your work is
checkable — and so is everyone else's.

> **In practice, the public half of that is not currently serving.** `[live]` The score ledger
> returns `401` to a miner, and the bench config's `public_transcript_url_template` and
> `public_mirror_url_template` are both `null`. So you can regenerate any dataset you like
> ([Stage 0](#stage-0--reproduce-a-scored-dataset-before-writing-a-line)), but you cannot
> currently fetch a scored run's seed or transcript to check anyone's number — including your
> own. Re-check those two fields before relying on the audit path.

---

## 12. Economics and go/no-go

> **Every figure below moves.** They are stamped, not stable. Re-read the leaderboard and the
> chain before you act on any of them; the ones in this table were 3.2–3.6% off within 24 hours
> of being written the first time.

| Metric | Value | Read |
|---|---|---|
| Rank-1 share of *miner* emission | **65%** | `[live]` `rank_shares: [0.65, 0.14, 0.10, 0.07, 0.04]` |
| **Miner share of subnet emission** | **41%** | dTAO split: 41% miners / 41% validators+stakers / 18% owner |
| Subnet alpha emission | 1.000 α/block | `[live]` `alpha_out_emission` |
| Subnet TAO inflow | ~2,337,530 rao/block | `[live]` `tao_in_emission`; ~0.233% of network |
| Alpha spot | ~0.0095 TAO | `[live]` `tao_in_emission / alpha_in_emission` |
| Paid slots | **5**, by construction | not a measure of competition — see below |
| Emission-eligible v12 competitors | **40** | `[live]` all finalized, all registered |
| Validators | 11 | `[live]` |
| Evaluation fee | 0.04 TAO (40,000,000 rao) | operator-configurable; the CLI shows the live figure |
| Registration | dynamic; burned, non-refundable, **usually > the eval fee** | see [§9.3](#93-submit) |
| Inference cost | **$0.19–$0.42 per full run** | `[live]` `average_run_cost_microusd` |
| Champion `official_composite` | 0.780856 (`aceron_v13`) | `[live]` |
| **Dethrone target** | **0.7924** | `[live]` `champion_defense.required_score` |
| Fifth place | 0.733684 (`lets_v610`) | `[live]` — the paid-slot floor |

### 12.1 What a rewarded position actually pays

`[inferred]` from the live chain figures above. The 41% miner share is the step an earlier
revision of this guide omitted. Without it the obvious calculation is
`0.00233753 τ/block × 7200 × 0.65 = 11.29 τ/day` — wrong twice over: it drops the miner share, and
it works in the TAO-inflow denomination rather than the alpha miners are actually paid in.

```
7,200 blocks/day × 1.000 α/block × 0.41 miner share  =  2,952 α/day to miners

  rank 1   × 0.65  =  1,918.8 α/day  ≈  18.23 τ/day     (at ~0.0095 τ/α)
  rank 2   × 0.14  =    413.3 α/day  ≈   3.93 τ/day
  rank 5   × 0.04  =    118.1 α/day  ≈   1.12 τ/day
  below 5th        =      0
```

Two caveats that matter more than the precision. This is **mark-to-market in alpha**, not TAO
received: the subnet absorbs materially less real TAO per day than it issues alpha, so the exit
price is not the spot price at any size. And the 65/14/10/7/4 curve is brutally convex — **rank 1
earns 16× rank 5.**

### 12.2 Break-even

The guide previously told you to "price the expected return" and then gave you no prices. Here is
the arithmetic; substitute your own rates.

**Cost.** Scoping [§5](#5-build-sequence)'s stages honestly — S0 1d, S1 2d, S2 3d, S3 5d, S4 4d,
S5 2d, S6 6d, S7 3d, S8 3d, S9 2d, S10 2d — is **33 engineer-days as a floor**, before
[§7.4](#74-build-your-own-adversarial-bank)'s adversarial bank, [§7.5](#75-two-distribution-shifts-you-must-plan-for)'s
MLP retrain for the Perplexity embedding space, and [Appendix B](#appendix-b--metrics-dashboard)'s
nine-rung ablation ladder on held-out seeds. Call it **50 days** realistically. Cash costs are
negligible against that: ~20 uploads × 0.04 TAO, registration, and single-digit dollars of
inference.

**Revenue horizon.** This is the number that decides it, and it is the one you cannot get.
`[live]` `available_bench_versions` lists **eleven** prior pools (v2–v12), and a benchmark rollover
**archives the scoring pool** — the clause [§12](#12-economics-and-gono-go) previously buried in a
subordinate sentence. `[unverified]` No source gives v12's activation date or expected lifetime
(see [§0.5](#05-the-board-reset-because-an-exploit-was-closed-not-because-the-problem-is-unsolved)),
so you are underwriting a 50-day build against a pool of unknown and plausibly shorter duration.

**The conclusion follows mechanically.** Over any short pool, rank 5 at ~1.12 τ/day does not repay
a 50-day build under any reasonable day rate. Rank 1 at ~18.23 τ/day does. **This is a
rank-1-or-nothing bet**, and it should be priced as one.

### 12.3 The honest read

**"Only five active miners" is not a measure of competition, and reading it as one inverts the
decision.** `[live]` The chain reports `active_miners = 5`, and that is real — but SN118 pays
65/14/10/7/4 to exactly five slots and nothing below, so the count of UIDs carrying non-zero
incentive is **pinned at 5 by construction**, no matter how many people are competing. The actual
field is the **40 finalized, registered, emission-eligible v12 entries**, of which **35 currently
earn nothing** while paying evaluation fees. The board is roughly eight times more crowded than
that number suggests.

The incumbents are also moving. Between two consecutive daily reads the board gained entries, a
miner absent from every prior snapshot (`kaelith`) took rank 4, and the previous fifth place was
pushed out of the paid set — while itself improving. You are not aiming at a stationary target.

Against that: the paid-slot floor is 0.7337 and the crown is 0.7924, on a benchmark whose ceiling
is 1.0 and whose champion still leaves 0.1365 of memory headroom
([§2.4](#24-what-is-deliberately-not-scored)). The problem is genuinely unfinished. Just price the
bet as rank-1-or-nothing, over a pool of unknown lifetime, before committing weeks — not after.

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

### Dataset generation (monorepo root) — build it first, it is Go

```bash
cd research/dittobench-datagen && go build ./cmd/generate    # Go 1.23+, stdlib only
./generate -bench-version 12 -seed 123456789 -run-size full -sha
./generate -bench-version 12 -run-size small                 # prints a fresh seed on stderr
```

Do **not** `go install` from the archived standalone repo — it has no v12 support. See
[Stage 0](#stage-0--reproduce-a-scored-dataset-before-writing-a-line).

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

The four fields worth pulling out of the leaderboard every time:

```bash
L=https://dittobench.ai/api/v1/public/leaderboard

# the number you must beat — do not compute it yourself
curl -s $L | jq '.emissions.champion_defense
                 | {required_score, required_lead, paired_standard_error, shared_seed_count}'

# the paid-slot floor, and the two-numbers-per-entry trap
curl -s $L | jq '.entries[:6] | map({rank, agent_name,
                 official_composite, composite, tool_mean, memory_mean})'

# how crowded it actually is, and what a full run costs to practise
curl -s $L | jq '{count, eligible: [.entries[]|select(.emission_eligible)]|length,
                  cost_usd: ([.entries[:5][].average_run_cost_microusd]|add/5/1e6)}'

# is the token-efficiency factor live yet?
curl -s $L | jq '.efficiency | {active, minimum_factor, maximum_factor, bonus_cap}'
```

The score ledger (`/api/v1/scoring/scores`, named in the bench config's `ledger_path`) is
validator-authenticated and returns `401` to a miner. Do not build a workflow on it.

---

## Appendix B — metrics dashboard

Track every one of these per release candidate, across many seeds.

| Metric | Target |
|---|---:|
| Median composite (full runs) | **`required_score` + 1.28 × your paired SE** (~0.8125 at verification) |
| P10 composite | above the incumbent's `official_composite` |
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
| Conversational-sanity factor | **1.0** |
| Tool-name F1 / **argument F1** | tracked **separately**, never as one number |
| Program stability across paraphrases | **100%** (compile-time check, no scoring run needed) |
| p95 `/run` latency | well under 60 s — the leading indicator for the whole-run deadline |

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
5. **The drift runs both ways, and the code is ahead of the prose.** `[repo]` The kit's
   `protocol.rs` sets `MAX_SUPPORTED_BENCH_VERSION = 12`, so the harness *accepts* v12 requests
   while the same file pins `evaluate`/`practice` generation at **v9** and the READMEs still say
   v8. So "the repo is behind" is too simple: read the constants, not the prose, and check what
   each command actually emits. See [§7.1](#71-the-ladder).

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

Re-verified live on 2026-09-02 at 21:55–21:57 UTC:

- `platform-api.heyditto.ai/api/v1/public/bench/config` — 200
- `dittobench.ai/api/v1/public/leaderboard` — 200, `count: 40`
- `platform-api.heyditto.ai/api/v1/scoring/scores` — **401**, validator-authenticated
- all 29 arXiv identifiers underlying the research synthesis this guide derives from — resolved,
  none fabricated

**Note on netuid 118's history:** the netuid registered 2025-06-06 as *HODL*, the mobiusfund ETF
subnet, and was rebranded to Ditto around April 2026 (`[unverified]` — the registration date is
confirmed on-chain and the mobiusfund provenance is confirmed, but no reachable source states a
rebrand date; it is inferred from repository timestamps). Its on-chain identity today is
`name: "Ditto"`, `description: "Open-Source Claude Cowork"`, owner contact `peyton@omniaura.ai`
(Omni Aura). Nothing about the netuid's 2025 history belongs to Ditto. Third-party directories are
still split — some aggregators serve stale "SN118 — HODL ETF" titles over Ditto content, so treat
any aggregator page for this netuid with suspicion.

---

## Appendix E — what the verification pass changed

Recorded so that a reader who saw an earlier revision knows which of their conclusions to discard.
Ordered by cost of having believed the old version.

| Was | Now | Basis |
|---|---|---|
| "Exactly two holes" in the egress boundary | **Three** — the embedding gateway over `OLLAMA_BASE_URL`, in Ollama wire format | `[repo]` |
| Model relay mentioned, never specified | Full contract: `DITTOBENCH_PROVIDER=platform`, `DITTOBENCH_INFERENCE_BASE_URL`, OpenAI-compatible, `Bearer ticket` | `[repo]` |
| Stage 0: "pull a published seed from the score ledger" | Ledger is `401`; build the Go generator and use seed `123456789` | `[live]` |
| "writable database path, non-root user" | Read-only rootfs, `/tmp` tmpfs only, uid 65532 | `[repo]` |
| Appendix B target: leader **+ 0.007** = 0.7879 | Live `required_score` **0.7924**, + 1.28 × paired SE ⇒ ~0.8125 | `[live]` |
| "Three integrity multipliers" | **Four**, plus a switched-off fifth whose max is **1.1** | `[live]` |
| Median of up to three validators is finalized | `continual_mean` over up to 32 retained waves | `[live]` |
| 37 scored miners; 5th = 0.713743 (`Hogwarts_v5`) | **40**; 5th = **0.733684** (`lets_v610`); `kaelith` at rank 4 | `[live]` |
| "Only five active miners — not a crowded board" | 5 is the number of **paid slots**; 40 agents contest them | `[live]` |
| §12 gives chain figures, no revenue or break-even | 41% miner share added; rank 1 ≈ 18.23 τ/day, rank 5 ≈ 1.12 τ/day; rank-1-or-nothing | `[inferred]` |
| §12 figures to 7 significant digits | All were 3.2–3.6% high within a day; precision reduced and marked | `[live]` |
| `evaluate` / `practice` presented as the dev loop | They generate **v9** — blind to every v12 lever in §6 | `[repo]` |
| Memory-tool errors: "expected behaviour" | Also: memory tools are **yours to implement**; `LifecycleCases` need the write to land | `[repo]` |
| Conversational sanity, `n: 351`, whole-run deadline | Absent entirely; now stated (the deadline value remains `[unverified]`) | `[live]` |
| "Free" Ollama path recommended for testing | A full run costs $0.19–$0.42; use the hosted key | `[live]` |
| 20 MiB framed as the binding constraint | 5.32 MiB used of 20; the runtime box is the real envelope | `[repo]` |
| 29 `[repo]` markers, 3 Recommendations, ~700 lines unmarked | Four-state provenance convention, applied | — |

Two things the pass **confirmed** that are easy to doubt: every `[repo]`/`[repo/api]` quotation
spot-checked appears verbatim at its cited path, and the whole bench-config block is
character-for-character correct. And §2.5's subtle claim — that paid slots follow the settling
mean rather than the raw composite — is live-confirmed: the `raw_rank` field currently reads
`1, 4, 5, 2, 3` against payout shares `0.65, 0.14, 0.10, 0.07, 0.04`. The two orders disagree
right now.
