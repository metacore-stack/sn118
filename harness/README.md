# ditto-p3 — an SN118 / DittoBench agent-memory harness

A greenfield harness for [SN118](https://github.com/ditto-assistant/ditto-subnet), built to the
architecture in [`../SN118-HARNESS-GUIDE.md`](../SN118-HARNESS-GUIDE.md): one immutable event
ledger, several rebuildable projections over it, a typed program compiled per request, and no
answer or tool call that cannot be traced back to retrieved evidence.

**Python 3.12, standard library only.** No pip install, no wheels, no model weights, no network
at build time. The whole build context is a few hundred KB against a 20 MiB cap, and `docker
build` cannot fail on a dependency fetch because there are none.

## Why stdlib-only is a design choice, not a limitation

| Need | Stdlib answer |
|---|---|
| BM25 lexical retrieval | SQLite **FTS5** `unicode61` |
| Exact codes, canaries, typos | SQLite FTS5 **`trigram`** tokenizer — character n-grams |
| Event ledger, temporal, graph | SQLite tables + WAL |
| Money arithmetic | `decimal.Decimal`, integer minor units — never float |
| Model relay | `urllib.request`, OpenAI-compatible |
| Embedding gateway | `urllib.request`, Ollama `/api/embed` wire format |
| Concurrency | `ThreadingHTTPServer`, thread-local connections |

## Layout

```
app/protocol.py   wire types; strict parse, opaque-ID discipline
app/store.py      immutable ledger + projections; per-user physical isolation
app/retrieve.py   parallel retrieval -> RRF -> evidence portfolio
app/sanity.py     turn classifier (greeting / declarative / behaviour-change / question)
app/llm.py        model relay + embedding gateway clients
app/compile.py    constrained query compiler with semantic-key cache
app/execute.py    deterministic executor (money, dates, latest-value, sets, graph)
app/verify.py     derivation DAG + calibrated abstention gate
app/tools.py      observed tool loop, argument canonicaliser
app/server.py     /health /seed /run
```

## Run

```bash
python3 -m app.server                 # serves 0.0.0.0:8080
DITTOBENCH_DB=/tmp/x.db python3 -m app.server
python3 -m tests.run                  # invariant suite; no model or network needed
```

## The score is `raw x gate`, and the gate is where the points are

For bench_version >= 7 the published composite is not the mean of the two
halves. It is that mean multiplied by a gate of bounded factors
(`services/dittobench-api/internal/scorer/v7.go`):

```
raw       = 0.5 * tool_mean + 0.5 * memory_mean
gate      = max(0.40, toolEfficiency * metamorphic * memoryOverCall)
            * canaryIntegrity * conversationalSanity * transformAudit
composite = raw * gate
```

Three of those factors are entirely about harness discipline, not accuracy:

| factor | cost | what triggers it |
|---|---|---|
| memory over-call | up to 25% | any non-memory tool observed on a memory question -- as a FRACTION of observed memory cases, so one stray call can be the full penalty |
| conversational sanity | floor 0.25 | geometric mean of greeting / declarative-ack / behavior slice pass rates |
| canary integrity | x0.25 | surfacing another party's planted nonce, even once |

On this harness the gate went 0.505 -> 0.835 across four fixes while raw
accuracy stayed flat at ~0.43 -- worth more than any single memory family.
`STATUS.md` has the per-seed decomposition; `bench.sh` output plus the per-case
report is enough to recompute every factor yourself.

## Score it with the real grader

`local-rehearsal.py` in ditto-subnet drives the real generator, grader, staged
seeding and a validator-visible `tool_endpoint` -- but it builds and launches
the Rust starter kit first, so it hard-requires `cargo` and can only score that
harness. One patch removes that coupling:

```bash
python3 scripts/enable_external_harness.py ~/ditto-subnet   # adds --harness-url
DITTO_REPO=~/ditto-subnet ./scripts/bench.sh 16 small       # 16 seeds, mean +/- stderr
```

`bench.sh` gives each seed a fresh database and reports a standard error,
because single runs of identical code vary by ~0.02 composite -- enough to make
a change look like an improvement when it is noise. Needs Go (for the scorer)
and, for the dense projection, an Ollama-compatible embedder on
`OLLAMA_BASE_URL`.

## Status

See [`STATUS.md`](STATUS.md) for what is built, what is stubbed, and what is untested.

## Tool routing

`app/tools.py` routes by intent, most specific first, and stays silent by
default: the stored-data gate answers anything about the user's own records
from memory and calls nothing external (the v12 memory over-call gate
multiplies the whole composite). Named capabilities sit in front of that gate
where they must -- calendar, schedules, memory read chains, image editing,
one-off calculations, tool discovery -- and explicit opt-outs ("don't search
the web for this") win over everything. One decision is state-dependent:
"start it the way we agreed" lists the saved workflows first and then either
runs the one named after the project or dispatches a one-off agent job. The
tool loop makes that choice after the listing; nothing in memory can
authorise a consequential call, which still needs an imperative in the
current turn.

## Identity-bearing tool arguments

The validator hands the harness opaque ids (case, user, pair, subject) and
internalises every tool call it observes or that the harness reports. An
argument named `pair_id`, `pairIds`, `subject_id`, `session_id`, `case_id` or
`user_id` must carry one of those ids, byte-exact; anything else makes the
whole case an `invalid_v9_identity_capability` -- scored zero, and counted as a
non-memory call on a memory case. So such arguments are never derived from
the request: they are echoed from a previous hop's observation (`[pair <id>]`,
`[subject <id>]`) or from the retrieved record the request is about, and a
memory tool is posted to the endpoint only when the store can vouch for every
id in its arguments. It is served locally regardless.

## Web answers

When a search ran, the answer carries the figure the result states for the
entity the question names -- the result deliberately also states a figure for
a similarly named distractor -- rather than anything from memory.

## Where it stands

On 8 held-out seeds under the real v12 scorer (`scripts/bench.sh 8 small`):
composite 0.907 +/- 0.020, tool 1.00, memory 0.88, gate 0.97 (seeds 0.84-0.95). The progression
and every finding behind it are in `STATUS.md`.
