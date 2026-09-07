"""DittoBench wire protocol.

Shapes match the Go validator's contract in
`research/dittobench-datagen/protocol/protocol.go`, verified 2026-09-02.

Two rules drive every decision in this module:

1. **Opaque identifiers are capabilities.** `case_id`, `user_id`, `pair_id`,
   `session_id` and `subject_id` are persisted byte-exact and compared only for
   equality. Nothing here derives family, order or grading behaviour from their
   spelling -- v12 replaced readable session ids with opaque hashes precisely to
   punish harnesses that parsed them.

2. **Be liberal in what you accept.** `SeedRequest` serialises absent arrays as
   a missing key rather than `null`, and a strict decoder that rejects either one
   fails a whole wave. Every optional field here tolerates absent *and* null.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass, field
from typing import Any


# --------------------------------------------------------------------------
# defensive coercion
# --------------------------------------------------------------------------

def _str(v: Any, default: str = "") -> str:
    """Coerce to str without inventing content. None/absent -> default."""
    if v is None:
        return default
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    return default


def _int(v: Any, default: int = 0) -> int:
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            return default
    return default


def _list(v: Any) -> list:
    """Absent, null and non-list all mean 'empty' -- never raise on a wave."""
    return v if isinstance(v, list) else []


def _obj(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


# --------------------------------------------------------------------------
# timestamps
# --------------------------------------------------------------------------

_TS_TRAILING_Z = re.compile(r"[Zz]$")


def parse_timestamp(raw: str) -> _dt.datetime | None:
    """Parse an RFC3339 timestamp to an aware UTC datetime.

    Returns None rather than raising: a malformed timestamp on one pair must
    not cost the whole wave. Callers fall back to seeding order, which is a
    weaker but still usable recency signal.
    """
    s = _str(raw).strip()
    if not s:
        return None
    s = _TS_TRAILING_Z.sub("+00:00", s)
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        # Tolerate a space separator and fractional seconds beyond 6 digits.
        s2 = s.replace(" ", "T")
        s2 = re.sub(r"(\.\d{6})\d+", r"\1", s2)
        try:
            dt = _dt.datetime.fromisoformat(s2)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


def epoch_seconds(raw: str) -> float | None:
    dt = parse_timestamp(raw)
    return dt.timestamp() if dt else None


# --------------------------------------------------------------------------
# /seed
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class MemoryPair:
    pair_id: str
    session_id: str
    timestamp: str          # kept as the raw wire string, byte-exact
    prompt: str
    response: str

    @staticmethod
    def parse(d: Any) -> "MemoryPair | None":
        d = _obj(d)
        pair_id = _str(d.get("pair_id"))
        if not pair_id:
            return None     # unaddressable; cannot be upserted idempotently
        return MemoryPair(
            pair_id=pair_id,
            session_id=_str(d.get("session_id")),
            timestamp=_str(d.get("timestamp")),
            prompt=_str(d.get("prompt")),
            response=_str(d.get("response")),
        )


@dataclass(frozen=True, slots=True)
class Subject:
    id: str
    subject_text: str
    description_text: str

    @staticmethod
    def parse(d: Any) -> "Subject | None":
        d = _obj(d)
        sid = _str(d.get("id"))
        if not sid:
            return None
        return Subject(
            id=sid,
            subject_text=_str(d.get("subject_text")),
            description_text=_str(d.get("description_text")),
        )


@dataclass(frozen=True, slots=True)
class SubjectLink:
    subject_id: str
    pair_id: str

    @staticmethod
    def parse(d: Any) -> "SubjectLink | None":
        d = _obj(d)
        sid, pid = _str(d.get("subject_id")), _str(d.get("pair_id"))
        return SubjectLink(sid, pid) if sid and pid else None


@dataclass(frozen=True, slots=True)
class SeedRequest:
    user_id: str
    wave: int
    pairs: tuple[MemoryPair, ...]
    subjects: tuple[Subject, ...]
    links: tuple[SubjectLink, ...]

    @staticmethod
    def parse(body: Any) -> "SeedRequest":
        d = _obj(body)
        return SeedRequest(
            user_id=_str(d.get("user_id")),
            wave=_int(d.get("wave"), 0),
            pairs=tuple(p for p in (MemoryPair.parse(x) for x in _list(d.get("pairs"))) if p),
            subjects=tuple(s for s in (Subject.parse(x) for x in _list(d.get("subjects"))) if s),
            links=tuple(l for l in (SubjectLink.parse(x) for x in _list(d.get("links"))) if l),
        )

    @property
    def is_raw_pairs(self) -> bool:
        """Tier B: pairs arrive with no prepared subject index.

        The starter docs call building subjects here 'the highest-value change
        you can make'.
        """
        return bool(self.pairs) and not self.subjects


# --------------------------------------------------------------------------
# /run
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict        # JSON Schema

    @staticmethod
    def parse(d: Any) -> "ToolDefinition | None":
        d = _obj(d)
        name = _str(d.get("name"))
        if not name:
            return None
        return ToolDefinition(name, _str(d.get("description")), _obj(d.get("parameters")))


@dataclass(frozen=True, slots=True)
class RunRequest:
    case_id: str
    system_prompt: str
    user_input: str
    tools: tuple[ToolDefinition, ...]
    bench_version: int
    tool_endpoint: str
    user_id: str
    inference_base_url: str

    @staticmethod
    def parse(body: Any) -> "RunRequest":
        d = _obj(body)
        return RunRequest(
            case_id=_str(d.get("case_id")),
            system_prompt=_str(d.get("system_prompt")),
            user_input=_str(d.get("user_input")),
            tools=tuple(t for t in (ToolDefinition.parse(x) for x in _list(d.get("tools"))) if t),
            bench_version=_int(d.get("bench_version"), 0),
            tool_endpoint=_str(d.get("tool_endpoint")),
            user_id=_str(d.get("user_id")),
            inference_base_url=_str(d.get("inference_base_url")),
        )

    def tool(self, name: str) -> ToolDefinition | None:
        for t in self.tools:
            if t.name == name:
                return t
        return None


@dataclass(slots=True)
class ToolCall:
    """An *observed* tool call: one that was actually executed.

    `hop` is the 0-based order of the call within the case. Never synthesise
    these -- the validator grades the trajectory it observed at `tool_endpoint`,
    and a self-reported call it did not serve is not evidence.
    """
    name: str
    args: dict
    hop: int

    def wire(self) -> dict:
        return {"name": self.name, "args": self.args, "hop": self.hop}


@dataclass(slots=True)
class RunResponse:
    final_text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    answer: str = ""
    abstain: bool = False
    confidence: float | None = None

    def wire(self) -> dict:
        d: dict[str, Any] = {
            "final_text": self.final_text,
            "tool_calls": [c.wire() for c in self.tool_calls],
            "prompt_tokens": int(self.prompt_tokens),
            "output_tokens": int(self.output_tokens),
            "latency_ms": int(self.latency_ms),
        }
        # `answer` is matched by the grader before prose containment, so always
        # emit it when we have one. Empty string is omitted (omitempty on the
        # Go side) rather than sent as "".
        if self.answer:
            d["answer"] = self.answer
        if self.abstain:
            d["abstain"] = True
        if self.confidence is not None:
            d["confidence"] = round(float(self.confidence), 4)
        return d


# --------------------------------------------------------------------------
# tool_endpoint round-trip
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ToolExecRequest:
    case_id: str
    user_id: str
    name: str
    args: dict
    hop: int

    def wire(self) -> dict:
        d: dict[str, Any] = {"case_id": self.case_id, "name": self.name}
        if self.user_id:
            d["user_id"] = self.user_id
        if self.args:
            d["args"] = self.args
        if self.hop:
            d["hop"] = self.hop
        return d


@dataclass(frozen=True, slots=True)
class ToolExecResponse:
    result: str
    error: str

    @staticmethod
    def parse(body: Any) -> "ToolExecResponse":
        d = _obj(body)
        return ToolExecResponse(result=_str(d.get("result")), error=_str(d.get("error")))

    @property
    def is_error(self) -> bool:
        return bool(self.error)

    @property
    def is_unavailable_memory_tool(self) -> bool:
        """The endpoint declining a memory tool.

        Expected behaviour, not a bug: memory tools are ours to serve. The
        documented shape is an empty result plus
        `{"error": "tool not available via this endpoint: search_memories"}`.
        """
        return "not available via this endpoint" in self.error.lower()


def dumps(obj: Any) -> bytes:
    """Compact, deterministic JSON. separators avoid gratuitous whitespace."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
