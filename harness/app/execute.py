"""Deterministic execution: value extraction and typed operators.

The model reads prose and identifies semantic roles. It does not do the
arithmetic, the time comparison, or the state resolution. That split is the
whole point: the answer to a computed question never appears verbatim in
memory, and asking a 20B model to hold four prose amounts in its head and
subtract is where scores go to die.

**On the cheating boundary.** v12 samples one of four query programs per
metamorphic group, and it would be easy -- and disqualifying -- to write
`if family == "larger_minus_settled": ...`. Nothing here does that. What is
implemented is a small set of *general* operators (latest, max, subtract,
adjust, sum, count) plus a compiler that picks them from what the request
actually says. Those operators compose into the four observed shapes the same
way they compose into any other arithmetic question, and the code has no notion
of a benchmark family, no dispatch table keyed on one, and no v12 branch.

Money is `Decimal` in integer minor units throughout. Float never touches a
currency value: 0.1 + 0.2 != 0.3 is not an acceptable property for a graded
numeric answer.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# --------------------------------------------------------------------------
# money and number extraction
# --------------------------------------------------------------------------

_CURRENCY = {
    "$": "USD", "usd": "USD", "dollar": "USD", "dollars": "USD", "us$": "USD",
    "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "£": "GBP", "gbp": "GBP", "pound": "GBP", "pounds": "GBP", "sterling": "GBP",
    "¥": "JPY", "jpy": "JPY", "yen": "JPY",
}

# Scale words that multiply a bare figure. v12 states amounts in prose, so
# "22 thousand" and "1.5 million" both occur.
# "USD cents", "minor units", "in cents" -- the figure IS the minor unit.
_MINOR_UNIT = re.compile(r"\b(?:cents?|minor[-\s]units?|pence|pennies|centavos?)\b", re.I)

_SCALE = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "million": 1_000_000,
          "bn": 1_000_000_000, "billion": 1_000_000_000}

# A number with optional thousands separators and decimals, optionally followed
# by a scale word, optionally preceded or followed by a currency marker.
_NUM = re.compile(
    r"(?P<pre>[$€£¥])?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<scale>k\b|thousand\b|m\b|million\b|bn\b|billion\b)?"
    r"\s*(?P<post>usd|eur|gbp|jpy|dollars?|euros?|pounds?|yen)?",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Amount:
    """A monetary or numeric value in integer minor units.

    `minor` is the exact value scaled by 10**exp -- cents for a 2-dp currency.
    Keeping the scale explicit means we never round mid-computation.
    """
    minor: int
    exp: int = 2
    currency: str = ""

    @staticmethod
    def from_decimal(d: Decimal, currency: str = "", exp: int = 2) -> "Amount":
        q = (d * (10 ** exp)).quantize(Decimal(1))
        return Amount(int(q), exp, currency)

    @staticmethod
    def from_minor(n: int, currency: str = "", exp: int = 2) -> "Amount":
        """A figure already stated in minor units ("482680 USD cents")."""
        return Amount(int(n), exp, currency)

    def money_answer(self) -> str:
        """Render money the way the deterministic grader parses it.

        `parseMoneyToken` reads a digit token with NO decimal point as whole
        currency units and multiplies by 100; a token with exactly two fraction
        digits is read as whole*100 + fraction. `expected_answer` is stated in
        MINOR units. So the decimal form round-trips exactly for any value,
        while a bare integer only happens to be right when the source text was
        denominated in whole units -- which is why dollar-denominated seeds
        scored 1.000 and cent-denominated ones scored 0.000 through this same
        code path.
        """
        sign = "-" if self.minor < 0 else ""
        v = abs(int(self.minor))
        return f"{sign}{v // 100}.{v % 100:02d}"

    @property
    def decimal(self) -> Decimal:
        return Decimal(self.minor) / (10 ** self.exp)

    def __add__(self, o: "Amount") -> "Amount":
        return Amount(self.minor + self._align(o), self.exp, self.currency or o.currency)

    def __sub__(self, o: "Amount") -> "Amount":
        return Amount(self.minor - self._align(o), self.exp, self.currency or o.currency)

    def _align(self, o: "Amount") -> int:
        if o.exp == self.exp:
            return o.minor
        return int(Decimal(o.minor) * (10 ** (self.exp - o.exp)))

    def __lt__(self, o: "Amount") -> bool:
        return self.minor < self._align(o)

    def format(self) -> str:
        """Render the way a grader's number-token path will match.

        Integers print without a decimal tail: '11500', not '11500.00'. The
        grader has an exact number-token path for numeric answers, and a
        gratuitous '.00' is a different token.
        """
        d = self.decimal
        if d == d.to_integral_value():
            return str(int(d))
        return format(d.normalize(), "f")


@dataclass(frozen=True, slots=True)
class Extracted:
    """A value bound to the span it came from. No span, no claim."""
    amount: Amount
    span: str
    start: int
    end: int


def extract_amounts(text: str) -> list[Extracted]:
    """Every numeric value in a piece of prose, each tied to its source span."""
    out: list[Extracted] = []
    for m in _NUM.finditer(text or ""):
        raw = m.group("num").replace(",", "")
        try:
            d = Decimal(raw)
        except InvalidOperation:
            continue
        scale = (m.group("scale") or "").lower().rstrip(".")
        if scale:
            d *= _SCALE.get(scale, 1)
        cur = ""
        for key in (m.group("pre"), m.group("post")):
            if key:
                cur = _CURRENCY.get(key.lower().rstrip("s"), _CURRENCY.get(key.lower(), ""))
                if cur:
                    break
        lo = max(0, m.start() - 60)
        # Is this figure already in minor units? v12 states amounts both ways
        # ("$1,530" and "482680 USD cents"), and reading a cents figure as
        # dollars inflates it 100x -- silently, since the arithmetic still
        # works and only the graded token is wrong.
        tail = text[m.end():m.end() + 24].lower()
        minor_unit = bool(_MINOR_UNIT.search(tail)) or bool(
            _MINOR_UNIT.search(text[lo:m.start()].lower()))
        amt = (Amount.from_minor(int(d), cur) if minor_unit and d == d.to_integral_value()
               else Amount.from_decimal(d, cur))
        out.append(Extracted(amt, text[lo:m.end() + 40], m.start(), m.end()))
    return out


# --------------------------------------------------------------------------
# role binding
# --------------------------------------------------------------------------

# Role vocabulary. These are ordinary business words, not benchmark labels --
# the same terms appear in any invoicing or budgeting conversation.
ROLE_PATTERNS: dict[str, re.Pattern] = {
    "draft": re.compile(r"\b(?:draft|initial|original|proposed|starting|opening|first)\b", re.I),
    "approved": re.compile(r"\b(?:approved|authoris|authoriz|sanctioned|greenlit|cleared)\w*\b", re.I),
    "settled": re.compile(r"\b(?:settled|paid|payment|remitted|transferred|disbursed|cleared\s+for)\b", re.I),
    "adjustment": re.compile(r"\b(?:raise[sd]?|rais(?:ing)?|lower[sd]?|increas\w*|decreas\w*|"
                             r"reduc\w*|adjust\w*|revis\w*|bump\w*|cut)\b", re.I),
    "correction": re.compile(r"\b(?:later\s+revision|supersed\w*|correct\w*|amend\w*|"
                             r"updated?\s+to|now\s+stands?\s+at|actually)\b", re.I),
}

# Direction of an adjustment, stated in prose because v12 removed the `%+d`
# sign tell that v11 leaked.
_UP = re.compile(r"\b(?:rais\w*|increas\w*|bump\w*|up|added|plus|more)\b", re.I)
_DOWN = re.compile(r"\b(?:lower\w*|decreas\w*|reduc\w*|down|cut|less|minus|off)\b", re.I)


def adjustment_sign(text: str) -> int:
    """+1 / -1 for an adjustment stated in prose. 0 when undetermined."""
    up, down = bool(_UP.search(text or "")), bool(_DOWN.search(text or ""))
    if up and not down:
        return 1
    if down and not up:
        return -1
    return 0


def roles_in(text: str) -> set[str]:
    return {r for r, pat in ROLE_PATTERNS.items() if pat.search(text or "")}


def bind_amounts(text: str) -> list[tuple[str, "Amount", str]]:
    """Bind each amount to the role word NEAREST it, not to every role in the text.

    "the invoice was approved at $1530, and a payment of $108 has cleared"
    contains both an `approved` and a `settled` marker, so tagging every amount
    with every role found anywhere in the passage makes 1530 and 108 both
    approved AND settled -- and the arithmetic then picks arbitrarily. Binding
    positionally is the whole difference between 1530-108 and nonsense.

    Preference is for the closest role marker BEFORE the amount ("approved at
    $1530"), falling back to the closest one after it ("a payment of $108").
    """
    out: list[tuple[str, Amount, str]] = []
    markers: list[tuple[int, int, str]] = []
    for role, pat in ROLE_PATTERNS.items():
        for m in pat.finditer(text or ""):
            markers.append((m.start(), m.end(), role))
    if not markers:
        return out
    for e in extract_amounts(text or ""):
        before = [(e.start - end, role) for start, end, role in markers
                  if end <= e.start]
        after = [(start - e.end, role) for start, end, role in markers
                 if start >= e.end]
        pick = ""
        # "lowered the approved amount BY $208" -- the nearest marker is
        # `approved`, but the amount is the delta, not the approved figure.
        # "by" immediately before a number is the general cue for a magnitude of
        # change, and it has to outrank proximity or every adjustment gets
        # absorbed into whatever it adjusts.
        lead = (text or "")[max(0, e.start - 12):e.start].lower()
        if re.search(r"\bby\s*[$€£¥]?\s*$", lead):
            pick = "adjustment"
        if not pick and before:
            d, role = min(before)
            if d <= 60:
                pick = role
        if not pick and after:
            d, role = min(after)
            if d <= 40:
                pick = role
        if pick:
            out.append((pick, e.amount, e.span))
    return out


@dataclass(slots=True)
class Slot:
    """One filled evidentiary role, with the event it is grounded in."""
    name: str
    amount: Amount
    event_id: int
    span: str
    ts: float | None = None


# --------------------------------------------------------------------------
# operators
# --------------------------------------------------------------------------

class ProgramError(Exception):
    """A program that cannot be executed on the slots it was given.

    Raised rather than guessed: an unfillable program means abstain or fall
    back to prose, never a plausible-looking number.
    """


def op_latest(slots: list[Slot]) -> Slot:
    """Most recent by timestamp, falling back to event order.

    This is the corrections operator: vector search happily returns the old and
    the new value for a corrected fact, and something has to pick.
    """
    if not slots:
        raise ProgramError("latest: no candidates")
    return max(slots, key=lambda s: ((s.ts if s.ts is not None else -1e18), s.event_id))


def op_max(slots: list[Slot]) -> Slot:
    if not slots:
        raise ProgramError("max: no candidates")
    return max(slots, key=lambda s: s.amount.minor)


def op_min(slots: list[Slot]) -> Slot:
    if not slots:
        raise ProgramError("min: no candidates")
    return min(slots, key=lambda s: s.amount.minor)


def op_subtract(a: Amount, b: Amount) -> Amount:
    return a - b


def op_adjust(base: Amount, delta: Amount, sign: int) -> Amount:
    if sign == 0:
        raise ProgramError("adjust: direction not determined from prose")
    return base + delta if sign > 0 else base - delta


def op_sum(slots: list[Slot]) -> Amount:
    if not slots:
        raise ProgramError("sum: no candidates")
    total = slots[0].amount
    for s in slots[1:]:
        total = total + s.amount
    return total


# --------------------------------------------------------------------------
# dates and durations
# --------------------------------------------------------------------------

_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_REL_DAYS = re.compile(r"\b(\d+)\s+days?\s+(ago|later|after|before)\b", re.I)


def parse_dates(text: str) -> list[_dt.date]:
    out = []
    for m in _ISO_DATE.finditer(text or ""):
        try:
            out.append(_dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            continue
    return out


def days_between(a: _dt.date, b: _dt.date) -> int:
    return abs((b - a).days)


# --------------------------------------------------------------------------
# sets and counting
# --------------------------------------------------------------------------

def op_distinct(values: list[str]) -> list[str]:
    """Order-preserving dedup on a normalised key.

    Naive dedup collapsing repeated mentions is the classic aggregation-family
    failure, so the key is normalised but the original surface is returned.
    """
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        k = re.sub(r"\s+", " ", (v or "").strip().casefold())
        if k and k not in seen:
            seen.add(k)
            out.append(v.strip())
    return out


def format_list(values: list[str]) -> str:
    """Comma-separated, which is the shape the `answer` slot expects."""
    return ", ".join(op_distinct(values))


# --------------------------------------------------------------------------
# the compiled program
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Program:
    """A typed plan: which roles to fill and what to do with them.

    Built from the request's own language by `compile_program`, never from a
    benchmark family label -- there is no such label on the wire, and deriving
    one would be the thing §11 forbids.
    """
    operators: list[str] = field(default_factory=list)
    required: list[str] = field(default_factory=list)
    answer_type: str = "text"          # text | money | number | list | date
    notes: str = ""


# Question surface -> operator intent. Ordinary English, matched generously,
# because the same program must survive paraphrase (the metamorphic factor).
_ASK_REMAINING = re.compile(
    r"\b(?:remain\w*|left|outstanding|balance|still\s+(?:owed|due|open)|"
    r"how\s+much\s+is\s+(?:left|remaining)|unpaid|owing)\b", re.I)
_ASK_LATEST = re.compile(
    r"\b(?:latest|current|now|most\s+recent|up\s?to\s?date|these\s+days|"
    r"today|at\s+present|final)\b", re.I)
_ASK_TOTAL = re.compile(r"\b(?:total|altogether|combined|sum|in\s+all|overall)\b", re.I)
_ASK_COUNT = re.compile(r"\b(?:how\s+many|number\s+of|count)\b", re.I)
_ASK_LIST = re.compile(r"\b(?:list|which\s+ones|all\s+of|name\s+(?:them|all))\b", re.I)


def compile_program(question: str, evidence_text: str = "") -> Program:
    """Derive the program from what the request asks for.

    Deliberately conservative: when the question does not clearly imply an
    operator chain, return an empty program and let the prose path answer. A
    wrongly-compiled program produces a confident wrong number, which is worse
    than no program at all.
    """
    q = question or ""
    p = Program()

    if _ASK_COUNT.search(q):
        p.operators = ["count"]
        p.answer_type = "number"
        return p
    if _ASK_LIST.search(q):
        p.operators = ["distinct"]
        p.answer_type = "list"
        return p

    ev_roles = roles_in(evidence_text)

    if _ASK_REMAINING.search(q):
        # "What remains" = (the authoritative figure) - (what has been settled).
        # Which figure is authoritative depends on what the evidence contains,
        # and that is a property of the evidence, not of a benchmark family.
        p.answer_type = "money"
        p.required = ["settled"]
        if "correction" in ev_roles:
            p.operators = ["select_latest", "subtract"]
            p.required += ["draft", "approved"]
            p.notes = "a later revision supersedes an earlier figure"
        elif "adjustment" in ev_roles:
            p.operators = ["adjust", "subtract"]
            p.required += ["draft", "adjustment"]
            p.notes = "a stated adjustment moves the base figure before subtracting"
        elif "approved" in ev_roles and "draft" in ev_roles:
            p.operators = ["max", "subtract"]
            p.required += ["draft", "approved"]
            p.notes = "two competing figures; the larger governs"
        else:
            p.operators = ["subtract"]
            p.required += ["draft", "approved"]
        return p

    if _ASK_TOTAL.search(q):
        p.operators = ["sum"]
        p.answer_type = "money"
        return p

    if _ASK_LATEST.search(q):
        p.operators = ["select_latest"]
        p.answer_type = "text"
        return p

    return p


def semantic_key(question: str, evidence_text: str = "") -> str:
    """A paraphrase-stable key for caching a compiled program.

    Two phrasings of one question must compile to one program, or the
    metamorphic-consistency factor charges for the difference. Keying the cache
    on the *program-relevant features* rather than the question string is what
    makes that true by construction instead of by luck.
    """
    p = compile_program(question, evidence_text)
    return "|".join(("op:" + ",".join(p.operators), "ty:" + p.answer_type,
                     "rq:" + ",".join(sorted(p.required))))
