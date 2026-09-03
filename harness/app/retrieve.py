"""Parallel retrieval over the projections, fused with RRF.

Four retrievers run over the same ledger and disagree usefully:

  word   BM25 over `unicode61` -- names, phrases, paraphrase-adjacent wording
  gram   BM25 over character trigrams -- random codes, canaries, typos, partial
         recall. This is the projection that carries the canary family, and
         without it a semantic-only harness eats the ×0.85 integrity factor.
  vec    cosine over embeddings, when the embedding gateway is reachable
  graph  subject/mention traversal -- relational binding when the question
         never names the subject alias, which v12 made universal

Fusion is Reciprocal Rank Fusion. RRF is used deliberately over score-blending:
BM25 scores and cosine similarities are not commensurable, and any fixed
weighting between them is a calibration that goes stale the moment the embedder
changes -- which §7.5 says it does, between local practice and the validator
gateway. RRF consumes *ranks*, so it is invariant to that.

The output is a small diverse evidence portfolio, not a giant transcript:
'Lost in the Middle' is a real effect and the 60 s budget is real too.
"""

from __future__ import annotations

import math
import re
import sqlite3
import struct
from dataclasses import dataclass, field

from .store import Event, Store, normalise

# RRF constant. 60 is the value from Cormack et al. and is not tuned here:
# tuning it against local practice would be fitting a v9 dataset (§7.1).
RRF_K = 60

# How deep each retriever goes before fusion. Deep enough that a needle ranked
# poorly by one projection can still be rescued by another.
POOL = 60


# --------------------------------------------------------------------------
# query preparation
# --------------------------------------------------------------------------

_WORD = re.compile(r"[0-9A-Za-z_]+", re.UNICODE)
# A token worth sending to the trigram index: mixed case/digits, or hyphenated
# alphanumerics -- the shape of VK-7Q2M, AX-991, invoice numbers, SKUs.
_CODEY = re.compile(r"^(?=.*\d)[A-Za-z0-9][A-Za-z0-9-]{2,}$")

_STOP = frozenset("""
a an the and or but if then than that this these those of in on at to from by for with without
is are was were be been being do does did doing have has had having i me my we our you your he
him his she her it its they them their what which who whom when where why how all any both each
few more most other some such no nor not only own same so too very can will just should now
about into over under again further once here there
""".split())


def query_terms(q: str) -> list[str]:
    return [t for t in _WORD.findall(q or "") if normalise(t) not in _STOP and len(t) > 1]


def codey_terms(q: str) -> list[str]:
    """Tokens that look like identifiers, plus any hyphenated run.

    Split on whitespace rather than \\w so `VK-7Q2M` survives as one token.
    """
    out: list[str] = []
    for raw in re.split(r"[\s,;:.!?()\[\]{}\"']+", q or ""):
        t = raw.strip()
        if len(t) >= 3 and _CODEY.match(t):
            out.append(t)
    return out


def _fts_or(terms: list[str]) -> str:
    """Build a safe FTS5 OR query. Every term is double-quoted, so FTS5
    operators inside user text (NEAR, *, ^, -) are literals, not syntax."""
    esc = [t.replace('"', '""') for t in terms if t]
    return " OR ".join(f'"{e}"' for e in esc)


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Hit:
    event_id: int
    rank: int
    score: float
    source: str


@dataclass(slots=True)
class Candidate:
    event_id: int
    rrf: float
    sources: dict[str, int] = field(default_factory=dict)   # source -> rank
    event: Event | None = None

    @property
    def why(self) -> str:
        return ",".join(f"{s}#{r}" for s, r in sorted(self.sources.items()))


# --------------------------------------------------------------------------
# individual retrievers
# --------------------------------------------------------------------------

def _fts_search(conn: sqlite3.Connection, table: str, user: str,
                match: str, limit: int, source: str) -> list[Hit]:
    if not match:
        return []
    try:
        rows = conn.execute(
            f"SELECT f.rowid, bm25({table}) AS s FROM {table} f"
            f" JOIN events e ON e.event_id = f.rowid"
            f" WHERE {table} MATCH ? AND e.user_id = ? AND e.tombstone = 0"
            f" ORDER BY s LIMIT ?",
            (match, user, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        # A malformed MATCH must never take down a case. Degrade to no hits
        # from this projection; the others still fire.
        return []
    # bm25() returns a negative score, better = more negative.
    return [Hit(r[0], i, -float(r[1]), source) for i, r in enumerate(rows)]


def search_word(store: Store, user: str, query: str, limit: int = POOL) -> list[Hit]:
    return _fts_search(store._conn(), "px_word", user,
                       _fts_or(query_terms(query)), limit, "word")


def search_gram(store: Store, user: str, query: str, limit: int = POOL) -> list[Hit]:
    """Trigram search, biased to identifier-shaped tokens.

    Falls back to the rarest few natural words when the query carries no codey
    token, which is what makes this projection also useful for typo tolerance.
    """
    terms = codey_terms(query)
    if not terms:
        terms = [t for t in query_terms(query) if len(t) >= 4][:4]
    return _fts_search(store._conn(), "px_gram", user, _fts_or(terms), limit, "gram")


def search_graph(store: Store, user: str, query: str, limit: int = POOL) -> list[Hit]:
    """Entity/subject traversal.

    Two paths, both cheap: exact mention hits on surfaces we have seen, and
    subject expansion where a matched subject drags in its linked pairs. This is
    what answers 'the workstream that carries a settled payment' when the
    question never names the alias.
    """
    conn = store._conn()
    qn = normalise(query)
    scored: dict[int, float] = {}

    for surface, norm in store.known_mentions(user):
        if not norm or len(norm) < 3:
            continue
        if norm in qn:
            # Longer surfaces are more specific, so weight by length.
            w = 1.0 + math.log1p(len(norm))
            for eid in store.events_mentioning(user, surface, limit=limit):
                scored[eid] = scored.get(eid, 0.0) + w

    for sid, stext, desc, _derived in store.all_subjects(user):
        blob = normalise(f"{stext} {desc}")
        if not blob:
            continue
        overlap = sum(1 for t in query_terms(query) if normalise(t) in blob)
        if overlap == 0:
            continue
        for pid in store.pairs_for_subject(user, sid):
            for r in conn.execute(
                "SELECT event_id FROM events WHERE user_id=? AND pair_id=? AND tombstone=0",
                (user, pid),
            ).fetchall():
                scored[r[0]] = scored.get(r[0], 0.0) + 0.5 * overlap

    order = sorted(scored.items(), key=lambda kv: -kv[1])[:limit]
    return [Hit(eid, i, s, "graph") for i, (eid, s) in enumerate(order)]


def _unpack(blob: bytes, dim: int) -> tuple[float, ...]:
    return struct.unpack(f"<{dim}f", blob)


def search_vec(store: Store, user: str, qvec: list[float], limit: int = POOL) -> list[Hit]:
    """Cosine over stored embeddings. No-op when nothing is vectorised."""
    if not qvec:
        return []
    qn = math.sqrt(sum(x * x for x in qvec)) or 1.0
    out: list[tuple[int, float]] = []
    for eid, dim, norm, blob in store.vectors(user):
        if dim != len(qvec):
            continue
        v = _unpack(blob, dim)
        dot = 0.0
        for a, b in zip(qvec, v):
            dot += a * b
        out.append((eid, dot / (qn * (norm or 1.0))))
    out.sort(key=lambda kv: -kv[1])
    return [Hit(eid, i, s, "vec") for i, (eid, s) in enumerate(out[:limit])]


# --------------------------------------------------------------------------
# fusion
# --------------------------------------------------------------------------

def rrf(runs: list[list[Hit]], k: int = RRF_K) -> list[Candidate]:
    """Reciprocal Rank Fusion over several ranked runs.

    score(d) = Σ_runs 1 / (k + rank(d))    -- rank is 0-based here.
    """
    acc: dict[int, Candidate] = {}
    for run in runs:
        for h in run:
            c = acc.get(h.event_id)
            if c is None:
                c = acc[h.event_id] = Candidate(h.event_id, 0.0)
            c.rrf += 1.0 / (k + h.rank + 1)
            # Keep the best rank seen from each source.
            prev = c.sources.get(h.source)
            if prev is None or h.rank < prev:
                c.sources[h.source] = h.rank
    return sorted(acc.values(), key=lambda c: -c.rrf)


def diversify(store: Store, user: str, cands: list[Candidate],
              limit: int) -> list[Candidate]:
    """Trim to a small, *diverse* portfolio.

    Near-duplicate evidence wastes the context budget and pushes the actual
    needle down the prompt. Two cheap rules: at most two events per pair (the
    user turn and its reply), and skip an event whose normalised text we have
    already taken.
    """
    seen_text: set[str] = set()
    per_pair: dict[str, int] = {}
    out: list[Candidate] = []
    ids = [c.event_id for c in cands]
    evs = {e.event_id: e for e in store.events(user, ids)}
    for c in cands:
        e = evs.get(c.event_id)
        if e is None:
            continue
        key = normalise(e.text)[:160]
        if key in seen_text:
            continue
        if per_pair.get(e.pair_id, 0) >= 2:
            continue
        c.event = e
        seen_text.add(key)
        per_pair[e.pair_id] = per_pair.get(e.pair_id, 0) + 1
        out.append(c)
        if len(out) >= limit:
            break
    return out


def search_recent(store: Store, user: str, limit: int = POOL) -> list[Hit]:
    """Most-recent events. Not a semantic retriever -- a backstop.

    Recency is a genuine prior in a conversation log, and it is the only signal
    that still works when every lexical projection whiffs.
    """
    rows = store._conn().execute(
        "SELECT event_id FROM events WHERE user_id=? AND tombstone=0"
        " ORDER BY COALESCE(ts_epoch, 0) DESC, wave DESC, seq DESC, event_id DESC"
        " LIMIT ?", (user, limit)).fetchall()
    return [Hit(r[0], i, 1.0 / (i + 1), "recent") for i, r in enumerate(rows)]


def retrieve(store: Store, user: str, query: str, *, qvec: list[float] | None = None,
             limit: int = 12, pool: int = POOL) -> list[Candidate]:
    """The full retrieval path: four projections in, one portfolio out.

    Every retriever is scoped to `user` in its own SQL. There is no global
    retrieval step anywhere in this function -- cross-user isolation is a
    property of the query, not of a filter applied afterwards.
    """
    runs = [
        search_word(store, user, query, pool),
        search_gram(store, user, query, pool),
        search_graph(store, user, query, pool),
    ]
    if qvec:
        runs.append(search_vec(store, user, qvec, pool))
    fused = diversify(store, user, rrf(runs), limit)

    # Backstop. A purely lexical stack cannot bridge "which city do I live in"
    # to "I moved to Lisbon" -- that is the dense projection's job, and the
    # embedding gateway is not always reachable. Returning an empty portfolio
    # would push the verifier straight to abstain, and abstaining on an
    # answerable case scores exactly the same as answering wrong. So top up
    # with recency rather than hand back nothing.
    #
    # Appended after fusion, never fused: a backstop must not outrank a real
    # lexical or graph hit.
    if len(fused) < limit:
        have = {c.event_id for c in fused}
        extra = [h for h in search_recent(store, user, pool) if h.event_id not in have]
        if extra:
            topped = diversify(store, user, [Candidate(h.event_id, 0.0, {"recent": h.rank})
                                             for h in extra], limit - len(fused))
            fused.extend(topped)
    return fused


def recall_at_k(store: Store, user: str, query: str, needle_event_ids: set[int],
                k: int = 10, qvec: list[float] | None = None) -> bool:
    """Did the needle survive into the top k? The `mem-eval` primitive.

    Measure this per question family, never in aggregate -- a high overall
    recall@10 hides the one family sitting at zero.
    """
    got = retrieve(store, user, query, qvec=qvec, limit=k)
    return any(c.event_id in needle_event_ids for c in got)
