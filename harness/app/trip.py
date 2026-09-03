"""Trip itineraries: a plan of days per leg, then a change to one leg.

    "When Gav and I first mapped that trip out, we had 11 days in Italy,
     9 in Portugal, then 3 in Sweden."
    "Quick update on the trip Gav and I planned: we're adding 2 days to our
     time in Italy, but leaving the other two stays alone."

Questions ask for one leg after the change, the longest stay, or the whole
trip. All three are arithmetic over the same two turns, so they are solved
here deterministically rather than left to a word extractor, which answered
"2025" (the year in the question) and "in".
"""

from __future__ import annotations

import re

from .tools import _despell

_ASK = re.compile(r"\bhow\s+many\s+days\b|\bdays\b.{0,30}\b(?:longest|whole|total|entire|altogether)\b|"
                  r"\b(?:longest|shortest)\s+(?:stay|leg|stop)\b", re.IGNORECASE)
_PLAN = re.compile(r"\b(\d{1,3})\s+days?\s+in\s+([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)*)", re.IGNORECASE)
_PLAN_MORE = re.compile(r"\b(\d{1,3})\s+in\s+([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)*)", re.IGNORECASE)
_CHANGE = re.compile(
    r"\b(adding|add|extending|extend|cutting|cut|trimming|trim|dropping|drop|removing|remove|shortening)\s+"
    r"(\d{1,3})\s+days?\s+(?:to|from|off)\s+(?:our\s+)?(?:time\s+in\s+|stay\s+in\s+)?([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)*)",
    re.IGNORECASE)
_LEG_Q = re.compile(r"\bin\s+([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)*)\b(?:\s+(?:after|now|following))")
_COUNTRY_Q = re.compile(r"\b(?:the\s+)?([A-Z][\w'-]+)\s+(?:part|leg|stay|portion|stretch)\b")


def wants(question: str) -> bool:
    return bool(_ASK.search(question or ""))


_COMPANION = re.compile(r"\bplanned\s+with\s+([A-Z][\w'-]+)\b")
_TRIP_NAME = re.compile(r"\b([a-z]+\s+(?:route|path|circuit|loop|trail|run|tour|line))\b", re.IGNORECASE)


def solve(question: str, evidence: list[str], retrieve=None) -> tuple[str, str] | None:
    """(answer, explanation) or None. `evidence` is the retrieved turns' text.

    The store holds several trips, each with its own plan and update, and the
    question names one by nickname ("cedar path"). The nickname turn names
    the companion ("planned with Mo"); the plan and the update are the
    companion's turns ("When Mo and I first mapped that trip out ..."). So:
    nickname -> companion -> only that companion's turns count.
    """
    q = _despell(question or "")
    m = _TRIP_NAME.search(q)
    nick = m.group(1).lower() if m else ""
    companion = ""
    for text in evidence:
        if nick and nick in text.lower():
            cm = _COMPANION.search(_despell(text))   # "plqnned with Mo"
            if cm:
                companion = cm.group(1)
                break
    if companion:
        pool = [x for x in evidence if re.search(rf"\b{re.escape(companion)}\b", x)]
        if retrieve is not None:
            have = set(pool)
            for x in retrieve(f"{companion} and I trip days"):
                if x not in have and re.search(rf"\b{re.escape(companion)}\b", x):
                    pool.append(x); have.add(x)
        if pool:
            evidence = pool
    legs: dict[str, int] = {}
    order: list[str] = []
    for text in evidence:
        t = _despell(text)
        if not re.search(r"\bdays?\b", t, re.IGNORECASE):
            continue
        for m in _PLAN.finditer(t):
            name = m.group(2).strip()
            if name.lower() not in legs:
                legs[name.lower()] = int(m.group(1)); order.append(name)
        # "..., 9 in Portugal, then 3 in Sweden" -- the unit is implied
        if legs:
            for m in _PLAN_MORE.finditer(t):
                name = m.group(2).strip()
                if name.lower() not in legs:
                    legs[name.lower()] = int(m.group(1)); order.append(name)
    if not legs:
        return None
    why = [f"{n}={legs[n.lower()]}" for n in order]
    for text in evidence:
        for m in _CHANGE.finditer(_despell(text)):
            verb, n, leg = m.group(1).lower(), int(m.group(2)), m.group(3).strip().lower()
            if leg not in legs:
                continue
            delta = n if verb.startswith(("add", "extend")) else -n
            legs[leg] += delta
            why.append(f"{leg}{delta:+d}")
    ql = q.lower()
    if re.search(r"\b(?:whole|total|entire|altogether|overall)\b", ql):
        return str(sum(legs.values())), " ".join(why) + f" total={sum(legs.values())}"
    if re.search(r"\blongest\b", ql):
        return str(max(legs.values())), " ".join(why) + f" longest={max(legs.values())}"
    if re.search(r"\bshortest\b", ql):
        return str(min(legs.values())), " ".join(why) + f" shortest={min(legs.values())}"
    for name in order:
        if re.search(rf"\b{re.escape(name)}\b", q, re.IGNORECASE):
            return str(legs[name.lower()]), " ".join(why) + f" {name}={legs[name.lower()]}"
    return None
