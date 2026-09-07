"""HTTP surface: GET /health, POST /seed, POST /run.

Binds 0.0.0.0 -- not 127.0.0.1. A harness on the loopback default is
unreachable from outside the container and fails the 10 s health gate outright,
which is a paid screening failure for a one-word cause.

Threaded rather than async on purpose. A scored run executes several cases
concurrently, and the work here is either SQLite (which releases the GIL) or a
network round-trip (which also releases it). What matters far more than the
concurrency model is that no database lock is ever held across a network call;
that discipline lives in `Store` and `Agent`, not here.
"""

from __future__ import annotations

import json
import os
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .agent import Agent
from .llm import EmbedClient, ModelClient
from .protocol import RunRequest, RunResponse, SeedRequest, dumps
from .store import Store

# A full /seed body is large. The reference harness raises its limit to 256 MB;
# every common framework default (Express 100 KB, axum 2 MB) is far below what
# a full-size wave needs, and the failure mode is a 413 that seeds zero pairs
# and quietly forfeits the entire memory half of the composite.
MAX_BODY = 256 * 1024 * 1024

_started = time.time()

# DITTOBENCH_TRACE=<path>: append one JSON line per /seed and /run with the
# exact request the scorer sent, the exact response returned, and wall-clock
# timestamps. The scored report carries per-case scores and notes but never the
# request or the answer text -- so when a case that passes in a sequential
# probe fails under the concurrent scorer, this is the only way to see what
# was actually asked (user_id? tools? order?) and what was actually said.
_TRACE_PATH = os.environ.get("DITTOBENCH_TRACE")
_trace_lock = threading.Lock()


def _trace(kind: str, request: object, response: object, t0: float) -> None:
    if not _TRACE_PATH:
        return
    rec = {"t": time.time(), "kind": kind, "wall_ms": int((time.perf_counter() - t0) * 1000),
           "request": request, "response": response, "thread": threading.get_ident()}
    try:
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with _trace_lock, open(_TRACE_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ditto-p3"

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # Default logging writes a line per request to stderr, which under a
        # 351-case run is pure overhead. Keep it behind a flag.
        if os.environ.get("DITTOBENCH_LOG"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, payload: dict) -> None:
        body = dumps(payload)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self) -> object:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if n <= 0 or n > MAX_BODY:
            return None
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return None

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/health", "/"):
            # The reference harness also advertises capabilities here; harmless
            # to include and it is the documented negotiation signal for a
            # restored per-case relay path.
            self._send(200, {"status": "ok",
                             "capabilities": ["case_scoped_inference_v1"],
                             "uptime_s": round(time.time() - _started, 1)})
            return
        if path == "/stats":
            st: Store = self.server.store              # type: ignore[attr-defined]
            self._send(200, {u: st.counts(u) for u in st.users()})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        body = self._body()
        if body is None:
            self._send(400, {"error": "invalid or oversized JSON body"})
            return
        if path == "/seed":
            self._seed(body)
        elif path == "/run":
            self._run(body)
        else:
            self._send(404, {"error": "not found"})

    def _seed(self, body: object) -> None:
        st: Store = self.server.store                  # type: ignore[attr-defined]
        try:
            counts = st.seed(SeedRequest.parse(body))
            _trace("seed", {k: (v if k != "pairs" else len(v or [])) for k, v in (body or {}).items()}
                   if isinstance(body, dict) else body, counts, time.perf_counter())
        except Exception as e:                          # noqa: BLE001
            # A failed wave must not take the process down; later waves and
            # every /run still need to be served.
            self._send(200, {"pairs": 0, "subjects": 0, "links": 0, "error": str(e)[:200]})
            return
        self._send(200, counts)

    def _run(self, body: object) -> None:
        agent: Agent = self.server.agent                # type: ignore[attr-defined]
        req = RunRequest.parse(body)
        t0 = time.perf_counter()
        try:
            res = agent.run(req)
        except Exception as e:                          # noqa: BLE001
            # Never 500 a case. A malformed response and an exception both
            # score 0, but an exception risks the connection and the run.
            res = RunResponse(
                final_text="",
                abstain=True,
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )
            if os.environ.get("DITTOBENCH_LOG"):
                sys.stderr.write(f"run error {req.case_id}: {e!r}\n")
        _trace("run", body, res.wire(), t0)
        self._send(200, res.wire())


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Bound the pool: unbounded threads on a 2-CPU box turn a burst of
    # concurrent cases into thrash.
    request_queue_size = 128


def build(db_path: str | None = None) -> Server:
    store = Store(db_path)
    agent = Agent(store=store, model=ModelClient(), embed=EmbedClient())
    port = int(os.environ.get("PORT") or os.environ.get("DITTOBENCH_PORT") or 8080)
    srv = Server(("0.0.0.0", port), Handler)
    srv.store = store                                   # type: ignore[attr-defined]
    srv.agent = agent                                   # type: ignore[attr-defined]
    return srv


def main() -> int:
    srv = build()
    host, port = srv.server_address[:2]
    sys.stderr.write(f"ditto-p3 listening on {host}:{port} db={srv.store.path}\n")
    sys.stderr.flush()
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
