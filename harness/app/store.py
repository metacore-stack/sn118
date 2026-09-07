"""Immutable event ledger and the projections rebuilt from it.

Design, per the guide's §4:

    One immutable event history; several rebuildable projections over it.

The ledger is append-only. A correction never overwrites a fact -- it appends a
new event that supersedes the old one, so we can answer both "what is true now"
and "what was true then". Deletions leave tombstones. That is what makes a
repeated `/seed` naturally idempotent and makes point-in-time questions
answerable at all.

Isolation is physical and enforced *in the query*, never by asking a model to
ignore rows afterwards. Every table is keyed by `user_id` and every read path
takes it as a bound parameter. One cross-user leak is the ×0.50 canary cliff.

Concurrency (the guide's Stage 9, the maintainers' own most expensive mistake):
connections are thread-local, WAL is on, and **no transaction is ever held
across a network call**. Everything in this module is pure local I/O; embedding
and model round-trips happen in the caller, outside any transaction we open.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .protocol import MemoryPair, SeedRequest, Subject, SubjectLink, epoch_seconds

DEFAULT_DB = "/tmp/dittobench.db"

# Roles a ledger event can carry. The assistant-recall family plants the answer
# *only* in a reply, so indexing user turns alone forfeits a whole family.
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

# Origin of an event. Seeded pairs are the haystack; `tool` events are writes
# the harness itself made through its own memory tools, which the write-then-read
# LifecycleCases require to actually land.
ORIGIN_SEED = "seed"
ORIGIN_TOOL = "tool"


_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;
PRAGMA busy_timeout=15000;

-- ---------------------------------------------------------------- ledger --
-- Append-only. Never UPDATE a row here except to set a tombstone or link a
-- supersession; the raw text is immutable once written.
CREATE TABLE IF NOT EXISTS events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    pair_id      TEXT NOT NULL,
    session_id   TEXT NOT NULL DEFAULT '',
    role         TEXT NOT NULL,
    text         TEXT NOT NULL,
    ts_raw       TEXT NOT NULL DEFAULT '',
    ts_epoch     REAL,
    wave         INTEGER NOT NULL DEFAULT 0,
    seq          INTEGER NOT NULL DEFAULT 0,
    origin       TEXT NOT NULL DEFAULT 'seed',
    content_hash TEXT NOT NULL,
    supersedes   INTEGER,
    tombstone    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, pair_id, role, origin)
);
CREATE INDEX IF NOT EXISTS ix_events_user      ON events(user_id, tombstone);
CREATE INDEX IF NOT EXISTS ix_events_user_time ON events(user_id, ts_epoch);
CREATE INDEX IF NOT EXISTS ix_events_pair      ON events(user_id, pair_id);
CREATE INDEX IF NOT EXISTS ix_events_session   ON events(user_id, session_id);

-- ------------------------------------------------------------ projections --
-- 2a. LEXICAL / word-level. BM25 over natural language: names, phrases.
CREATE VIRTUAL TABLE IF NOT EXISTS px_word USING fts5(
    text,
    tokenize='unicode61 remove_diacritics 2',
    content=''
);
-- 2a'. LEXICAL / character trigram. This is the canary and exact-code index:
-- embeddings represent random tokens like VK-7Q2M poorly, and word tokenisers
-- split them unhelpfully. Trigrams also absorb typos and partial recall.
CREATE VIRTUAL TABLE IF NOT EXISTS px_gram USING fts5(
    text,
    tokenize='trigram',
    content=''
);
-- rowid bridge: FTS5 external-content tables are keyed by rowid == event_id.

-- 2b. DENSE SEMANTIC. Vectors are optional -- the embedding gateway may be
-- absent locally. Stored as raw float32 bytes; cosine is computed in Python.
CREATE TABLE IF NOT EXISTS px_vec (
    event_id INTEGER PRIMARY KEY,
    user_id  TEXT NOT NULL,
    dim      INTEGER NOT NULL,
    norm     REAL NOT NULL,
    vec      BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_vec_user ON px_vec(user_id);

-- 2c. TEMPORAL STATE. Validity intervals over extracted claims. `valid_to`
-- NULL means "still true"; a correction closes the previous interval.
CREATE TABLE IF NOT EXISTS px_state (
    state_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    slot       TEXT NOT NULL,
    value      TEXT NOT NULL,
    event_id   INTEGER NOT NULL,
    valid_from REAL,
    valid_to   REAL,
    superseded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_state_slot ON px_state(user_id, slot, superseded);

-- 2d. ENTITY-EVENT GRAPH. Subjects are supplied in Tier A and *derived* in
-- Tier B; both land here so downstream code cannot tell the difference.
CREATE TABLE IF NOT EXISTS subjects (
    user_id      TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    subject_text TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    derived      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, subject_id)
);
CREATE TABLE IF NOT EXISTS subject_links (
    user_id    TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    pair_id    TEXT NOT NULL,
    derived    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, subject_id, pair_id)
);
CREATE INDEX IF NOT EXISTS ix_links_pair ON subject_links(user_id, pair_id);

-- Mentions: entity surface forms observed in event text, for relational
-- binding when the question never names the subject alias.
CREATE TABLE IF NOT EXISTS mentions (
    user_id  TEXT NOT NULL,
    surface  TEXT NOT NULL,
    norm     TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, norm, event_id)
);
CREATE INDEX IF NOT EXISTS ix_mentions_norm ON mentions(user_id, norm);

-- Wave bookkeeping, so staged seeding is an ordered idempotent upsert.
CREATE TABLE IF NOT EXISTS waves (
    user_id  TEXT NOT NULL,
    wave     INTEGER NOT NULL,
    seen_at  REAL NOT NULL,
    PRIMARY KEY (user_id, wave)
);
"""


def _hash(*parts: str) -> str:
    h = hashlib.blake2b(digest_size=16)
    for p in parts:
        h.update(p.encode("utf-8", "replace"))
        h.update(b"\x1f")
    return h.hexdigest()


# Surface forms worth indexing as mentions: capitalised words/phrases, and
# anything with the shape of a code or identifier.
_CAP_RUN = re.compile(r"\b([A-Z][\w''-]*(?:\s+[A-Z][\w''-]*){0,3})\b")
_CODEY = re.compile(r"\b(?=[A-Za-z0-9-]{4,})(?:[A-Z0-9]+-?){2,}\b")


def normalise(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().casefold()


# Stopwords that produce useless capitalised mentions at sentence start.
_MENTION_STOP = frozenset("""
a an and are as at be but by for from had has have he her him his i if in is it its me my
of on or our she that the their them there they this to was we were what when where which
who will with you your yes no ok okay hi hello hey thanks thank please just also so then
""".split())


def extract_mentions(text: str) -> set[str]:
    """Surface forms worth binding on. Deliberately cheap and recall-biased --
    precision comes from the retrieval fusion downstream, not from here."""
    out: set[str] = set()
    for m in _CAP_RUN.finditer(text or ""):
        s = m.group(1).strip()
        if len(s) < 2:
            continue
        if normalise(s) in _MENTION_STOP:
            continue
        out.add(s)
    for m in _CODEY.finditer(text or ""):
        s = m.group(0).strip()
        if len(s) >= 4 and any(c.isdigit() for c in s):
            out.add(s)
    return out


@dataclass(frozen=True, slots=True)
class Event:
    event_id: int
    user_id: str
    pair_id: str
    session_id: str
    role: str
    text: str
    ts_raw: str
    ts_epoch: float | None
    wave: int
    seq: int
    origin: str
    tombstone: int


def _row_to_event(r: sqlite3.Row) -> Event:
    return Event(
        event_id=r["event_id"], user_id=r["user_id"], pair_id=r["pair_id"],
        session_id=r["session_id"], role=r["role"], text=r["text"],
        ts_raw=r["ts_raw"], ts_epoch=r["ts_epoch"], wave=r["wave"],
        seq=r["seq"], origin=r["origin"], tombstone=r["tombstone"],
    )


class Store:
    """Thread-safe SQLite-backed ledger + projections.

    One connection per thread (SQLite connections are not shareable across
    threads safely). WAL lets readers proceed while a writer commits, which is
    what keeps concurrent `/run` cases from serialising behind a `/seed`.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.environ.get("DITTOBENCH_DB", DEFAULT_DB)
        if self.path != ":memory:":
            d = os.path.dirname(os.path.abspath(self.path))
            # The scored container's root filesystem is read-only; only /tmp is
            # writable. Fail loudly here rather than on the first /seed.
            os.makedirs(d, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._shared_mem_conn: sqlite3.Connection | None = None
        with self._conn() as c:
            c.executescript(_SCHEMA)

    # -- connection management ---------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            if self.path == ":memory:":
                # A per-thread :memory: db would give every thread its own empty
                # database. Share one connection instead; tests are single-threaded.
                if self._shared_mem_conn is None:
                    self._shared_mem_conn = sqlite3.connect(
                        ":memory:", check_same_thread=False)
                    self._shared_mem_conn.row_factory = sqlite3.Row
                c = self._shared_mem_conn
            else:
                c = sqlite3.connect(self.path, timeout=15.0, check_same_thread=False)
                c.row_factory = sqlite3.Row
                c.execute("PRAGMA busy_timeout=15000")
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None and c is not self._shared_mem_conn:
            c.close()
            self._local.conn = None

    # -- seeding -----------------------------------------------------------

    def seed(self, req: SeedRequest) -> dict[str, int]:
        """Ordered idempotent upsert of one wave.

        Returns the counts *loaded from this request*, so a repeated identical
        `/seed` returns identical counts and changes no stored state -- the
        Stage 1 gate.
        """
        user = req.user_id
        conn = self._conn()
        # The write lock is held only around local SQLite work. Nothing inside
        # it can block on the network.
        with self._write_lock:
            with conn:  # one transaction for the whole wave
                conn.execute(
                    "INSERT OR REPLACE INTO waves(user_id, wave, seen_at) VALUES (?,?,?)",
                    (user, req.wave, time.time()),
                )
                for i, p in enumerate(req.pairs):
                    self._upsert_pair(conn, user, p, req.wave, i)
                for s in req.subjects:
                    conn.execute(
                        "INSERT INTO subjects(user_id, subject_id, subject_text, description, derived)"
                        " VALUES (?,?,?,?,0)"
                        " ON CONFLICT(user_id, subject_id) DO UPDATE SET"
                        "   subject_text=excluded.subject_text,"
                        "   description=excluded.description, derived=0",
                        (user, s.id, s.subject_text, s.description_text),
                    )
                for l in req.links:
                    conn.execute(
                        "INSERT OR IGNORE INTO subject_links(user_id, subject_id, pair_id, derived)"
                        " VALUES (?,?,?,0)",
                        (user, l.subject_id, l.pair_id),
                    )
        return {"pairs": len(req.pairs), "subjects": len(req.subjects), "links": len(req.links)}

    def _upsert_pair(self, conn: sqlite3.Connection, user: str,
                     p: MemoryPair, wave: int, seq: int) -> None:
        ts_e = epoch_seconds(p.timestamp)
        for role, text in ((ROLE_USER, p.prompt), (ROLE_ASSISTANT, p.response)):
            if not text:
                continue
            ch = _hash(user, p.pair_id, role, text)
            cur = conn.execute(
                "SELECT event_id, content_hash, text FROM events"
                " WHERE user_id=? AND pair_id=? AND role=? AND origin=?",
                (user, p.pair_id, role, ORIGIN_SEED),
            )
            row = cur.fetchone()
            if row is not None:
                if row["content_hash"] == ch:
                    continue                      # byte-identical replay: no-op
                # Same opaque id, different text: the wave is correcting itself.
                # Re-point the projections rather than appending a duplicate,
                # because pair_id is the addressable key the validator reuses.
                eid = row["event_id"]
                conn.execute(
                    "UPDATE events SET text=?, ts_raw=?, ts_epoch=?, wave=?, seq=?,"
                    " session_id=?, content_hash=? WHERE event_id=?",
                    (text, p.timestamp, ts_e, wave, seq, p.session_id, ch, eid),
                )
                self._reindex(conn, eid, user, row["text"], text)
                continue
            cur = conn.execute(
                "INSERT INTO events(user_id, pair_id, session_id, role, text, ts_raw,"
                " ts_epoch, wave, seq, origin, content_hash)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (user, p.pair_id, p.session_id, role, text, p.timestamp, ts_e,
                 wave, seq, ORIGIN_SEED, ch),
            )
            self._index_new(conn, int(cur.lastrowid), user, text)

    # -- projection maintenance -------------------------------------------

    def _index_new(self, conn: sqlite3.Connection, eid: int, user: str, text: str) -> None:
        conn.execute("INSERT INTO px_word(rowid, text) VALUES (?,?)", (eid, text))
        conn.execute("INSERT INTO px_gram(rowid, text) VALUES (?,?)", (eid, text))
        self._index_mentions(conn, eid, user, text)

    def _reindex(self, conn: sqlite3.Connection, eid: int, user: str,
                 old_text: str, new_text: str) -> None:
        """Replace an event's index entries when a wave corrects itself.

        A contentless FTS5 table cannot look up the row it is deleting, so the
        'delete' command must be given the ORIGINAL text -- that is how it knows
        which postings to remove. Passing anything else (an empty string, the new
        text) silently leaves the old tokens in the index, and a corrected fact
        goes on matching its superseded value forever. That is precisely the
        knowledge-update family, so it would be an invisible, expensive bug.
        """
        for tbl in ("px_word", "px_gram"):
            conn.execute(f"INSERT INTO {tbl}({tbl}, rowid, text) VALUES ('delete', ?, ?)",
                         (eid, old_text))
        conn.execute("DELETE FROM mentions WHERE user_id=? AND event_id=?", (user, eid))
        self._index_new(conn, eid, user, new_text)

    def _index_mentions(self, conn: sqlite3.Connection, eid: int, user: str, text: str) -> None:
        for surf in extract_mentions(text):
            conn.execute(
                "INSERT OR IGNORE INTO mentions(user_id, surface, norm, event_id) VALUES (?,?,?,?)",
                (user, surf, normalise(surf), eid),
            )

    # -- harness-owned writes (LifecycleCases) -----------------------------

    def write_memory(self, user: str, text: str, *, pair_id: str = "",
                     ts_raw: str = "") -> int:
        """Append an event the harness itself authored via its memory tools.

        The write-then-read lifecycle family seeds an instruction in one wave and
        asks a question in a later one that is only answerable if this landed.
        """
        pid = pair_id or f"tool:{_hash(user, text, str(time.time()))[:16]}"
        ch = _hash(user, pid, ROLE_USER, text)
        conn = self._conn()
        with self._write_lock:
            with conn:
                cur = conn.execute(
                    "INSERT INTO events(user_id, pair_id, session_id, role, text, ts_raw,"
                    " ts_epoch, wave, seq, origin, content_hash)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(user_id, pair_id, role, origin) DO UPDATE SET text=excluded.text",
                    (user, pid, "", ROLE_USER, text, ts_raw, epoch_seconds(ts_raw) or time.time(),
                     10_000, 0, ORIGIN_TOOL, ch),
                )
                eid = int(cur.lastrowid)
                self._index_new(conn, eid, user, text)
        return eid

    def tombstone(self, user: str, event_ids: Iterable[int]) -> int:
        ids = [int(e) for e in event_ids]
        if not ids:
            return 0
        conn = self._conn()
        q = ",".join("?" * len(ids))
        with self._write_lock:
            with conn:
                cur = conn.execute(
                    f"UPDATE events SET tombstone=1 WHERE user_id=? AND event_id IN ({q})",
                    (user, *ids),
                )
        return cur.rowcount

    # -- reads (every one scoped by user_id in the query itself) -----------

    def event(self, user: str, event_id: int) -> Event | None:
        r = self._conn().execute(
            "SELECT * FROM events WHERE user_id=? AND event_id=?", (user, event_id)
        ).fetchone()
        return _row_to_event(r) if r else None

    def events(self, user: str, event_ids: Sequence[int],
               include_tombstoned: bool = False) -> list[Event]:
        if not event_ids:
            return []
        q = ",".join("?" * len(event_ids))
        tomb = "" if include_tombstoned else " AND tombstone=0"
        rows = self._conn().execute(
            f"SELECT * FROM events WHERE user_id=? AND event_id IN ({q}){tomb}",
            (user, *[int(e) for e in event_ids]),
        ).fetchall()
        by_id = {r["event_id"]: _row_to_event(r) for r in rows}
        return [by_id[i] for i in event_ids if i in by_id]

    def all_events(self, user: str) -> list[Event]:
        rows = self._conn().execute(
            "SELECT * FROM events WHERE user_id=? AND tombstone=0"
            " ORDER BY COALESCE(ts_epoch, 0), wave, seq, event_id", (user,)
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def pair_events(self, user: str, pair_id: str) -> list[Event]:
        rows = self._conn().execute(
            "SELECT * FROM events WHERE user_id=? AND pair_id=? AND tombstone=0"
            " ORDER BY role DESC", (user, pair_id)
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def counts(self, user: str) -> dict[str, int]:
        c = self._conn()
        one = lambda q, *a: int(c.execute(q, a).fetchone()[0])
        return {
            "events": one("SELECT COUNT(*) FROM events WHERE user_id=? AND tombstone=0", user),
            "pairs": one("SELECT COUNT(DISTINCT pair_id) FROM events WHERE user_id=?", user),
            "subjects": one("SELECT COUNT(*) FROM subjects WHERE user_id=?", user),
            "links": one("SELECT COUNT(*) FROM subject_links WHERE user_id=?", user),
            "mentions": one("SELECT COUNT(DISTINCT norm) FROM mentions WHERE user_id=?", user),
            "vectors": one("SELECT COUNT(*) FROM px_vec WHERE user_id=?", user),
        }

    def users(self) -> list[str]:
        return [r[0] for r in self._conn().execute(
            "SELECT DISTINCT user_id FROM events ORDER BY user_id").fetchall()]

    def waves_seen(self, user: str) -> list[int]:
        return [r[0] for r in self._conn().execute(
            "SELECT wave FROM waves WHERE user_id=? ORDER BY wave", (user,)).fetchall()]

    # -- subjects ----------------------------------------------------------

    def subjects_for_pair(self, user: str, pair_id: str) -> list[str]:
        return [r[0] for r in self._conn().execute(
            "SELECT subject_id FROM subject_links WHERE user_id=? AND pair_id=?",
            (user, pair_id)).fetchall()]

    def pairs_for_subject(self, user: str, subject_id: str) -> list[str]:
        return [r[0] for r in self._conn().execute(
            "SELECT pair_id FROM subject_links WHERE user_id=? AND subject_id=?",
            (user, subject_id)).fetchall()]

    def all_subjects(self, user: str) -> list[tuple[str, str, str, int]]:
        return [(r["subject_id"], r["subject_text"], r["description"], r["derived"])
                for r in self._conn().execute(
                    "SELECT * FROM subjects WHERE user_id=?", (user,)).fetchall()]

    def add_derived_subject(self, user: str, subject_id: str, text: str,
                            description: str, pair_ids: Iterable[str]) -> None:
        """Tier B: a subject we built ourselves from raw pairs.

        Marked `derived=1` so it never overwrites a supplied subject, but it is
        otherwise indistinguishable to every read path.
        """
        conn = self._conn()
        with self._write_lock:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO subjects(user_id, subject_id, subject_text,"
                    " description, derived) VALUES (?,?,?,?,1)",
                    (user, subject_id, text, description),
                )
                for pid in pair_ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO subject_links(user_id, subject_id, pair_id,"
                        " derived) VALUES (?,?,?,1)", (user, subject_id, pid))

    # -- mentions ----------------------------------------------------------

    def events_mentioning(self, user: str, surface: str, limit: int = 200) -> list[int]:
        return [r[0] for r in self._conn().execute(
            "SELECT event_id FROM mentions WHERE user_id=? AND norm=? LIMIT ?",
            (user, normalise(surface), limit)).fetchall()]

    def known_mentions(self, user: str) -> list[tuple[str, str]]:
        return [(r["surface"], r["norm"]) for r in self._conn().execute(
            "SELECT surface, norm, COUNT(*) n FROM mentions WHERE user_id=?"
            " GROUP BY norm ORDER BY n DESC", (user,)).fetchall()]

    # -- vectors -----------------------------------------------------------

    def put_vector(self, user: str, event_id: int, vec: bytes, dim: int, norm: float) -> None:
        conn = self._conn()
        with self._write_lock:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO px_vec(event_id, user_id, dim, norm, vec)"
                    " VALUES (?,?,?,?,?)", (event_id, user, dim, norm, vec))

    def unvectorised(self, user: str, limit: int = 5000) -> list[tuple[int, str]]:
        rows = self._conn().execute(
            "SELECT e.event_id, e.text FROM events e"
            " LEFT JOIN px_vec v ON v.event_id = e.event_id"
            " WHERE e.user_id=? AND e.tombstone=0 AND v.event_id IS NULL LIMIT ?",
            (user, limit)).fetchall()
        return [(r[0], r[1]) for r in rows]

    def vectors(self, user: str) -> list[tuple[int, int, float, bytes]]:
        return [(r["event_id"], r["dim"], r["norm"], r["vec"]) for r in self._conn().execute(
            "SELECT v.event_id, v.dim, v.norm, v.vec FROM px_vec v"
            " JOIN events e ON e.event_id=v.event_id AND e.tombstone=0"
            " WHERE v.user_id=?", (user,)).fetchall()]
