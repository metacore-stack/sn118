"""Clients for the two relays the sandbox actually exposes.

Sandbox egress is deny-all. Exactly three holes exist, and two of them are
here:

  chat        OpenAI-compatible chat completions. The validator sets
              DITTOBENCH_PROVIDER=platform (legacy alias `chutes`) and injects
              DITTOBENCH_INFERENCE_BASE_URL. Auth is the literal, non-secret
              header `Authorization: Bearer ticket` -- the real upstream key is
              held outside the sandbox by the proxy, which also forces the model
              and medium reasoning effort.

  embeddings  Ollama-compatible. The validator *replaces* OLLAMA_BASE_URL with
              its own ticket-bound gateway, which locks the profile
              `dittobench-v7-openrouter-pplx-embed-v1-0.6b-768-v1` backed by
              `perplexity/pplx-embed-v1-0.6b` at 768 dims. Note the wire format
              is Ollama's `/api/embed`, NOT OpenAI's `/v1/embeddings`: a harness
              that guesses the OpenAI shape here gets nothing.

Both clients are built so that an unreachable relay degrades rather than
crashes. No model is a survivable condition -- deterministic retrieval still
answers a large share of memory cases. A raised exception in the middle of a
`/run` is not survivable, because it scores 0 for that case.

Nothing in this module may be called while a database transaction is open.
Holding a lock across a network round-trip is the single most expensive mistake
on this benchmark: it serialises concurrent cases into one queue and fails the
whole run on a deadline, with no per-case error to point at.
"""

from __future__ import annotations

import json
import math
import os
import struct
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

USER_AGENT = "ditto-p3/1.0"


def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v.strip()
    return default


def _post_json(url: str, payload: dict, headers: dict[str, str],
               timeout: float) -> tuple[int, dict | None, str]:
    """POST JSON, return (status, parsed, error). Never raises."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    for k, v in headers.items():
        if v:
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw.decode("utf-8", "replace")), ""
            except json.JSONDecodeError:
                return r.status, None, f"non-JSON response ({len(raw)}B)"
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read()[:400].decode("utf-8", "replace")
        except Exception:
            pass
        return e.code, None, f"HTTP {e.code}: {detail}"
    except urllib.error.URLError as e:
        return 0, None, f"unreachable: {e.reason}"
    except (TimeoutError, OSError) as e:
        return 0, None, f"io: {e}"


# --------------------------------------------------------------------------
# chat
# --------------------------------------------------------------------------

@dataclass(slots=True)
class ChatResult:
    text: str = ""
    prompt_tokens: int = 0
    output_tokens: int = 0
    error: str = ""
    latency_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.error


class ModelClient:
    """OpenAI-compatible chat completions against whichever relay is configured.

    Determinism matters more than variety here: the metamorphic-consistency
    factor punishes a harness that answers a question one way and its paraphrase
    another, so temperature is pinned to 0 and a fixed seed is sent (the
    reference harness does the same).
    """

    def __init__(self) -> None:
        self.provider = _env("DITTOBENCH_PROVIDER", default="platform").lower()
        self.model = _env("DITTOBENCH_MODEL", default="openai/gpt-oss-20b")
        self.base = self._base_url()
        self.timeout = float(_env("DITTOBENCH_MODEL_TIMEOUT", default="45"))
        self._lock = threading.Lock()
        self._budget_exhausted = False

    def _base_url(self) -> str:
        # The validator's injected URL always wins when present.
        injected = _env("DITTOBENCH_INFERENCE_BASE_URL")
        if injected:
            return injected.rstrip("/")
        if self.provider in ("platform", "chutes"):
            # Nothing injected and we were told we are on the platform: there is
            # no sane default, so leave it empty and degrade loudly.
            return ""
        if self.provider == "openrouter":
            return "https://openrouter.ai/api/v1"
        if self.provider == "ollama":
            return _env("OLLAMA_BASE_URL", default="http://localhost:11434").rstrip("/") + "/v1"
        return ""

    def _headers(self) -> dict[str, str]:
        if self.provider in ("platform", "chutes"):
            # Literal string, not a secret. The proxy holds the real key.
            return {"Authorization": "Bearer ticket"}
        if self.provider == "openrouter":
            k = _env("OPENROUTER_API_KEY")
            return {"Authorization": f"Bearer {k}"} if k else {}
        return {}

    @property
    def available(self) -> bool:
        return bool(self.base)

    def chat(self, messages: list[dict], *, max_tokens: int = 700,
             timeout: float | None = None, per_case_base: str = "") -> ChatResult:
        """One chat round-trip. Returns a ChatResult; never raises."""
        base = (per_case_base or self.base).rstrip("/")
        if not base:
            return ChatResult(error="no inference base url configured")
        url = f"{base}/chat/completions" if not base.endswith("/chat/completions") else base
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.0,
            "seed": 42,
            "max_tokens": max_tokens,
            "stream": False,
        }
        t0 = time.perf_counter()
        status, data, err = _post_json(url, payload, self._headers(),
                                       timeout or self.timeout)
        dt = int((time.perf_counter() - t0) * 1000)
        if err or not data:
            return ChatResult(error=err or f"status {status}", latency_ms=dt)
        try:
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            text = msg.get("content") or ""
            # Some relays put reasoning in a sibling field; the visible answer
            # is still `content`, so we deliberately ignore the rest.
            usage = data.get("usage") or {}
            return ChatResult(
                text=text if isinstance(text, str) else "",
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
                latency_ms=dt,
            )
        except (KeyError, IndexError, TypeError, ValueError) as e:
            return ChatResult(error=f"malformed completion: {e}", latency_ms=dt)

    def chat_json(self, messages: list[dict], *, max_tokens: int = 700,
                  repair: bool = True) -> tuple[dict | None, ChatResult]:
        """Chat, expecting a JSON object back.

        Allows exactly one bounded repair attempt for malformed structured
        output. Note what this does and does not buy: it fixes schema-*invalid*
        output, which is the easy failure. It cannot detect output that is
        schema-valid and semantically wrong -- that is handled upstream by
        caching compiled programs on a semantic key, so paraphrases of one
        question cannot compile to two different programs.
        """
        res = self.chat(messages, max_tokens=max_tokens)
        if not res.ok:
            return None, res
        obj = extract_json(res.text)
        if obj is not None or not repair:
            return obj, res
        fixed = self.chat(
            messages + [
                {"role": "assistant", "content": res.text[:2000]},
                {"role": "user", "content":
                    "That was not valid JSON. Reply with the JSON object only -- "
                    "no prose, no code fence, no explanation."},
            ],
            max_tokens=max_tokens,
        )
        if not fixed.ok:
            return None, res
        merged = ChatResult(
            text=fixed.text,
            prompt_tokens=res.prompt_tokens + fixed.prompt_tokens,
            output_tokens=res.output_tokens + fixed.output_tokens,
            latency_ms=res.latency_ms + fixed.latency_ms,
        )
        return extract_json(fixed.text), merged


def extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a model reply.

    Models fence, prefix and apologise. Try the whole string, then a fenced
    block, then a brace-balanced scan -- respecting string literals so a `}`
    inside a value does not end the object early.
    """
    if not text:
        return None
    s = text.strip()
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    if "```" in s:
        for block in s.split("```")[1::2]:
            b = block.strip()
            if b.startswith("json"):
                b = b[4:].strip()
            try:
                v = json.loads(b)
                if isinstance(v, dict):
                    return v
            except json.JSONDecodeError:
                continue
    start = s.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(s[start:i + 1])
                        if isinstance(v, dict):
                            return v
                    except json.JSONDecodeError:
                        break
        start = s.find("{", start + 1)
    return None


# --------------------------------------------------------------------------
# embeddings
# --------------------------------------------------------------------------

class EmbedClient:
    """Ollama-compatible embeddings.

    The validator swaps OLLAMA_BASE_URL for its own gateway, so the *only*
    correct thing to do is read that variable and speak Ollama's wire format.
    `/api/embed` takes `input` (string or list) and returns `embeddings`; the
    older `/api/embeddings` takes `prompt` and returns `embedding`. Try the
    modern one, fall back once.
    """

    def __init__(self) -> None:
        self.base = _env("OLLAMA_BASE_URL", default="http://localhost:11434").rstrip("/")
        self.model = _env("DITTOBENCH_EMBED_MODEL", default="embeddinggemma")
        self.timeout = float(_env("DITTOBENCH_EMBED_TIMEOUT", default="30"))
        self._dead_until = 0.0
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.base) and time.time() >= self._dead_until

    def _mark_dead(self, seconds: float = 60.0) -> None:
        """Circuit-breaker. If the gateway is down, stop paying its timeout on
        every single case -- that alone can blow the whole-run deadline."""
        with self._lock:
            self._dead_until = time.time() + seconds

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch. Returns [] on any failure -- never raises.

        An empty return is a supported state: the dense projection simply does
        not contribute to fusion, and the lexical, trigram and graph projections
        carry the case.
        """
        if not texts or not self.available:
            return []
        status, data, err = _post_json(
            f"{self.base}/api/embed",
            {"model": self.model, "input": texts},
            {}, self.timeout,
        )
        if data and isinstance(data.get("embeddings"), list):
            out = [[float(x) for x in v] for v in data["embeddings"]
                   if isinstance(v, list)]
            if len(out) == len(texts):
                return out
        # Legacy single-prompt endpoint.
        out2: list[list[float]] = []
        for t in texts:
            status, data, err = _post_json(
                f"{self.base}/api/embeddings",
                {"model": self.model, "prompt": t}, {}, self.timeout)
            v = (data or {}).get("embedding")
            if not isinstance(v, list):
                self._mark_dead()
                return []
            out2.append([float(x) for x in v])
        return out2

    def embed_one(self, text: str) -> list[float]:
        got = self.embed([text])
        return got[0] if got else []


def pack_vector(vec: list[float]) -> tuple[bytes, int, float]:
    """float32 blob + dim + L2 norm, so cosine is a dot product at query time."""
    dim = len(vec)
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return struct.pack(f"<{dim}f", *vec), dim, norm
