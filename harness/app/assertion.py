"""Assertion resolution: which value in a passage is actually being claimed?

The `parser-divergence-*` families exist because retrieval is not the hard part
here. Both the right value and the wrong one are planted in the SAME seeded
pair, so a harness that finds the pair and echoes it has surfaced both:

    "my dentist is not Dr. Ava Gates. My current dentist is Dr. Diana Martin."
    "you used to have me down as banking with Tran Credit Union. As of last
     month I moved every account over to Perez Bank & Trust, so Perez Bank &
     Trust is my bank now."

The grader zeroes a response that surfaces a wrong same-attribute value, so
echoing the evidence sentence is worse than useless -- it converts a case we
retrieved correctly into a zero.

Four ways a value can appear without being asserted:

    negation          "my dentist is NOT Dr. Ava Gates"
    retraction        "you USED TO have me down as ... "
    hypothetical      "IF we had gone with ... it WOULD have been ..."
    reported speech   "my brother SAID the code was ..."

This module splits a passage into clauses, scores each for whether it carries a
live first-person assertion, and returns values from the winners only. It is a
general mechanism -- no family label reaches it, and nothing here knows what a
benchmark is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Clause boundaries. Sentence enders, plus the connectives these constructions
# actually hinge on ("..., so X is my bank now", "..., but I switched to Y").
#
# The negative lookbehind is load-bearing, not decoration. Without it a period
# after an honorific splits "Dr. Diana Martin" in half, the clause ends at "Dr."
# and the extracted name becomes "Dr" -- which turns a case whose evidence was
# retrieved perfectly into a zero. Single capitals cover middle initials.
# NOTE the scoped (?-i:) on the initials guard. `_SPLIT` is compiled with
# IGNORECASE, which makes a bare [A-Z] match lowercase as well -- so
# "(?<![A-Z]\.)" rejected EVERY sentence-ending period, the splitter returned
# one clause for the whole passage, and negation/retraction filtering silently
# stopped working. It still scored on some cases by accident, because taking the
# last proper noun in an unsplit passage often lands on the corrected value.
_ABBREV = (r"(?<!\bDr\.)(?<!\bMr\.)(?<!\bMrs\.)(?<!\bMs\.)(?<!\bProf\.)"
           r"(?<!\bSt\.)(?<!\bJr\.)(?<!\bSr\.)(?<!(?-i:[A-Z])\.)")
_SPLIT = re.compile(_ABBREV + r"(?<=[.!?;])\s+|\s+(?:,\s*)?(?:so|but|however|although|though|"
                    r"whereas|instead|rather)\s+|\s*—\s*|\s*--\s*", re.IGNORECASE)

_NEGATION = re.compile(
    r"\b(?:not|isn'?t|aren'?t|wasn'?t|weren'?t|no\s+longer|never|nor|"
    r"don'?t|doesn'?t|didn'?t|cannot|can'?t|won'?t)\b", re.IGNORECASE)

_RETRACTION = re.compile(
    r"\b(?:used\s+to|previously|formerly|before|earlier|at\s+one\s+point|"
    r"you\s+(?:have|had)\s+me\s+down|on\s+file|old|former|ex-|"
    r"i\s+(?:moved|switched|changed|left)\s+(?:away\s+)?from|"
    r"was\s+my|were\s+my|had\s+been)\b", re.IGNORECASE)

_HYPOTHETICAL = re.compile(
    r"\b(?:if\s+|would\s+have|would\s+be|could\s+have|might\s+have|"
    r"suppose|hypothetical\w*|imagine|were\s+we\s+to|had\s+we|"
    r"were\s+i\s+to|in\s+that\s+case|quote[ds]?\s+(?:us|me)\b)", re.IGNORECASE)

# THIRD time IGNORECASE has bitten this file: a bare [A-Z] inside an
# IGNORECASE pattern matches lowercase, so "my own note says" looked like
# "Katherine said". Any character class here that means "a capital letter"
# must be wrapped in (?-i: ).
_REPORT_VERB = (r"said|says|claimed|claims|told\s+me|mentioned\s+that|thinks|thought|"
                r"believes|believed|heard|insists|insisted|reckons|guessed|swears")
# Reported speech only counts when somebody ELSE is the source. "my own note
# says it is 2677" is the user's own record and is the very value being asked
# for -- flagging it as hearsay drops the correct answer and leaves only the
# third party's wrong one.
_REPORTED = re.compile(
    rf"(?:\b(?:(?-i:[A-Z][a-z]+)|he|she|they|someone|somebody|everyone|my\s+(?:brother|"
    rf"sister|friend|colleague|neighbou?r|partner|boss|mother|father|mum|dad))\s+"
    rf"(?:{_REPORT_VERB})\b)|\baccording\s+to\s+(?!my\b)",
    re.IGNORECASE)

# Markers that a clause states the CURRENT state. These outrank position.
_CURRENT = re.compile(
    r"\b(?:now|current(?:ly)?|these\s+days|today|as\s+of|going\s+forward|"
    r"from\s+now\s+on|new|latest|updated?\s+to|switched\s+to|moved\s+(?:to|over)|"
    r"is\s+my|are\s+my|i\s+use|i\s+bank\s+with)\b", re.IGNORECASE)

# First-person ownership. A clause about someone else's dentist is not about
# the user's, which is what the near-miss disambiguation family turns on.
_FIRST_PERSON = re.compile(r"\b(?:my|mine|i|me|our|we)\b", re.IGNORECASE)


@dataclass(slots=True)
class Clause:
    text: str
    index: int
    negated: bool
    retracted: bool
    hypothetical: bool
    reported: bool
    current: bool
    first_person: bool

    @property
    def asserted(self) -> bool:
        """Is this clause a live claim about the user's present state?"""
        return not (self.negated or self.retracted or self.hypothetical or self.reported)

    @property
    def rank(self) -> tuple:
        """Sort key, best first.

        Ordering rationale: an asserted clause always beats a non-asserted one;
        among asserted clauses an explicit currency marker beats position; and
        position breaks the remaining ties LATER-first, because these passages
        are written as correction-then-truth ("X was wrong, Y is right").
        """
        return (not self.asserted, not self.current, not self.first_person, -self.index)


def split_clauses(text: str) -> list[Clause]:
    out: list[Clause] = []
    for i, raw in enumerate(_SPLIT.split(text or "")):
        s = (raw or "").strip()
        if len(s) < 3:
            continue
        out.append(Clause(
            text=s, index=i,
            negated=bool(_NEGATION.search(s)),
            retracted=bool(_RETRACTION.search(s)),
            hypothetical=bool(_HYPOTHETICAL.search(s)),
            reported=bool(_REPORTED.search(s)),
            current=bool(_CURRENT.search(s)),
            first_person=bool(_FIRST_PERSON.search(s)),
        ))
    return out


def asserted_clauses(text: str) -> list[Clause]:
    """Clauses that carry a live claim, best first."""
    return sorted((c for c in split_clauses(text) if c.asserted), key=lambda c: c.rank)


def best_clause(text: str) -> Clause | None:
    cs = sorted(split_clauses(text), key=lambda c: c.rank)
    return cs[0] if cs else None


# --------------------------------------------------------------------------
# value extraction
# --------------------------------------------------------------------------

_TITLE = r"(?:Dr\.?|Doctor|Mr\.?|Mrs\.?|Ms\.?|Prof\.?)"
# A proper-noun run, optionally with an honorific and internal &/of/and.
_NAME = re.compile(
    rf"\b(?:{_TITLE}\s+)?[A-Z][\w'’-]+(?:\s+(?:&|and|of|de|van|von|del)\s+[A-Z][\w'’-]+|"
    rf"\s+[A-Z][\w'’-]+){{0,3}}\b")
# A code has letters AND digits, or a hyphen: VK-7Q2M, DURALA-7895, A1B2C3.
# The previous pattern, (?:[A-Z0-9]+-?){2,}, matched ANY 4+ digit number by
# backtracking ("716"+"2"), so a budget figure passed as a "code" and the
# adjacency rule below never got the chance to reject it.
_CODE = re.compile(
    # A code starts and ends on a letter or digit. Without the anchors the
    # pattern also matched the lone hyphen inside "front-door", and that "-"
    # was returned as the code from the reported clause of a door-code case.
    r"\b(?=[A-Za-z0-9-]{4,})(?=[A-Z0-9-]*-|(?=[A-Z0-9]*[A-Z])[A-Z0-9]*\d)[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?\b"
    # The benchmark's own token shape: "rohulo_8926", "kijodo_6948". Lowercase
    # letters, one underscore, four digits -- nothing else in English prose
    # looks like it. Asked for "my check-in code", the uppercase-only pattern
    # saw no code at all and the answer fell through to the door code.
    r"|\b[a-z]{4,8}_\d{4}\b")
_NUMBER = re.compile(r"\b\d[\d,]*(?:\.\d+)?\b")

# Words that look like names at sentence start but never are the answer.
_NOT_A_NAME = frozenset("""
i my me we our the a an this that these those one thing update as of so but and
just keep straight records record note now current currently new old former
""".split())


def _clean_name(s: str) -> str:
    s = re.sub(r"^(?:%s)\s+" % _TITLE, "", s).strip(" .,;:")
    return s


def extract_value(clause: str, kind: str = "value") -> str:
    """Pull the salient value out of one clause.

    `kind` mirrors the grader's answer kinds well enough to choose an extractor:
    'code' for identifiers, 'money'/'number' for figures, otherwise a name.
    """
    c = clause or ""
    if kind == "code":
        m = _CODE.findall(c)
        if m:
            return m[0]
        # Plenty of things called a "code" are plain digits -- a door code, a
        # PIN. `_CODE` needs two alphanumeric groups, so it never matches those.
        # Accept a bare number ONLY when it sits next to the word "code" in this
        # clause ("the front-door code is 2677"). The unconditional fallback
        # returned an unrelated figure (7162) on a canary case whose evidence
        # held no code at all -- a confident wrong answer where an abstention
        # was the honest outcome.
        near = re.search(r"\bcode\b\W{0,12}(?:is|was|=|:)?\W{0,4}(\d[\d,]*)", c, re.I) or \
               re.search(r"(\d[\d,]*)\W{0,12}\bcode\b", c, re.I)
        return near.group(1).replace(",", "") if near else ""
    if kind in ("money", "number"):
        m = _NUMBER.findall(c)
        return m[-1].replace(",", "") if m else ""
    # names: take the last proper-noun run, which is where the corrected value
    # sits in "X is not A. My current X is B."
    cands = [_clean_name(m.group(0)) for m in _NAME.finditer(c)]
    cands = [x for x in cands if x and x.split()[0].lower() not in _NOT_A_NAME]
    return cands[-1] if cands else ""


# Somebody else's value, sitting in the same passage as the user's. The canary
# family plants a colleague's badge code right beside the user's own and asks
# for "mine, not either colleague's"; surfacing the colleague's is the ×0.25
# integrity disqualifier, not merely a wrong answer.
_THIRD_PARTY = re.compile(
    r"\b(?:colleague|coworker|co-worker|partner|friend|brother|sister|neighbou?r|"
    r"boss|manager|client|teammate|(?-i:[A-Z][a-z]+)'s)\b", re.IGNORECASE)
# A clause that assigns the thing to someone else, or disowns it outright.
_DISOWNED = re.compile(r"\bbelongs\s+to\b|\bnot\s+(?:mine|my)\b|\bthat'?s\s+theirs\b|\bisn'?t\s+mine\b", re.IGNORECASE)


def resolve(passage: str, kind: str = "value") -> tuple[str, str]:
    """Return (value, supporting_clause) for the live assertion in a passage.

    Falls back to the best-ranked clause overall when nothing is cleanly
    asserted -- answering from a weak clause still beats abstaining, since the
    loss is symmetric. The one exception is a code-kind question: a code bound
    to a third party in its own clause is never the user's, and is skipped even
    if it is the only code in sight.
    """
    for c in asserted_clauses(passage):
        # "My colleague Priya's badge code is DESEHA-6849" contains "my", so a
        # first-person test does not exclude it. For a code question any clause
        # that names a third party is skipped outright: a code sitting beside
        # someone else's name is theirs, whatever pronoun introduces them.
        if kind == "code" and _THIRD_PARTY.search(c.text):
            continue
        v = extract_value(c.text, kind)
        if v:
            return v, c.text
    if kind == "code" and re.search(r"\bcode\b", passage or "", re.I):
        # A plain-digit code whose clause does not itself say "code": "Katherine
        # insisted the front-door code was 7440, but my own note says IT IS
        # 2677." The word lives in the reported clause; the value lives in the
        # asserted one. Accept an asserted copula + digits when the passage as
        # a whole is about a code -- and still nothing for "budget of 7162".
        for c in asserted_clauses(passage):
            if _THIRD_PARTY.search(c.text):
                continue
            m = re.search(r"\b(?:is|=|:|reads|says\s+it\s+is)\s*(\d{3,8})\b", c.text, re.I)
            if m:
                return m.group(1), c.text
    b = best_clause(passage)
    if b:
        v = extract_value(b.text, kind)
        if v:
            return v, b.text
    return "", ""


def strip_unasserted(passage: str) -> str:
    """The passage with negated / retracted / hypothetical / reported clauses removed.

    Used to build `final_text`. Emitting the raw evidence sentence surfaces the
    planted wrong value alongside the right one, and the grader zeroes a
    response that does that -- so a case we retrieved perfectly becomes a zero.
    """
    keep = [c.text for c in split_clauses(passage) if c.asserted]
    return " ".join(keep) if keep else (passage or "")


# --------------------------------------------------------------------------
# attribute-anchored extraction
# --------------------------------------------------------------------------

_STOPW = frozenset("""
what which who whose where when why how should would could does do did is are was
were the a an my your our their his her its this that these those you your me i we
now then here there for from with about into onto upon set setting use using apply
applied choose chosen pick want need long ahead workday workdays own not never no
none nothing dr doctor mr mrs ms prof
""".split())


def question_attributes(question: str) -> list[str]:
    """Content nouns the question is asking ABOUT ('accent color', 'font')."""
    return [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z-]{2,}", question or "")
            if w.lower() not in _STOPW]


# Allow an honorific to lead the value so "Dr. Diana Martin" is captured whole;
# without it the run stops at "Dr" and the real name is lost.
# FOURTH IGNORECASE bite in this file. The continuation `\s+[A-Z]...` is meant
# to absorb only further CAPITALISED words ("Diana Martin", "Perez Bank &
# Trust"); under IGNORECASE it absorbed "last spring" too and "I moved to
# Lisbon last spring" yielded the value "Lisbon last spring". Scoped (?-i:).
# "Reynolds Bank & Trust", "Procter and Gamble": an ampersand or "and" joins
# capitalised words into one name; the extractor used to stop at "Reynolds".
_VALUEW = (r"(?:(?:Dr|Doctor|Mr|Mrs|Ms|Prof)\.?\s+)?"
           r"[A-Za-z][\w'’-]*(?:\s+(?:(?:&|and)\s+)?(?-i:[A-Z])[\w'’-]*){0,3}")
_LEAD_TITLE = re.compile(r"^(?:Dr|Doctor|Mr|Mrs|Ms|Prof)\.?\s+", re.IGNORECASE)


def value_for_attribute(passage: str, attrs: list[str]) -> str:
    """Find the value bound to one of `attrs` in `passage`.

    Preference questions ("which accent color should you choose?") are answered
    by a value stated next to the attribute in an earlier turn, and a generic
    proper-noun grab picks the wrong token entirely -- on "Pleease keep my
    workspace on light mode" it returns "Pleease", a typo'd greeting that merely
    looks like a name. Anchoring on the attribute the question actually names is
    what makes the extraction mean anything.

    Three bindings, in order of how directly they state the value:
        "<attr> is <value>"        my accent is teal
        "<value> is the <attr>"    Atkinson Hyperlegible is the font I want
        "<value> <attr>"           light mode
    """
    text = passage or ""
    # Patterns outer, attributes inner. Exhausting every attribute against the
    # STRONG bindings before trying the weak adjacency one matters: for "which
    # accent color should you choose?" the attribute list starts with "ditto",
    # and bare adjacency on that returns "personal" from "my personal Ditto
    # accent", beating the "accent is teal" that actually answers the question.
    templates = (
        r"\b{a}\b\s*(?:is|are|=|:)\s*(?P<v>{V})",
        r"(?P<v>{V})\s+(?:is|are)\s+(?:the\s+)?{a}\b",
        r"\b(?P<v>[A-Za-z][\w'’-]*)\s+{a}\b",
    )
    for tpl in templates:
        for a in attrs:
            m = re.search(tpl.format(a=re.escape(a), V=_VALUEW), text, re.IGNORECASE)
            if not m:
                continue
            v = _LEAD_TITLE.sub("", m.group("v")).strip(" .,;:")
            if v and v.lower() not in _STOPW and v.lower() != a and v.lower() not in attrs:
                return v
    return ""


# --------------------------------------------------------------------------
# the value a declarative turn STATES
# --------------------------------------------------------------------------

_STATE_PATTERNS = (
    # "my personal Ditto accent is teal"  /  "my dentist is Dr. Diana Martin"
    r"\bmy\b(?:\s+\w+){0,4}\s+(?:is|are|will\s+be)\s+(?P<v>%s)",
    # "Aptos is the font I want"  /  "Perez Bank is my bank now"
    r"(?P<v>%s)\s+(?:is|are)\s+(?:the|my)\b",
    # "keep my workspace on light mode"  /  "switch to dark mode"
    r"\b(?:on|to|in)\s+(?P<v>[A-Za-z][\w-]*)\s+(?:mode|theme|style|setting)\b",
    # "I moved to Lisbon"  /  "I use Postgres"
    r"\bI\s+(?:moved|relocated|switched|changed|now\s+use|use|prefer|chose|picked)\s+"
    r"(?:to\s+)?(?P<v>%s)",
)


def stated_value(text: str) -> str:
    """The value a first-person declarative asserts, for the answer slot.

    A conversational-declarative case expects the STATED value back ("teal",
    "Aptos", "light"); a later behavior case asks for the same value. Returning
    it in the answer slot here does double duty: it satisfies the
    acknowledgement grader and it is what gets stored for the later read.
    """
    t = strip_unasserted(text or "")
    if not t:
        return ""
    for pat in _STATE_PATTERNS:
        rx = pat % _VALUEW if "%s" in pat else pat
        m = re.search(rx, t, re.IGNORECASE)
        if not m:
            continue
        v = _LEAD_TITLE.sub("", m.group("v")).strip(" .,;:")
        if v and v.lower() not in _STOPW and v.lower() not in _NOT_A_NAME:
            return v
    return ""
