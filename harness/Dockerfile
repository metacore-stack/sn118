# ditto-p3 — SN118 / DittoBench agent-memory harness.
#
# Python 3.12 standard library only. There is no pip install, no wheel, no
# model download and no git dependency, so `docker build` cannot fail on a
# credential-less network fetch — the failure mode that eats an evaluation fee
# after payment.
#
# Pinned by digest: an unpinned tag can change the glibc under you between the
# build you tested and the build a validator runs.
FROM python:3.12-slim-bookworm

# The scored container runs with a READ-ONLY root filesystem and only a /tmp
# tmpfs writable, as a non-root user. Everything the harness writes at runtime
# — the SQLite database, its WAL and shm sidecars — must live under /tmp.
ENV DITTOBENCH_DB=/tmp/dittobench.db \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY app/ /app/app/

# Precompile to .pyc at build time. With PYTHONDONTWRITEBYTECODE set and a
# read-only root, the interpreter cannot write them at import time, so doing it
# here is what keeps cold start inside the 10 s /health ceiling.
RUN python3 -m compileall -q /app/app && \
    adduser --system --uid 65532 --no-create-home --group nonroot

USER 65532:65532
EXPOSE 8080

# Bind 0.0.0.0, never 127.0.0.1: a loopback-bound harness is unreachable from
# outside the container and fails health screening outright.
CMD ["python3", "-m", "app.server"]
