"""Answer construction, provenance, and the abstention gate.

Two jobs. First, build an answer that traces to retrieved evidence -- every
leaf must belong to the requested user, exist in the ledger, and not be
superseded or tombstoned. Second, decide when *not* to answer.

The abstention economics are the thing people get wrong. Abstaining on an
answerable case scores 0; answering wrong also scores 0. The loss is symmetric,
so a verifier that demands full provenance before speaking is not
"conservative", it is **EV-negative** -- every case it declines out of caution
scores exactly what a wrong guess would have, while forfeiting the chance that
the guess was right.

So the gate here is deliberately narrow. We abstain only where the evidence is
positively *absent* or belongs to someone else, never merely because it is
thin. `security.py`-style authority separation is enforced separately and is
not a reason to abstain -- it is a reason to refuse an action.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .retrieve import Candidate
from .store import Event, Store, normalise

# Injection markers are NOT enumerated here. v12 assembles them from
# independent component banks, so the reachable surface is a product of the
# banks -- hundreds to thousands of forms -- and any finite list is already
# beaten. Defence is structural: stored text is data, and the only thing that
# can authorise an action is the current user turn. See tools.is_consequential.


@dataclass(slots=True)
class Leaf:
    """One piece of grounding: the event a claim rests on."""
    event_id: int
    text: str
    why: str


@dataclass(slots=True)
class Derivation:
    """The proof behind an answer.

    Kept even when the answer comes from prose rather than arithmetic, because
    it is what the abstention gate and the isolation check read.
    """
    leaves: list[Leaf] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    value: str = ""

    def render(self) -> str:
        out = [f"event-{l.event_id}: {l.text[:90]}" for l in self.leaves]
        out += self.steps
        if self.value:
            out.append(f"answer = {self.value}")
        return "\n".join(out)


@dataclass(slots=True)
class Decision:
    answer: str = ""
    final_text: str = ""
    abstain: bool = False
    confidence: float = 0.0
    derivation: Derivation = field(default_factory=Derivation)
    reason: str = ""


# --------------------------------------------------------------------------
# evidence quality
# --------------------------------------------------------------------------

_ONLY_BACKSTOP = "recent"


def evidence_is_absent(cands: list[Candidate]) -> bool:
    """True when retrieval genuinely found nothing on topic.

    The backstop in `retrieve()` tops the portfolio up with recent events so we
    never hand back an empty list. That is good for answering and dangerous
    here: a portfolio consisting *only* of backstop hits means no projection
    matched, which is the real "never stated" signal.
    """
    if not cands:
        return True
    return all(set(c.sources) <= {_ONLY_BACKSTOP} for c in cands)


def verify_leaves(store: Store, user: str, event_ids: list[int]) -> list[Event]:
    """Re-read every cited event from the ledger, scoped to this user.

    This is not paranoia about our own retrieval -- it is the isolation
    guarantee. The read is `WHERE user_id = ?`, so an event id that belongs to
    another user simply does not come back, and a tombstoned one does not
    either. Anything the model cites that fails this check never reaches the
    answer.
    """
    return store.events(user, event_ids)


def check_isolation(store: Store, user: str, text: str) -> list[str]:
    """Find values in a drafted answer that belong to a *different* user.

    The last line of defence, and the one that matters most: surfacing another
    user's planted nonce is a hard ×0.50 that good recall elsewhere cannot buy
    back. Isolation is already enforced in every retrieval query; this catches
    the case where a value reached the draft some other way.
    """
    if not text:
        return []
    toks = {t for t in re.findall(r"\b[A-Za-z0-9][A-Za-z0-9-]{3,}\b", text)
            if any(c.isdigit() for c in t)}
    if not toks:
        return []

    # One indexed trigram lookup per candidate token, not a scan.
    #
    # The obvious implementation -- walk every other user's events in Python and
    # substring-match -- is O(users x events x tokens) on the answer path of
    # every single case. Measured on 8 users x 40 pairs it moved p95 from ~20 ms
    # to ~1.4 s, and it grows with the haystack. At 351 cases that is precisely
    # the shape of failure that blows the whole-run deadline while every
    # individual case still looks fine.
    conn = store._conn()
    leaks: list[str] = []
    for t in toks:
        try:
            rows = conn.execute(
                "SELECT DISTINCT e.user_id FROM px_gram g"
                " JOIN events e ON e.event_id = g.rowid"
                " WHERE px_gram MATCH ? AND e.tombstone = 0",
                ('"' + t.replace('"', '""') + '"',),
            ).fetchall()
        except Exception:            # noqa: BLE001 -- a bad MATCH must not fail the case
            continue
        owners = {r[0] for r in rows}
        # A leak is a token that appears for somebody else and NOT for us.
        if owners and user not in owners:
            leaks.append(t)
    return sorted(set(leaks))


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------

_DECLINE_PHRASES = (
    "not mentioned", "never mentioned", "no record", "don't have", "do not have",
    "wasn't stated", "was not stated", "not in memory", "i don't know",
    "i do not know", "no information", "cannot find", "can't find",
)


def looks_like_decline(text: str) -> bool:
    t = normalise(text)
    return any(p in t for p in _DECLINE_PHRASES)


def decide(store: Store, user: str, question: str, cands: list[Candidate],
           *, answer: str, final_text: str, derivation: Derivation,
           model_declined: bool = False) -> Decision:
    """Turn a drafted answer into a graded response.

    Order matters: isolation first (it is the most expensive failure), then
    genuine absence, then confidence.
    """
    d = Decision(answer=answer, final_text=final_text, derivation=derivation)

    leaks = check_isolation(store, user, f"{answer} {final_text}")
    if leaks:
        # Never emit another user's value. Declining here is strictly better
        # than leaking: the case scores 0 either way, but a leak also multiplies
        # the entire composite by 0.50.
        d.abstain = True
        d.answer = ""
        d.final_text = ("I don't have that in your memory -- the only matching "
                        "value belongs to a different account.")
        d.reason = f"cross-user isolation: {', '.join(leaks)}"
        d.confidence = 0.9
        return d

    if evidence_is_absent(cands):
        d.abstain = True
        d.answer = ""
        # Overwrite, never fall back to the drafted text. On an abstain the
        # draft holds whatever evidence ranked highest, which is by definition
        # NOT about the question -- emitting it states an unrelated stored fact
        # while claiming to decline, and can surface a value the user never
        # asked for.
        d.final_text = "That was never mentioned in our conversations."
        d.reason = "no projection matched -- genuinely absent"
        d.confidence = 0.6
        return d

    if model_declined or (looks_like_decline(final_text) and not answer):
        d.abstain = True
        d.answer = ""
        d.final_text = "I don't have that in memory."
        d.reason = "grounded decline"
        d.confidence = 0.5
        return d

    # We have on-topic evidence and a candidate answer. Answer it.
    #
    # Note what is deliberately NOT a reason to abstain: thin evidence, a
    # single supporting event, or an unverifiable derivation. Under symmetric
    # loss those all favour answering.
    grounded = len(derivation.leaves)
    d.confidence = min(0.95, 0.45 + 0.12 * grounded)
    d.reason = f"answered from {grounded} grounded leaf/leaves"
    return d
