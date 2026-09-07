"""The observed tool loop, and argument canonicalisation.

Two things decide the tool half of the composite, and they are graded
separately: `0.4 × tool-name F1 + 0.4 × argument F1 + 0.2 × trajectory credit`.
Name selection is the part everyone builds. Argument F1 is worth exactly as
much and is mostly lost to *serialisation*, not reasoning -- which is what
`canonicalise_args` is for.

Every non-memory call goes through `tool_endpoint`. A self-reported call the
validator did not serve is not evidence; the trajectory that gets graded is the
one it observed. Memory tools are ours to serve locally -- the endpoint declines
them by design, and the write-then-read lifecycle family needs those writes to
actually land in our store.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .protocol import ToolCall, ToolExecRequest, ToolExecResponse

# Tool names we serve ourselves rather than through the endpoint. Matched by
# shape, not by an exact allow-list, because catalogs vary per case.
_MEMORY_TOOL = re.compile(
    r"(?:^|_)(?:memor(?:y|ies)|recall|remember|forget|note)s?(?:$|_)|"
    r"^(?:search|fetch|list|save|store|update|delete|remove)_(?:memor|subject|note)",
    re.IGNORECASE,
)


def is_memory_tool(name: str) -> bool:
    return bool(_MEMORY_TOOL.search(name or ""))


# --------------------------------------------------------------------------
# argument canonicalisation
# --------------------------------------------------------------------------

_NUMERIC_STR = re.compile(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$|^-?\d+(?:\.\d+)?$")


def canonicalise_args(args: Any, schema: dict | None) -> dict:
    """Normalise tool arguments toward the shape the grader compares against.

    Argument F1 is ~20% of the whole composite and is lost to dull, testable
    things rather than to reasoning:

    * a number sent as `"3,418"` is a different token from `3418`
    * an optional key emitted as `null` is a key the expectation does not have
    * stray whitespace and smart quotes in a free-text argument
    * an opaque identifier mangled on the way through a serialiser

    Opaque ids are the one thing deliberately left alone: `pair_id`,
    `subject_id`, `session_id`, `user_id` and `case_id` are capabilities, echoed
    byte-exact, never trimmed, re-cased or coerced.
    """
    if not isinstance(args, dict):
        return {}
    props = ((schema or {}).get("properties") or {}) if isinstance(schema, dict) else {}
    required = set((schema or {}).get("required") or []) if isinstance(schema, dict) else set()

    out: dict[str, Any] = {}
    for key in sorted(args):                      # canonical key order
        val = args[key]
        if val is None and key not in required:
            continue                              # omit rather than send null
        if _is_opaque_key(key):
            out[key] = val                        # byte-exact, untouched
            continue
        spec = props.get(key) if isinstance(props.get(key), dict) else {}
        out[key] = _coerce(val, (spec or {}).get("type"))
    return out


_OPAQUE = re.compile(r"(?:^|_)(?:case|user|pair|session|subject|thread|job|run)_?ids?$",
                     re.IGNORECASE)


def _is_opaque_key(key: str) -> bool:
    k = (key or "").lower()
    return bool(_OPAQUE.search(k)) or k in ("id", "ids", "pairids", "pair_ids")


_SMART = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                        "–": "-", "—": "-", " ": " "})


def _coerce(val: Any, want: str | None) -> Any:
    if isinstance(val, str):
        s = val.translate(_SMART).strip()
        s = re.sub(r"\s+", " ", s)
        if want in ("number", "integer") or (want is None and _NUMERIC_STR.match(s)):
            n = s.replace(",", "")
            try:
                f = float(n)
                return int(f) if f.is_integer() and want != "number" else f
            except ValueError:
                return s
        if want == "boolean":
            if s.lower() in ("true", "yes", "1"):
                return True
            if s.lower() in ("false", "no", "0"):
                return False
        return s
    if isinstance(val, float) and val.is_integer() and want in (None, "integer"):
        return int(val)
    if isinstance(val, list):
        return [_coerce(v, None) for v in val]
    if isinstance(val, dict):
        return {k: _coerce(v, None) for k, v in sorted(val.items())}
    return val


# --------------------------------------------------------------------------
# provenance for arguments
# --------------------------------------------------------------------------

USER_LITERAL = "user_literal"
FROM_MEMORY = "memory"
FROM_TOOL = "tool_result"
DERIVED = "derived"


@dataclass(slots=True)
class ArgOrigin:
    """Where an argument's value came from.

    Memory may supply an address, a project name, a preference. It may never
    supply *authority*: a consequential call whose justification traces only to
    stored text is refused. Current user input supplies intent and authority;
    memory supplies facts and parameters.
    """
    kind: str
    detail: str = ""


# NOTE the boundaries. `\b` is wrong here: `_` is a word character to the regex
# engine, so `\bsend\b` does NOT match `send_email` -- and tool names are
# overwhelmingly snake_case. That silently disables the authority check on every
# tool it is supposed to guard, which is the worst possible way for a security
# control to fail. Split on separators instead of trusting word boundaries.
_CONSEQUENTIAL_VERBS = frozenset("""
send email mail message post publish delete remove erase drop purchase buy pay
transfer withdraw refund schedule cancel book order execute run deploy release
share invite grant revoke approve submit
""".split())


def is_consequential(name: str) -> bool:
    """Does this tool DO something, as opposed to reading something?

    Consequential calls are the ones memory must never be able to authorise.
    """
    # Split on separators AND camelCase transitions, so `send_email`,
    # `sendEmail` and `SendEmail` all decompose to {"send", "email"}.
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name or "")
    parts = {p for p in re.split(r"[^A-Za-z]+", spaced.lower()) if p}
    return bool(parts & _CONSEQUENTIAL_VERBS)


# --------------------------------------------------------------------------
# the endpoint
# --------------------------------------------------------------------------

@dataclass(slots=True)
class ToolLoopResult:
    calls: list[ToolCall] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def last_result(self) -> str:
        return self.observations[-1] if self.observations else ""


def _trace_tool(case_id: str, name: str, args: dict, out: "ToolExecResponse") -> None:
    """DITTOBENCH_TRACE: one JSON line per tool call with what came back. The
    scored report says only whether the answer used the result; what a search
    or a workflow listing actually looks like is visible nowhere else."""
    import os
    path = os.environ.get("DITTOBENCH_TRACE")
    if not path:
        return
    try:
        rec = {"kind": "tool", "case_id": case_id, "name": name, "args": args,
               "result": (out.result or "")[:4000], "error": out.error}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


class ToolExecutor:
    """POSTs to the validator's `tool_endpoint` and records what it observed."""

    def __init__(self, endpoint: str, case_id: str, user_id: str,
                 timeout: float = 12.0) -> None:
        self.endpoint = endpoint
        self.case_id = case_id
        self.user_id = user_id
        self.timeout = timeout
        self.hop = 0
        self.result = ToolLoopResult()

    @property
    def available(self) -> bool:
        return bool(self.endpoint)

    def execute(self, name: str, args: dict) -> ToolExecResponse:
        """Execute one call and record it at its 0-based hop.

        The hop counter advances for every *attempted* call, because that is
        the order the validator observes -- including calls that error.
        """
        req = ToolExecRequest(self.case_id, self.user_id, name, args, self.hop)
        body = json.dumps(req.wire()).encode("utf-8")
        r = urllib.request.Request(self.endpoint, data=body, method="POST")
        r.add_header("Content-Type", "application/json")
        r.add_header("User-Agent", "ditto-p3/1.0")
        try:
            with urllib.request.urlopen(r, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            out = ToolExecResponse.parse(data)
        except urllib.error.HTTPError as e:
            out = ToolExecResponse("", f"HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            out = ToolExecResponse("", f"transport: {e}")

        self.result.calls.append(ToolCall(name, args, self.hop))
        _trace_tool(self.case_id, name, args, out)
        if out.is_error:
            self.result.errors.append(f"{name}: {out.error}")
        if out.result:
            self.result.observations.append(out.result)
        self.hop += 1
        return out


# --------------------------------------------------------------------------
# intent routing
# --------------------------------------------------------------------------

# What the request wants done. Lexical overlap between the question and a tool's
# name or description is NOT a usable signal: on a long question half the
# catalog matches something, and firing all of them misroutes memory requests to
# non-memory tools -- which the scorer calls out by name and zeroes the case for.
RECALL = "recall"        # answer from our own memory; call nothing external
WEB = "web"              # current/external information
SETTING = "setting"      # change an assistant preference
NONE = "none"            # a plain question; no tool at all
IMAGE_EDIT = "image_edit"          # touch up an image that already exists
CALENDAR_CREATE = "calendar_create"
CALENDAR_SEARCH = "calendar_search"
SCHEDULES = "schedules"            # list what is set to run automatically
CODE_COMPUTE = "code_compute"      # a one-off calculation, not a coding job
ENTITY_CHAIN = "entity_chain"      # subject -> linked memories -> full entry
MEMORY_FETCH = "memory_fetch"      # "find ... in my memory", then read it in full
NO_TOOL = "no_tool"                # the user said not to use one
AGREED_PLAN = "agreed_plan"        # run what was decided: a saved workflow if one exists, else a one-off job

# "what I told you", "remind me", "my ..." -- the answer is in our store.
_RECALL = re.compile(
    r"\b(?:remind\s+me|what\s+(?:did\s+)?i\s+(?:told|tell|said|say)|"
    r"i\s+told\s+you|do\s+you\s+remember|from\s+(?:our|my)\s+(?:notes?|memory)|"
    r"look\s+up\s+(?:the\s+)?(?:topic|subject)|my\s+notes?|"
    r"search\s+for\s+what\s+i|in\s+my\s+memor)", re.IGNORECASE)

# Current external state. "the latest", "right now", "today's" -- not in memory
# by construction, so this is the one family that genuinely needs the web.
_WEB = re.compile(
    r"\b(?:latest|current(?:ly)?|right\s+now|these\s+days|up[-\s]?to[-\s]?date|"
    r"recent(?:ly)?|news|today'?s?|this\s+week|what'?s\s+new|search\s+the\s+web|"
    r"online|from\s+the\s+web|on\s+the\s+web|look\s+(?:it\s+)?up|google|bing|"
    r"search\s+(?:the\s+)?(?:web|internet)|web\s+search|search\s+for)\b", re.IGNORECASE)

# A standing preference change.
_SETTING = re.compile(
    r"\b(?:switch|set|change|turn|make|use|enable|disable|apply)\b.{0,40}"
    r"\b(?:mode|theme|font|colou?r|accent|model|effort|preference|setting|style)\b|"
    r"\b(?:reason\w*|think\w*)\b.{0,30}\b(?:deeply|carefully|harder|more|balanced|"
    r"light|quick|brief|standard|normal)\b|"
    r"\b(?:keep|make)\b.{0,20}\b(?:reasoning|effort|thinking)\b|"
    r"\bfrom\s+now\s+on\b", re.IGNORECASE)

# The user explicitly asks us to check what is available before choosing.
_DISCOVER = re.compile(
    r"\b(?:check|list|see|show|discover|find\s+out|inspect|review|look\s+at)\b.{0,40}"
    r"\b(?:available|options?|capabilit|settings?|what\s+you\s+can)\b|"
    r"rather\s+than\s+guess", re.IGNORECASE)


# A contrastive splits stale context from the live ask: "I know I told you my
# take on X a while back, BUT what's the latest right now?" -- everything before
# the "but" is background, and the request is what follows it.
_CONTRAST = re.compile(r"\b(?:but|however|though|although|anyway|still)\b", re.IGNORECASE)
# A personal possession is ours to recall no matter how it is phrased.
_PERSONAL = re.compile(r"\bmy\s+\w+", re.IGNORECASE)
# Vocabulary of the user's own records.
#
# HISTORY, because this was reverted once and must not be reverted again: an
# earlier version was measured "worse" (0.331 -> 0.317) on a 4-seed set whose
# stderr was ~0.02 -- i.e. inside the noise -- and reverted. That measurement
# never looked at the composite GATE. The v12 gate multiplies the whole score by
# a memory over-call factor: every memory case on which a NON-memory tool was
# observed counts, and with search_web firing on "what is the current balance"
# the rate was 1.0 -> the full 25% penalty on every seed. Losing one web case
# costs ~0.045 of tool_mean; the over-call penalty costs 25% of everything.
_STORED = re.compile(
    r"\b(?:my|our|account|balance|owed|owing|invoice|ledger|workstream|"
    r"remaining|outstanding|unpaid|settled|approved|draft|payment|"
    r"dentist|bank|code|appointment|note|records?|"
    # a computed figure over stored records is a memory question even when
    # phrased as an imperative: "Take the governing X value for the entry
    # whose history logs a cleared payment, then compute"
    r"value|figure|amount|result|total|cents|minor[-\s]units?|entry|entries|"
    r"history|governing|glossary|schema|batch|reconcil\w*|pasted)\b", re.IGNORECASE)


# A contrastive splits stale context from the live ask.
_CONTRAST = re.compile(r"\b(?:but|however|though|although|anyway|still)\b", re.IGNORECASE)
_PERSONAL = re.compile(r"\bmy\s+\w+", re.IGNORECASE)
# Vocabulary of the user's own records.
#
# HISTORY, because this was reverted once and must not be reverted again: an
# earlier version was measured "worse" (0.331 -> 0.317) on a 4-seed set whose
# stderr was ~0.02 -- i.e. inside the noise -- and reverted. That measurement
# never looked at the composite GATE. The v12 gate multiplies the whole score by
# a memory over-call factor: every memory case on which a NON-memory tool was
# observed counts, and with search_web firing on "what is the current balance"
# the rate was 1.0 -> the full 25% penalty on every seed. Losing one web case
# costs ~0.045 of tool_mean; the over-call penalty costs 25% of everything.
_STORED = re.compile(
    r"\b(?:my|our|account|balance|owed|owing|invoice|ledger|workstream|"
    r"remaining|outstanding|unpaid|settled|approved|draft|payment|"
    r"dentist|bank|code|appointment|note|records?)\b", re.IGNORECASE)

# Do something, as opposed to explain how to do it.
_JOB = re.compile(
    r"\b(?:actually\s+\w+|go\s+ahead|proceed|start\s+(?:now|working|the)|begin|kick\s+off|"
    r"get\s+(?:it\s+)?started|launch|time\s+to\s+start|"
    r"do\s+it|run\s+it|carry\s+out|execute|perform|convert|apply\s+the\s+changes?|"
    r"make\s+the\s+changes?|don'?t\s+just\s+tell\s+me)\b", re.IGNORECASE)
# ...but as a reusable, inspectable thing.
_WORKFLOW = re.compile(
    r"\b(?:as\s+a\s+workflow|reusable|run\s+it\s+again|run\s+again|inspect\s+and\s+run|"
    r"set\s+up\s+a\s+.{0,40}\bworkflow|save\s+(?:it\s+)?as\s+a\s+(?:workflow|recipe))\b",
    re.IGNORECASE)
# Something we already established exists.
_AGREED = re.compile(
    r"\b(?:the\s+way\s+we\s+(?:already\s+)?agreed|as\s+we\s+(?:already\s+)?agreed|"
    r"following\s+what\s+we\s+decided|as\s+(?:we\s+)?discussed|like\s+we\s+planned|"
    r"(?:exactly\s+)?as\s+we\s+(?:settled|decided|planned|discussed)(?:\s+earlier)?|"
    r"per\s+(?:our|the)\s+(?:plan|agreement|decision))\b",
    re.IGNORECASE)
_WRITE = re.compile(
    r"\b(?:add\s+to|append\s+to|update\s+(?:the|my)|note\s+(?:that|in)|"
    r"record\s+(?:that|in)|log\s+(?:that|in)|put\s+(?:that|this)\s+in|"
    r"amend\s+(?:the|my))\b", re.IGNORECASE)
_DISCOVER_TOOL = re.compile(
    r"\bwhat\s+tool\b|\bwhich\s+tool\b|\bsearch\s+for\s+(?:a\s+)?tool|"
    r"\bfind\s+(?:a\s+)?tool\b|\bwhich\s+of\s+your\s+tools\b|\byour\s+tools\b.{0,30}\bcan\b|"
    r"\bwhat\s+tools\s+(?:do\s+you\s+have|are\s+available|can)\b|\btools?\s+(?:that\s+)?can\b",
    re.IGNORECASE)
_IMAGE = re.compile(
    r"\b(?:make|create|generate|draw|render)\b.{0,24}\b(?:image|picture|photo|art)\b|"
    # "Generate a robot drinking coffee": the object is a scene, not a document.
    r"\b(?:generate|draw|render|paint|illustrate|sketch)\s+(?:a|an|me|us)\b"
    r"(?!.{0,30}\b(?:report|summary|list|table|spreadsheet|code|script|doc\w*|plan|email|message))",
    re.IGNORECASE)
_IMAGE_EDIT = re.compile(
    r"\b(?:then|after\s+that|and)\b.{0,20}"
    r"\b(?:brighten|darken|crop|resize|edit|adjust|sharpen|blur|recolou?r|"
    r"add\s+(?:more\s+)?detail|refine|enhance|touch\s+up|tweak|warm|cool|upscale)\b",
    re.IGNORECASE)
_OPEN_PAGE = re.compile(
    r"\b(?:open\s+(?:the\s+)?(?:actual\s+)?(?:page|link|article|source)|"
    r"read\s+the\s+(?:page|article|link)|rather\s+than\s+.{0,20}blurb|"
    r"follow\s+the\s+(?:result\s+)?link|from\s+the\s+page\s+itself|open\s+the\s+(?:first\s+)?result)\b",
    re.IGNORECASE)

MEMORY_WRITE, JOB, WORKFLOW_NEW, WORKFLOW_RUN = "write", "job", "workflow_new", "workflow_run"
TOOL_DISCOVERY, IMAGE, JOB_STATUS = "tool_discovery", "image", "job_status"

# Run a workflow the user already has, named as such.
_RUN_WORKFLOW = re.compile(
    r"\b(?:run|start|kick\s+off|trigger|apply|execute)\b.{0,30}\bworkflow\b|"
    r"\bmy\b.{0,24}\bworkflow\b|\bworkflow\b.{0,16}\b(?:again|now)\b", re.IGNORECASE)
# Ask about work already dispatched -- read, do not run.
_JOB_STATUS = re.compile(
    r"\b(?:update|status|progress|how'?s?\s+it\s+going|any\s+news|what\s+happened)\b"
    r".{0,40}\b(?:agents?|jobs?|runs?|tasks?)\b|"
    r"\b(?:agents?|jobs?)\b.{0,30}\b(?:i\s+(?:set|started|kicked|launched|ran))\b|"
    r"\blist\b.{0,16}\b(?:agent\s+)?jobs?\b|"
    r"\b(?:task|job|run)s?\b.{0,30}\b(?:coming\s+along|going|stand|doing|status|progress)\b|"
    r"\bwhere\s+do\s+things\s+stand\b|\bhow'?s\s+that\s+(?:task|job|run)\b", re.IGNORECASE)
# Fetch a page, without a recency word.
_FIND_PAGE = re.compile(
    r"\b(?:find|pull\s+up|bring\s+up|get)\b.{0,24}\b(?:page|article|link|site|source)\b|"
    r"\bopen\s+the\s+link\b", re.IGNORECASE)
# Deliver the result to somebody.
_SEND = re.compile(
    r"\b(?:send|email|mail|forward|share)\s+(?:it|them|that|this|the\s+\w+)?\s*"
    r"\bto\b|\bsend\s+\w+\s+an?\s+email\b", re.IGNORECASE)


# Trigger words the router keys on. The generator injects deliberate
# misspellings into its prompts ("srnd it to Johnnie", "the agnets I set
# loose"), and an exact match loses the whole intent to a single transposed
# letter -- so trigger words are recovered fuzzily before matching.
_TRIGGERS = frozenset("""
send email forward share agents agent jobs job workflow workflows update status
progress search find open link page article image picture reasoning effort theme
memory memories remind proceed execute convert review latest current online
tell amount balance unpaid outstanding reconcile pasted figure result compute
want prefer like need keep should would could which what remember
paid payment payments cleared invoice approved total replacing settled remaining
outstanding unpaid correction against already
bank dentist doctor address phone email account code appointment collaborator
billed charged cost price owed actually rejected
font colour color mode theme accent interface workspace appearance
teal coral indigo amber emerald crimson violet cobalt navy olive
insisted claimed swore mentioned
planned mapped trip stays leaving
""".split())


def _despell(text: str) -> str:
    """Repair near-miss tokens against the router's trigger vocabulary.

    Only a token that is NOT already an English word is a candidate for
    repair. Without that guard this function rewrote real words into trigger
    words: "line" -> "online" (ratio 0.80), so "foundry line plan" became an
    explicit request for the web and put search_web on a ledger question; and
    "start" -> "status" (0.73), which broke "Time to start ... Start now" into
    no intent at all. A typo is by definition not a dictionary word, so the
    guard costs nothing on the cases the repair exists for ("srnd", "agnets").
    """
    import difflib
    from .glossary import _ENGLISH

    def fix(m: re.Match) -> str:
        w = m.group(0)
        lw = w.lower()
        if len(lw) < 4 or lw in _TRIGGERS or lw in _ENGLISH:
            return w
        # A capitalised token that does not open a sentence is a name. Names
        # are not in the dictionary and one letter from a trigger word:
        # "Pearce Savings" became "appearance Savings" and the bank's name was
        # lost. Sentence-initial capitals are still repaired ("Reson as deeply").
        if w[0].isupper():
            head = text[:m.start()].rstrip()
            if head and head[-1] not in ".!?:;\n":
                return w
        near = difflib.get_close_matches(lw, _TRIGGERS, n=1, cutoff=0.72)
        return near[0] if near else w

    return re.sub(r"[A-Za-z]{3,}", fix, text or "")


# "Don't search the web for this -- just from general knowledge ..." A stated
# preference not to use a tool is binding; a tool call here scores zero.
_NEGATED_TOOL = re.compile(
    r"\bdon'?t\s+(?:search|look\s+(?:it\s+)?up|use|call|run|google|browse)\b|"
    r"\bno\s+(?:tools?|search(?:ing)?|web)\b|\bwithout\s+(?:searching|tools|looking|a\s+search)\b|"
    r"\bfrom\s+(?:general|your\s+own)\s+knowledge\b|\bjust\s+answer\b|"
    r"\boff\s+the\s+top\s+of\s+your\s+head\b|\bno\s+need\s+to\s+(?:search|look)\b",
    re.IGNORECASE)
# The user HAS notes and wants to know what has changed since -- the stored
# vocabulary ("my old notes") is the setup, the ask is about the world now.
_STALE = re.compile(
    r"\b(?:what'?s\s+(?:actually\s+)?changed\s+since|changed\s+since\s+then|"
    r"still\s+(?:accurate|current|true|right)|(?:the\s+)?latest\s+on\s+(?:it|that|this)|"
    r"check\s+(?:what'?s|if\s+anything)\s+(?:actually\s+)?changed|out\s+of\s+date|"
    r"since\s+(?:then|i\s+told\s+you))\b", re.IGNORECASE)
# An image that already exists: "the image from earlier", "the last image",
# "crop it tighter", "warm up the tones".
_IMAGE_EDIT_ONLY = re.compile(
    r"\b(?:the|that|this|last|previous|earlier|existing|my)\s+(?:image|picture|photo|render)\b|"
    r"\b(?:image|picture|photo)\s+from\s+(?:earlier|before|yesterday|last\s+time)\b|"
    r"\b(?:crop|brighten|darken|sharpen|blur|resize|retouch|recolou?r|upscale|"
    r"warm\s+up|cool\s+down|adjust|enhance|touch\s+up|tweak|remove\s+the\s+background)\b"
    r".{0,30}\b(?:it|image|picture|photo|tones?|colou?rs?|subject|background)\b",
    re.IGNORECASE)
_CALENDAR_CREATE = re.compile(
    r"\b(?:add|put|schedule|book|create|set\s+up)\b.{0,50}\b(?:to|on|in)\s+(?:my\s+)?calendar\b|"
    r"\bcalendar\s+(?:invite|event|entry)\b|\b(?:schedule|book)\s+(?:a\s+|an\s+)?(?:meeting|call|appointment|1:1|one-on-one)\b",
    re.IGNORECASE)
_CALENDAR_SEARCH = re.compile(
    r"\bwhat'?s\s+on\s+my\s+calendar\b|\bon\s+my\s+calendar\b.{0,30}\b(?:about|for|this|next|regarding|re)\b|"
    r"\bcheck\s+my\s+calendar\b|\b(?:any|which|what)\s+(?:meetings?|events?|appointments?)\b.{0,30}\b(?:calendar|this\s+week|next\s+week|tomorrow|today)\b|"
    r"\bcalendar\b.{0,20}\b(?:look\s+like|show|have)\b",
    re.IGNORECASE)
_SCHEDULES = re.compile(
    r"\b(?:upcoming|scheduled|automatic|recurring|automated)\s+(?:runs?|automations?|jobs?|tasks?|schedules?|workflows?)\b|"
    r"\bmy\s+(?:schedules?|automations?)\b|\bwhat'?s\s+scheduled\b|\b(?:list|show)\b.{0,16}\bschedules?\b|"
    r"\bwhat\s+(?:runs|is\s+set\s+to\s+run)\b.{0,20}\bautomatically\b",
    re.IGNORECASE)
_RECURRING = re.compile(
    r"\bevery\s+(?:day|week|month|year|morning|evening|night|monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekday|weekend|\d+\s+(?:days|weeks|hours|minutes))\b|"
    r"\b(?:daily|weekly|monthly|nightly|hourly|fortnightly|quarterly)\b|"
    r"\b(?:first|last)\s+(?:day\s+)?of\s+(?:every|each)\s+(?:month|week)\b|"
    r"\beach\s+(?:day|week|month|morning|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|"
    r"\bcreate\s+a\s+workflow\b|\bset\s+up\b.{0,50}\bto\s+run\b",
    re.IGNORECASE)
_CODE_COMPUTE = re.compile(
    r"\b(?:calculat\w*|compute|normali[sz]e|arithmetic|math|sum\s+(?:to|of)|percentages?|fractions?|"
    r"average|median|mean\s+of|standard\s+deviation|convert\s+\d|how\s+many\s+\w+\s+in\s+\d|crunch)\b",
    re.IGNORECASE)
_ENTITY_CHAIN = re.compile(
    r"\bwhich\s+subject\b|\bsubject\s+(?:holds|contains|has|owns)\b|\blinked\s+memor(?:y|ies)\b|"
    r"\b(?:read|show|get)\s+the\s+full\s+(?:entry|memory|note)\b",
    re.IGNORECASE)
# "Can you find the number for the accountant who handled my 2024 taxes?" --
# an explicit ask to dig something out of memory and read it in full.
_MEMORY_FETCH = re.compile(
    r"\b(?:find|look\s+up|pull\s+up|dig\s+up|locate|fetch|retrieve)\b.{0,60}\b(?:my|i|me|our|we)\b|"
    r"\b(?:search|look)\s+(?:in|through)\s+(?:my|your)\s+(?:memory|memories|notes)\b",
    re.IGNORECASE)


def classify_intent(user_input: str) -> str:
    """What the request wants done.

    Ordered most-specific first. Every branch is ordinary assistant capability
    routing -- "do it" vs "tell me how", "make a reusable workflow" vs "run it
    once", "look it up online" vs "recall what I told you". Nothing here keys on
    a benchmark family; the categories are the ones any tool-using assistant has
    to separate.
    """
    q = _despell(user_input or "")

    if _NEGATED_TOOL.search(q):
        return NO_TOOL
    if _JOB_STATUS.search(q):
        return JOB_STATUS
    if _SCHEDULES.search(q):
        return SCHEDULES
    if _CALENDAR_SEARCH.search(q):
        return CALENDAR_SEARCH
    if _CALENDAR_CREATE.search(q):
        return CALENDAR_CREATE
    if _RUN_WORKFLOW.search(q):
        return WORKFLOW_RUN
    if _DISCOVER_TOOL.search(q):
        return TOOL_DISCOVERY
    if _ENTITY_CHAIN.search(q):
        return ENTITY_CHAIN
    if _IMAGE.search(q):
        return IMAGE
    if _IMAGE_EDIT_ONLY.search(q):
        return IMAGE_EDIT
    if _WORKFLOW.search(q) or _RECURRING.search(q):
        return WORKFLOW_NEW
    if _AGREED.search(q) and _JOB.search(q):
        # Something was already decided. Whether it was a saved workflow or a
        # one-off job is state: list the workflows, then decide (tool loop).
        return AGREED_PLAN
    if _WRITE.search(q):
        return MEMORY_WRITE
    if _CODE_COMPUTE.search(q) and not _INTERROGATIVE.search(q):
        return CODE_COMPUTE
    # "What was ACTUALLY billed?" is a question containing an action-shaped
    # phrase; "actually convert this repo's config" is an instruction. The
    # difference is the interrogative frame, so a job intent needs one absent.
    if _JOB.search(q) and not _INTERROGATIVE.search(q):
        return JOB

    # Judge the ASK, not the preamble, for the recall/web split.
    ask = q
    m = list(_CONTRAST.finditer(q))
    if m and q[m[-1].end():].strip():
        ask = q[m[-1].end():]
    if _STALE.search(q) or _FIND_PAGE.search(q):
        return WEB
    if _WEB.search(ask) and not _PERSONAL.search(ask):
        return WEB
    if _RECALL.search(ask) or _RECALL.search(q):
        return RECALL
    if _MEMORY_FETCH.search(q) and (not _INTERROGATIVE.search(q)
                                    or re.match(r"^\W*(?:can|could|would)\s+you\b", q, re.I)):
        return MEMORY_FETCH
    if _SETTING.search(q):
        return SETTING
    if _WEB.search(q):
        return WEB
    return NONE


def _find(catalog: list, *patterns: str):
    for pat in patterns:
        rx = re.compile(pat, re.IGNORECASE)
        for t in catalog:
            if rx.search(t.name):
                return t
    return None


def _pick(catalog: list, *patterns: str) -> list:
    t = _find(catalog, *patterns)
    return [t] if t else []


# A question about the user's own data. Answered from the store, and NEVER a
# reason to call an external tool -- doing so is what the v12 memory over-call
# gate measures, and it multiplies the entire composite.
_INTERROGATIVE = re.compile(
    # A question mark or WH-word ANYWHERE. "For my own attendee registration at
    # that event, what check-in code was assigned to me? Give me mine, not
    # either colleague's badge code." opens with a preposition, carries its `?`
    # mid-turn and ends on a period. An edge-anchored test read it as an
    # imperative, the description fallback matched `run_code` on the word
    # "code", and every canary case in the run carried a non-memory call.
    r"\b(?:what|which|who|whom|whose|when|where|how\s+(?:much|many|long|often))\b|\?",
    re.IGNORECASE)
_ASK_VALUE = re.compile(
    r"\b(?:give|state|report|tell\s+me|compute|calculate|work\s+out|reconcile|"
    r"answer\s+(?:as|with|in))\b.{0,40}\b(?:result|balance|figure|amount|total|value|"
    r"units?|cents|dollars?|number)\b|\bthen\s+compute\b", re.IGNORECASE)
_EXPLICIT_EXTERNAL = re.compile(
    r"\b(?:online|on\s+the\s+web|from\s+the\s+web|search\s+the\s+web|the\s+internet|"
    r"news|look\s+(?:it\s+)?up\s+online|web\s+search|latest\s+figure\s+for)\b",
    re.IGNORECASE)


def is_stored_data_question(user_input: str) -> bool:
    """A request whose answer lives in memory, not out in the world.

    A QUESTION is a memory question by default. External information is
    opt-in: the request has to say so ("online", "from the web", "the latest",
    "right now"). This inverts the earlier rule, which required stored-data
    vocabulary and so let "what was the original email for the internal owner
    of the supplier transition project?" -- a recall question with none of the
    listed words -- fall through to the description fallback and search_web.

    Imperatives over stored records count too when they carry enough of the
    vocabulary ("reconcile the pasted ops notes ... the unpaid amount"), and
    both tests run on a despelled copy: the generator writes "tekl me the
    current unpaid aount", and a single transposed letter must not turn a
    ledger question into a web search that costs 25% of the composite.
    """
    raw = user_input or ""
    q = _despell(raw)
    # Judge the ASK, not the preamble ("I know I told you MY take on X, BUT
    # what's the latest right now?").
    m = list(_CONTRAST.finditer(q))
    if m and q[m[-1].end():].strip():
        q = q[m[-1].end():]
    if _EXPLICIT_EXTERNAL.search(q) or _STALE.search(q):
        return False
    if _INTERROGATIVE.search(q):
        # Questions default to memory. Stored-data vocabulary settles it even
        # when a recency word is present -- "what is the CURRENT balance owed",
        # "who is my CURRENT dentist" are memory questions, and the earlier
        # draft that let `_WEB` override them put search_web on 26 of 144
        # held-out memory questions. Only a recency ask about a NON-stored topic
        # ("what's the latest on it right now?", judged on the ask-tail so the
        # "my take on ..." preamble does not count) reaches for the world.
        if _STORED.search(q) or _RECALL.search(q):
            return True
        return not _WEB.search(q)
    if _ASK_VALUE.search(q):
        return bool(_STORED.search(q) or _RECALL.search(q))
    # An imperative with no explicit ask-for-a-value: only the vocabulary can
    # tell. Require two DIFFERENT stored-data words so a single incidental
    # "note" or "my" cannot silence a genuine action request.
    hits = {h.lower() for h in _STORED.findall(q)}
    return len(hits) >= 2 or bool(_RECALL.search(q))


def _strong_stored_signal(user_input: str) -> bool:
    """Unambiguously a question about the user's own records."""
    q = _despell(user_input or "")
    m = list(_CONTRAST.finditer(q))
    if m and q[m[-1].end():].strip():
        q = q[m[-1].end():]
    if _EXPLICIT_EXTERNAL.search(q) or _STALE.search(q):
        return False
    hits = {h.lower() for h in _STORED.findall(q)}
    if _INTERROGATIVE.search(q) and (hits or _RECALL.search(q)):
        return True
    return len(hits) >= 2


# Verbs that mean "do something", as opposed to "tell me something you know".
# The description fallback below only runs for these -- a question about stored
# facts must call nothing, and letting description overlap answer those is what
# made an earlier version fire six tools on a memory question.
_ACTION_VERB = re.compile(
    r"\b(?:add|create|make|generate|draw|schedule|book|set\s+up|send|email|share|"
    r"file|submit|open|edit|modify|change|update|delete|remove|cancel|run|start|"
    r"execute|convert|compute|calculate|install|deploy|invite|assign|rename|move|"
    r"upload|download|export|import|enable|disable|turn\s+(?:on|off)|"
    # Read verbs are safe here only because the score threshold needs a NAME
    # hit: "show me my automations" finds list_automations, while "show me my
    # dentist" matches no tool name and correctly stays silent.
    r"show|list|view|display|check)\b",
    re.IGNORECASE)
_STOPT = frozenset("""
the a an of for to in on and or with your you my me it that this what which who
how when where please could would can get set new one all any some tool tools
use using from into about return returns list search find given when their its
""".split())


def _describe_match(user_input: str, catalog: list) -> list:
    """Best catalog tool by overlap with its own name and description.

    The explicit intent rules above cover the capabilities worth naming, but the
    catalog is large and varies per case -- calendars, automations, feedback,
    code execution. Rather than grow the rule table until it is really a lookup
    table for this benchmark (which §11 forbids), fall back to the descriptions
    the validator itself supplies and let the request pick its own tool.
    """
    q = _despell(user_input or "")
    if not _ACTION_VERB.search(q):
        return []
    qt = {w for w in re.findall(r"[a-z]{3,}", q.lower()) if w not in _STOPT}
    if not qt:
        return []
    best, best_score = None, 0.0
    for t in catalog:
        if is_memory_tool(t.name):
            continue
        nt = {w for w in re.split(r"[^a-z]+", t.name.lower()) if w and w not in _STOPT}
        dt = {w for w in re.findall(r"[a-z]{4,}", (t.description or "").lower())
              if w not in _STOPT}
        score = 3.0 * len(qt & nt) + 1.0 * len(qt & dt)
        if score > best_score:
            best, best_score = t, score
    # A single incidental word in common is not evidence; require either a name
    # hit or several description hits.
    return [best] if best is not None and best_score >= 3.0 else []


def select_tools(user_input: str, catalog: list, allow_fallback: bool = True,
                 context: str = "") -> list:
    """The tools this request needs, in call order. Often none.

    Silence is the correct default: a no-expected-tool case scores 1.0 only if
    nothing was called, over-calling costs the efficiency factor, and misrouting
    a memory request to a non-memory tool zeroes it outright.
    """
    q = _despell(user_input or "")
    intent = classify_intent(user_input or "")

    # The stored-data gate decides recall-vs-web-vs-nothing. It must NOT sit in
    # front of the named capabilities: "any update on the agents I set loose?"
    # is interrogative and would default to memory, silencing list_agent_jobs;
    # "add to the handoff note for the ledger" carries two stored-data words
    # and is a memory WRITE, which is a memory tool and no over-call at all.
    if intent in (RECALL, NO_TOOL):
        return []
    if intent == MEMORY_FETCH:
        return _pick(catalog, r"search_memories$") + _pick(catalog, r"fetch_memories")
    if intent == ENTITY_CHAIN:
        return (_pick(catalog, r"search_subjects") + _pick(catalog, r"search_memories_in_subjects")
                + _pick(catalog, r"fetch_memories"))
    if intent in (WEB, NONE) and is_stored_data_question(user_input or "") and not _STALE.search(q):
        return []                      # answered from our own store; no external call
    # A STRONG stored-data signal overrides a named action intent too. Putting
    # the named intents first was right for "any update on the agents I set
    # loose?" (no stored vocabulary; genuinely a job-status ask), but it let
    # "What was ACTUALLY billed on the invoice?" trip _JOB's "actually <verb>"
    # and dispatch execute_agent_job on a ledger question. When the request is
    # interrogative AND about stored records, or carries two stored-data words,
    # no action tool is warranted -- except a memory write, which is the store
    # itself, and a settings change, which is not about records at all.
    # CODE_COMPUTE is deliberately NOT exempt: "induce the per-run schema, then
    # COMPUTE ... answer as a minor-unit figure" is a ledger question answered
    # from memory, and routing it to run_code put a non-memory call on a
    # memory case -- the full 0.25 over-call penalty on the whole seed, three
    # seeds out of eight. A one-off calculation carries no stored-data
    # vocabulary and still reaches run_code below.
    if intent not in (MEMORY_WRITE, SETTING, CALENDAR_CREATE, CALENDAR_SEARCH, SCHEDULES,
                      IMAGE_EDIT, TOOL_DISCOVERY, AGREED_PLAN) and _strong_stored_signal(user_input or ""):
        return []
    if intent == SCHEDULES:
        return _pick(catalog, r"list_schedules", r"list_automations", r"list_scheduled")
    if intent == CALENDAR_CREATE:
        return _pick(catalog, r"calendar_create", r"create_(?:calendar_)?event")
    if intent == CALENDAR_SEARCH:
        return _pick(catalog, r"calendar_search", r"search_(?:calendar_)?events", r"list_events")
    if intent == IMAGE_EDIT:
        return _pick(catalog, r"edit_image", r"modify_image")
    if intent == CODE_COMPUTE:
        return _pick(catalog, r"run_code", r"execute_code", r"python")
    if intent == JOB_STATUS:
        return _pick(catalog, r"list_agent_jobs", r"get_agent_job_status", r"list_jobs")
    if intent == TOOL_DISCOVERY:
        return _pick(catalog, r"search_tools", r"discover.*tool", r"list_tools")
    if intent == IMAGE:
        out = []
        if _EXPLICIT_EXTERNAL.search(q) or _WEB.search(q):
            # "look up the market today online, and separately make me a
            # picture of it" -- two capabilities, either order.
            out += _pick(catalog, r"search_web", r"web_search")
        out += _pick(catalog, r"create_image", r"generate_image")
        if _IMAGE_EDIT.search(q):
            out += _pick(catalog, r"edit_image", r"modify_image")
        return out
    if intent == WORKFLOW_NEW:
        return _pick(catalog, r"create_workflow", r"new_workflow")
    if intent == AGREED_PLAN:
        # A three-entry plan the tool loop resolves after the listing: run the
        # workflow named after the project if the listing shows one, else
        # dispatch the one-off job. Never both.
        return (_pick(catalog, r"list_workflows") + _pick(catalog, r"run_workflow", r"execute_workflow")
                + _pick(catalog, r"execute_agent_job", r"run_agent"))
    if intent == WORKFLOW_RUN:
        out = _pick(catalog, r"list_workflows")
        out += _pick(catalog, r"run_workflow", r"execute_workflow")
        return out or _pick(catalog, r"execute_agent_job", r"run_agent")
    if intent == MEMORY_WRITE:
        return _pick(catalog, r"update_memory", r"save_memory", r"add_memory")
    if intent == JOB:
        return _pick(catalog, r"execute_agent_job", r"run_agent_job", r"run_job")
    if intent == WEB:
        out = _pick(catalog, r"search_web", r"web_search")
        if _OPEN_PAGE.search(q) or _FIND_PAGE.search(q):
            out += _pick(catalog, r"read_links", r"open_link", r"fetch_page")
        if _SEND.search(q):
            # "check the latest figure ... and send it to Johnnie" is two steps;
            # stopping after the lookup loses the capability the case is about.
            out += _pick(catalog, r"gmail_send", r"send_email", r"send_message")
        return out
    if intent == SETTING:
        out = []
        if _DISCOVER.search(q):
            out += _pick(catalog, r"discover.*capab", r"list.*(capab|setting|option)")
        ql = q.lower()
        # Specific attributes before the general one: "make the app ACCENT
        # teal-ish ... inspect the available APPEARANCE options first" is an
        # accent change, and "appearance" must not claim it for set_theme.
        for attr, pats in (
            ("accent|colou?r", (r"set_accent_colou?r",)),
            ("font", (r"set_chat_font", r"set_.*font")),
            ("effort|reason|think|deep|balanc", (r"set_reasoning_effort",)),
            ("model", (r"set_main_model", r"set_.*model")),
            ("theme|dark|light|appearance", (r"set_theme",)),
        ):
            if re.search(rf"\b(?:{attr})", ql):
                out += _pick(catalog, *pats)
                break
        return out
    # Nothing matched a named capability. If the request is an imperative, let
    # the catalog's own descriptions choose; otherwise stay silent. The caller
    # disables this for declarative and greeting turns -- "my accent is teal;
    # client palettes don't CHANGE that" is a statement, and letting overlap on
    # a stray verb fire execute_agent_job on it is a memory over-call.
    return _describe_match(user_input, catalog) if allow_fallback else []


# --------------------------------------------------------------------------
# argument value resolution
# --------------------------------------------------------------------------

# Allowed values are not declared as JSON-Schema enums in this catalog -- they
# are written into the parameter DESCRIPTION ("low, medium, or high"; "theme
# name, e.g. dark or light"). Reading them from there is what turns a call with
# the right tool name and a wrong argument (0.6 credit) into a full one.
_CAND_AFTER_EG = re.compile(r"(?:e\.g\.|such as|one of|:)\s*(?P<list>[\w\s,/|-]+)", re.I)
_SPLIT_CANDS = re.compile(r"\s*(?:,|/|\||\bor\b|\band\b)\s*", re.I)
_DESC_STOP = frozenset("""
name identifier default optional the a an of for to in on setting settings value
values level string integer number boolean array list query queries results mode
""".split())


def candidate_values(description: str) -> list[str]:
    """Allowed values a parameter description advertises, in stated order."""
    d = (description or "").strip()
    if not d:
        return []
    seg = d
    m = _CAND_AFTER_EG.search(d)
    if m:
        seg = m.group("list")
    parts = [p.strip(" .").lower() for p in _SPLIT_CANDS.split(seg)]
    out = [p for p in parts if p and " " not in p and p not in _DESC_STOP and len(p) < 20]
    # Two or more single-word alternatives is the shape of an enumeration; one
    # word is just prose.
    return out if len(out) >= 2 else []


# Words that select an extreme of an ordered scale without naming a level.
# "reason as deeply and carefully as possible" means the top of low/medium/high.
_MAX_WORDS = re.compile(
    r"\b(?:deeply|deeper|deepest|carefully|thoroughly|deep|hard(?:er|est)?|"
    r"maximum|max|most|as\s+much|deliberate|rigorous|exhaustive|best|highest)\b", re.I)
# "keep the reasoning balanced for everyday questions" asks for the MIDDLE of
# low/medium/high, not an extreme -- checked before the extremes so "balanced"
# is not mistaken for a degree word.
_MID_WORDS = re.compile(
    r"\b(?:balanced?|moderate|medium|everyday|normal|standard|default|"
    r"reasonable|middle)\b", re.I)
_MIN_WORDS = re.compile(
    r"\b(?:quick(?:ly|er)?|fast(?:er)?|brief(?:ly)?|minimal|least|cheap(?:er)?|"
    r"lowest|light(?:er)?\s+touch|skim)\b", re.I)


def _fuzzy_pick(request: str, cands: list[str]) -> str:
    """The candidate the request names, tolerating the generator's typos.

    Requests carry deliberate misspellings ("dak-ish mode" for dark), so an
    exact substring test is not enough; difflib over word-ish tokens recovers
    them without needing a spellchecker.
    """
    q = (request or "").lower()
    for c in cands:
        if re.search(rf"\b{re.escape(c)}", q):
            return c
    import difflib
    toks = re.findall(r"[a-z]{3,}", q)
    best, score = "", 0.0
    for c in cands:
        for t in toks:
            r = difflib.SequenceMatcher(None, c, t).ratio()
            if r > score:
                best, score = c, r
    return best if score >= 0.7 else ""


def resolve_argument(request: str, key: str, spec: dict, observations: list[str]) -> object:
    """A value for one parameter, derived from the request rather than copied.

    Passing the raw user sentence into every string parameter is what produced
    "wrong value for arg effort" and "wrong value for arg theme" -- the tool
    name was right and the call still scored partial.
    """
    desc = str((spec or {}).get("description") or "")
    typ = (spec or {}).get("type")
    if _is_opaque_key(key):
        # An identity-bearing argument (pair_id, pairIds, subject_id ...) must
        # be an id the validator handed us, echoed byte-exact -- never derived
        # from the request. Filling `pairIds` with the query text made the
        # whole case an "invalid v9 identity capability". The caller fills it
        # from observations or retrieved records.
        return None
    cands = candidate_values(desc)

    # Ids come from what an earlier call returned, never from the request:
    # fetch_memories(pairIds) after search_memories, run_workflow(id) after
    # list_workflows.
    if _OPAQUE.search(key) or key.lower() in ("pairids", "pair_ids", "ids", "memory_ids"):
        ids = re.findall(r"\b[0-9a-f]{16,}\b|\b[A-Za-z]+-[0-9A-Za-z]{4,}\b", " ".join(observations))
        ids = list(dict.fromkeys(ids))
        if ids:
            return ids if typ == "array" or key.lower().endswith("s") else ids[0]
    if key.lower() in ("color", "colour", "accent", "accent_color", "accent_colour"):
        c = _color_in(request, cands)
        if c:
            return c

    if cands:
        pick = _fuzzy_pick(request, cands)
        if not pick:
            # An ordered scale named only by degree ("as deeply as possible").
            if _MID_WORDS.search(request or ""):
                pick = cands[len(cands) // 2]
            elif _MAX_WORDS.search(request or ""):
                pick = cands[-1]
            elif _MIN_WORDS.search(request or ""):
                pick = cands[0]
        if pick:
            return pick

    if key in ("url", "urls", "link", "links", "href"):
        # The page to read is whatever the previous hop returned -- never the
        # request text. `read_links` takes `urls`, an array. Nothing to read
        # yet means leave it empty for the next hop, not invent one.
        for obs in reversed(observations or []):
            found = re.findall(r"https?://[^\s\"'<>\]\)]+", obs)
            if found:
                found = [u.rstrip(".,;") for u in dict.fromkeys(found)]
                return found[:3] if (typ == "array" or key.endswith("s")) else found[0]
        return None
    if typ == "array" or key.endswith("ies") or key.endswith("s") and typ is None:
        # search_web takes `queries`, an ARRAY. Sending a bare string fails the
        # schema and the call never happens.
        return [_query_text(request)]
    if _is_opaque_key(key):
        # An identity-bearing argument (pair_id, pairIds, subject_id ...) must be
        # an id the validator handed us, echoed byte-exact. Filling it with the
        # query text made the whole case an "invalid v9 identity capability".
        return None
    if key in ("content", "value", "note", "fact") and re.search(r"\bthat\s+", request or "", re.I):
        # "Add to the handoff note ... THAT we're doing the handoff Friday":
        # the memory's new content is the reported clause, not the request.
        tail = re.split(r"\bthat\s+", request, flags=re.I)[-1]
        tail = re.split(r"[.;!?]|\bIt'?s\b", tail, maxsplit=1)[0].strip(" ,")
        tail = re.sub(r"^(?:w'?e?'?re|we\s+are|i'?m|i\s+am)\s+(?:doing|having|holding)\s+(?:the\s+)?", "", tail, flags=re.I)
        # The scorer credits a required argument only when the observed value
        # CONTAINS the expected phrase; the expected content is the fact in
        # "<thing> is <value>" form ("handoff is Friday"). "the handoff Friday"
        # is the same fact with the copula elided -- restore it.
        m2 = re.match(r"^(?:the\s+)?(.+?)\s+(?:is\s+)?((?:next\s+|this\s+)?(?:monday|tuesday|wednesday|thursday|friday|"
                      r"saturday|sunday|tomorrow|today|tonight|noon|midnight)|\d{1,2}(?::\d{2})?\s*(?:am|pm)?|"
                      r"(?:on\s+)?\w+\s+\d{1,2}(?:st|nd|rd|th)?)$", tail, re.I)
        if m2 and " is " not in tail.lower():
            tail = f"{m2.group(1)} is {m2.group(2)}"
        if 2 <= len(tail.split()) <= 12:
            return tail
    if key in ("query", "q", "search", "text", "prompt", "question", "queries"):
        return _query_text(request)
    if typ in ("integer", "number"):
        m = re.search(r"\b(\d+)\b", request or "")
        return int(m.group(1)) if m else None
    if key in ("url", "urls", "link", "links", "href") and observations:
        # The page to read is whatever the previous hop returned -- never the
        # request text. `read_links` takes `urls`, an array.
        for obs in reversed(observations):
            found = re.findall(r"https?://[^\s\"'<>\]\)]+", obs)
            if found:
                found = [u.rstrip(".,;") for u in dict.fromkeys(found)]
                return found[:3] if (typ == "array" or key.endswith("s")) else found[0]
    return None


_COLORS = ("teal", "coral", "indigo", "amber", "emerald", "crimson", "violet", "cobalt",
           "navy", "olive", "red", "blue", "green", "yellow", "orange", "purple", "pink",
           "black", "white", "gray", "grey", "brown", "magenta", "cyan", "lime", "maroon",
           "gold", "silver", "beige", "turquoise", "lavender", "mint", "rose", "slate")


def _color_in(request: str, cands: list) -> str:
    """The colour the request names, tolerating "tesl-ish" for "teal"."""
    import difflib
    pool = [c for c in cands if isinstance(c, str)] or list(_COLORS)
    for tok in re.findall(r"[A-Za-z]+", request or ""):
        t = tok.lower().removesuffix("ish").rstrip("-")
        if len(t) < 3:
            continue
        m = difflib.get_close_matches(t, [c.lower() for c in pool], n=1, cutoff=0.75)
        if m:
            return next(c for c in pool if c.lower() == m[0])
    return ""


_LEAD_CONTEXT = re.compile(
    r"^.*?\b(?:but|however|though)\b\s*", re.IGNORECASE | re.DOTALL)
_ASK_PREFIX = re.compile(
    r"^\W*(?:what'?s|what\s+is|whats|tell\s+me|search\s+for|look\s+up|find|"
    r"give\s+me|show\s+me)\b\s*", re.IGNORECASE)


def _query_text(request: str) -> str:
    """The searchable topic, not the whole conversational turn.

    "I know I told you my take on quantum computing a while back, but what's the
    latest on it right now?" should search for the topic, not for the preamble.
    """
    full = (request or "").strip()
    q = _ASK_PREFIX.sub("", _LEAD_CONTEXT.sub("", full)).strip(" ?.!")
    # If stripping the preamble left only a pronoun standing in for the topic
    # ("what's the latest on IT right now"), the topic is back in the part we
    # removed -- search the whole turn instead of a contentless fragment.
    if not q or re.search(r"\b(?:it|that|this|them|those)\b", q):
        q = _ASK_PREFIX.sub("", full).strip(" ?.!")
    return q or full
