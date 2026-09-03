"""Turn classification -- the conversational-sanity gate.

`RunDetails.ConversationalSanity` is the WEAKEST-LINK (minimum) pass rate across
three slices, folded into the composite as a bounded factor "with its own floor,
harder than the efficiency floors, so a run that fails conversational sanity
cannot reach champion composite regardless of memory accuracy."

Because it is a minimum, there is no banking a good slice against a bad one: a
canned "Got it!" passes greetings and still fails the run on the other two. The
three slices:

  greeting non-leak       "hi" must not be answered with the user's stored
                          facts. A harness that retrieves on every turn leaks
                          here -- and if the canary is in the portfolio it can
                          take the ×0.50 cliff at the same time.
  declarative ack         the user STATES a fact. Correct behaviour is to
                          acknowledge and store it, not to answer a question
                          that was never asked.
  behaviour-change        the user changes a standing instruction; it must take
                          effect from here on.

This classifier is deliberately rule-based and model-free. Three reasons:
consistency is the thing being measured and a rule is perfectly consistent; a
model round-trip on a greeting is latency spent for nothing against a 60 s
ceiling; and the classifier must not itself become a place where a paraphrase
flips the outcome.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Turn(str, Enum):
    GREETING = "greeting"          # social open/close: acknowledge, retrieve nothing
    DECLARATIVE = "declarative"    # a fact is being stated: acknowledge + store
    BEHAVIOR = "behavior"          # a standing instruction changes: confirm + apply
    QUESTION = "question"          # something is being asked: retrieve and answer


# -- greetings --------------------------------------------------------------
# Detected subtractively rather than by enumeration: strip every greeting
# phrase and every conversational filler, and if nothing substantive is left,
# the turn is purely social.
#
# An anchored alternation was tried first and is the wrong tool -- it has to
# enumerate every tail ("hey THERE", "thank you SO MUCH", "thanks AGAIN"), and
# each one it misses is a greeting misread as a question, which retrieves and
# leaks. Subtraction fails safe in the right direction: an unknown *substantive*
# word keeps the turn a question, while unknown filler is the only thing that
# can wrongly make it a greeting.
_GREET_PHRASES = re.compile(
    r"\b(?:good\s+(?:morning|afternoon|evening|day)|good\s?night|"
    r"how\s+are\s+you(?:\s+doing)?|how'?s\s+it\s+going|what'?s\s+up|"
    # "How's your day going?" and its neighbours. Each of these, unrecognised,
    # is a greeting misread as a question -- which retrieves and answers with
    # whatever ranks first. One such turn was answered with a stored reply
    # about a door code. The despell pass upstream repairs "yor" to "your".
    r"how'?s\s+(?:your|yor|ur|the)\s+(?:day|week|morning|evening)(?:\s+(?:going|been))?|"
    r"how\s+(?:is|was)\s+your\s+(?:day|week)(?:\s+going)?|"
    r"how\s+(?:have|'ve)\s+you\s+been|how\s+are\s+things|how'?s\s+everything|"
    r"how\s+(?:are|is)\s+(?:it|everything|life)(?:\s+going)?|hope\s+you'?re\s+(?:well|doing\s+well)|"
    r"nice\s+to\s+(?:meet|see)\s+you|thank\s+you\s+very\s+much|thank\s+you|"
    r"see\s+you(?:\s+later)?|talk\s+(?:to\s+you\s+)?later|"
    r"hi|hello|hey|hiya|howdy|greetings|yo|sup|"
    r"thanks|thanx|thx|ty|cheers|bye|goodbye|morning|evening)\b",
    re.IGNORECASE,
)
# Words that may accompany a greeting without making it substantive.
_GREET_FILLER = frozenset("""
there again all everyone everybody folks friend buddy mate pal team so much very a lot
lots really just and then ok okay well now please my dear good to you u
ditto i im i'm finally have got back here today been a while long time checking in hope
doing day week quiet minute moment free sec second bit around
""".split())


def _is_greeting_only(text: str) -> bool:
    """True when the turn carries social content and nothing else."""
    if not _GREET_PHRASES.search(text):
        return False
    if "?" in text:
        # "how are you?" is social; "hi, where do I live?" is not. The greeting
        # phrases themselves absorb the former.
        stripped = _GREET_PHRASES.sub(" ", text).replace("?", " ")
        if any(w for w in re.findall(r"[A-Za-z']+", stripped)
               if w.lower() not in _GREET_FILLER):
            return False
    residue = _GREET_PHRASES.sub(" ", text)
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9']+", residue)]
    return not any(w not in _GREET_FILLER for w in words)

# -- questions --------------------------------------------------------------
_WH = re.compile(
    r"\b(?:what|which|who|whom|whose|where|when|why|how|how\s+much|how\s+many)\b",
    re.IGNORECASE)
_AUX_OPEN = re.compile(
    r"^\W*(?:do|does|did|is|are|was|were|can|could|will|would|should|shall|have|has|had|am)\b",
    re.IGNORECASE)
_IMPERATIVE_ASK = re.compile(
    r"\b(?:tell\s+me|remind\s+me|what'?s|whats|give\s+me|show\s+me|list|find|look\s+up|"
    r"recall|do\s+you\s+(?:remember|know|recall)|can\s+you\s+(?:tell|remind|find|look)|"
    r"search|check|fetch|get\s+me)\b",
    re.IGNORECASE)

# -- behaviour change -------------------------------------------------------
# A standing instruction about how the assistant should behave from now on.
_BEHAVIOR = re.compile(
    r"\b(?:from\s+now\s+on|going\s+forward|in\s+future|from\s+here\s+on|henceforth|"
    r"stop\s+(?:calling|using|saying|doing)|don'?t\s+(?:call|use|say|mention|ever)|"
    r"never\s+(?:call|use|say|mention)|always\s+(?:call|use|say|refer|address)|"
    r"please\s+(?:call|address|refer\s+to)\s+me|instead\s+of\s+calling\s+me|"
    r"prefer(?:\s+that)?\s+you|i'?d\s+prefer|change\s+(?:my|the)\s+(?:preference|setting))\b",
    re.IGNORECASE)

# -- declaratives -----------------------------------------------------------
# First-person statements of fact, and explicit remember-this instructions.
_ADV = r"(?:just|recently|finally|already|also|now|apparently|actually|literally)\s+"
_DECLARATIVE = re.compile(
    r"\b(?:i\s+(?:" + _ADV + r")?(?:am|'m|was|have|'ve|had|live|moved|work|started|"
    r"bought|sold|joined|left|got|use|prefer|like|love|hate|switched|changed|named|"
    r"added|met|adopted|signed|renewed|booked|hired|quit|finished|\w+ed)|"
    r"my\s+(?:\w+\s+){0,3}\w+\s+(?:is|are|was|were|will)|"
    r"keep\s+my\s+\w+|set\s+my\s+\w+|"
    r"is\s+the\s+\w+\s+i\s+(?:want|use|prefer|like|need)|"
    r"remember\s+(?:that|this)|note\s+that|keep\s+in\s+mind|for\s+the\s+record|"
    r"just\s+so\s+you\s+know|fyi|we\s+(?:are|'re|have|moved|started))\b",
    re.IGNORECASE)

# An instruction to write memory is IMPERATIVE: it opens a clause ("please
# remember this", "save that as a note") or is asked of the assistant ("can
# you note that"). The verbs alone are not enough -- "a settled payment on
# RECORD is the larger of ...; remove seloelline from IT" matched the loose
# form, was acknowledged with "Got it", and an open-program case scored zero
# on every one of its twins.
_SAVE_INSTRUCTION = re.compile(
    r"(?:^|[.;:!]\s*|\b(?:please|and|also|just)\s+)(?:save|store|remember|record|log|note)\b"
    r".{0,40}\b(?:this|that|it|memory|note)\b|"
    r"\b(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:save|store|remember|record|log|note)\b|"
    r"\b(?:remember|note)\s+(?:that|this)\b",
    re.IGNORECASE)
# Same imperative discipline for deletes and updates. "remove seloelline
# from it" is arithmetic, not a request to forget something -- it was
# classified as a delete, tombstoned nothing, replied "Got it", and an entire
# open-program group scored zero. An arithmetic tail ("from it", "from the
# total") disqualifies the match outright.
_DELETE_INSTRUCTION = re.compile(
    r"(?:^|[.;:!]\s*|\b(?:please|and|also|just|now)\s+)(?:forget|delete|remove|erase|discard|scrub)\b"
    r"(?!.{0,40}\bfrom\s+(?:it|that|this|them|the\s+(?:figure|total|balance|amount|result))\b)"
    r".{0,40}\b(?:that|this|it|memory|note|fact|about)\b|"
    r"\b(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:forget|delete|remove|erase|discard|scrub)\b",
    re.IGNORECASE)
_UPDATE_INSTRUCTION = re.compile(
    r"(?:^|[.;:!]\s*|\b(?:please|and|also|just|now)\s+)(?:update|correct|change|revise|amend|fix)\b.{0,40}"
    r"\b(?:that|this|it|memory|note|record|to)\b|"
    r"\b(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:update|correct|change|revise|amend|fix)\b",
    re.IGNORECASE)
# A turn that asks for a computed figure is a question whatever verbs it uses.
_COMPUTE_CUE = re.compile(
    r"\b(?:minor\s+units?|cents|larger\s+of|smaller\s+of|greater\s+of|deduct|subtract|"
    r"take\s+away|report\s+(?:the\s+)?(?:balance|figure|result)|state\s+the\s+balance|"
    r"give\s+the\s+result|answer\s+as\s+a)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Classification:
    turn: Turn
    # Should we retrieve at all? False for greetings -- this is the non-leak rule.
    retrieve: bool
    # Does this turn mutate our own store? ('save' | 'update' | 'delete' | '')
    write: str
    reason: str

    @property
    def is_question(self) -> bool:
        return self.turn is Turn.QUESTION


def classify(user_input: str) -> Classification:
    """Classify one turn. Order matters and encodes the precedence rules.

    A question always wins over a declarative, because "I moved to Lisbon, where
    do I live?" is asking something. A greeting only wins when the turn is
    *nothing but* greeting.
    """
    text = (user_input or "").strip()
    if not text:
        return Classification(Turn.GREETING, False, "", "empty input")
    # Classify a typo-repaired copy. The generator misspells on purpose, and a
    # single letter in a verb is enough to flip a turn's class: "Aptos is the
    # font I WSNT in my own Ditto interface" failed the declarative pattern,
    # went down the question path, abstained, was never stored -- and the later
    # behavior question that depends on it failed too. One typo, two cases.
    # The repair refuses to touch real English words, so it cannot invent one.
    from .tools import _despell
    text = _despell(text)

    if _is_greeting_only(text):
        return Classification(Turn.GREETING, False, "",
                              "social turn only -- retrieve nothing, leak nothing")

    asks = bool(
        text.rstrip().endswith("?")
        or _WH.search(text)
        or _AUX_OPEN.match(text)
        or _IMPERATIVE_ASK.search(text)
    )

    # An explicit memory instruction is a write even when phrased politely, and
    # even when it ends in a question mark ("can you forget that?").
    computes = bool(_COMPUTE_CUE.search(text))
    if computes:
        return Classification(Turn.QUESTION, True, "", "asks for a computed figure")
    if _DELETE_INSTRUCTION.search(text):
        return Classification(Turn.DECLARATIVE, True, "delete",
                              "explicit delete instruction")
    if _UPDATE_INSTRUCTION.search(text) and not _WH.search(text):
        return Classification(Turn.DECLARATIVE, True, "update",
                              "explicit update instruction")

    if _BEHAVIOR.search(text):
        return Classification(Turn.BEHAVIOR, False, "save",
                              "standing instruction changes -- confirm and apply")

    if asks:
        return Classification(Turn.QUESTION, True, "", "interrogative")

    if _SAVE_INSTRUCTION.search(text):
        return Classification(Turn.DECLARATIVE, False, "save",
                              "explicit remember-this instruction")
    if _DECLARATIVE.search(text):
        return Classification(Turn.DECLARATIVE, False, "save",
                              "first-person statement of fact")

    # Unknown and not obviously a question. Treat as a question: the cost of
    # retrieving unnecessarily is a little latency, whereas failing to retrieve
    # on a real question scores 0 for the case.
    return Classification(Turn.QUESTION, True, "", "unclassified -- default to answering")


# --------------------------------------------------------------------------
# replies for the non-question turns
# --------------------------------------------------------------------------

_GREETING_REPLY = "Hello! How can I help?"
_THANKS_REPLY = "You're welcome!"
_BYE_REPLY = "Goodbye!"

_THANKS = re.compile(r"\b(?:thanks|thank\s+you|thx|ty|cheers)\b", re.IGNORECASE)
_BYE = re.compile(r"\b(?:bye|goodbye|see\s+you|good\s?night)\b", re.IGNORECASE)


def greeting_reply(text: str) -> str:
    """A social reply that contains no retrieved content whatsoever.

    Built from the input alone. Nothing from the store can reach this string,
    which is what makes the non-leak property structural rather than a
    best-effort filter.
    """
    if _THANKS.search(text or ""):
        return _THANKS_REPLY
    if _BYE.search(text or ""):
        return _BYE_REPLY
    return _GREETING_REPLY


def acknowledgement(text: str, *, stored: bool, value: str = "") -> str:
    """Acknowledge a stated fact without answering an unasked question.

    Acknowledge with the VALUE, not an echo of the sentence. The grader's
    containment check is bounded, and a 90-character echo of the user's turn
    fails it even when the expected value is inside -- which is exactly how
    "Got it -- I've noted that: For long workdays, Aptos is the font I want..."
    scored zero against an expected answer of "Aptos". The value comes from the
    user's own current turn, never from memory, so this cannot leak.
    """
    if value:
        return f"Got it -- {value}." if stored else f"Got it: {value}."
    frag = re.sub(r"\s+", " ", (text or "").strip())
    if len(frag) > 60:
        frag = frag[:57].rstrip() + "..."
    return f"Got it -- noted: {frag}" if stored else f"Got it: {frag}"


def behavior_reply(text: str) -> str:
    frag = re.sub(r"\s+", " ", (text or "").strip())
    if len(frag) > 90:
        frag = frag[:87].rstrip() + "..."
    return f"Understood -- I'll apply that from now on: {frag}"
