#!/usr/bin/env bash
# Score this harness with the REAL DittoBench scorer over several seeds.
#
# Why a batch: single-seed runs of identical code vary by ~0.02 composite, so
# any change smaller than that is unmeasurable from one run. This reports the
# mean and the standard error so a change can be judged against its own noise.
#
# Each seed gets a FRESH database, because a scored run always starts from an
# empty store and seeds sharing one graph is not a faithful rehearsal.
#
#   scripts/bench.sh                    # 8 seeds, run-size small
#   scripts/bench.sh 12 small           # 12 seeds
#   SEEDS="1 2 3" scripts/bench.sh      # explicit seed list
#
# Requires: Go (for the scorer), and DITTO_REPO pointing at a ditto-subnet
# checkout whose local-rehearsal.py accepts --harness-url.
set -uo pipefail

N=${1:-8}
RUN_SIZE=${2:-small}
PORT=${PORT:-8123}
REPO=${DITTO_REPO:?set DITTO_REPO to a ditto-subnet checkout}
HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT=${OUT:-/tmp/bench-$$}
mkdir -p "$OUT"

if [ -n "${SEEDS:-}" ]; then
  read -ra seeds <<< "$SEEDS"
else
  seeds=(); for i in $(seq 0 $((N - 1))); do seeds+=($((100000 + i * 7919))); done
fi

kill_port() {
  local pid
  pid=$(ss -lptn "sport = :$PORT" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1)
  [ -n "$pid" ] && kill "$pid" 2>/dev/null
  sleep 0.3
}

for s in "${seeds[@]}"; do
  kill_port
  rm -f "$OUT/db-$s.db"*
  ( cd "$HARNESS_DIR" && nohup env DITTOBENCH_DB="$OUT/db-$s.db" \
      DITTOBENCH_PROVIDER="${DITTOBENCH_PROVIDER:-none}" \
      OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-}" PORT="$PORT" \
      python3 -m app.server >/dev/null 2>&1 & )
  for _ in $(seq 1 60); do
    curl -s -m 1 "localhost:$PORT/health" >/dev/null 2>&1 && break; sleep 0.1
  done
  ( cd "$REPO" && timeout 900 python3 \
      miners/dittobench-starter-kit/scripts/local-rehearsal.py \
      --harness-url "http://127.0.0.1:$PORT" --run-size "$RUN_SIZE" \
      --seed "$s" --bench-version 12 --report "$OUT/r-$s.json" >/dev/null 2>&1 )
  printf '.' >&2
done
kill_port
echo >&2

python3 - "$OUT" <<'PY'
import glob, json, statistics, sys
rows = []
for f in sorted(glob.glob(sys.argv[1] + "/r-*.json")):
    try:
        r = json.load(open(f))["report"]
    except Exception:
        continue
    rows.append((r["seed"], r["composite"], r["tool_mean"], r["memory_mean"]))
if not rows:
    sys.exit("no reports produced")
print(f"{'seed':>9}{'composite':>11}{'tool':>8}{'memory':>8}")
for s, c, t, m in rows:
    print(f"{s:>9}{c:>11.3f}{t:>8.3f}{m:>8.3f}")


def stat(v):
    mu = statistics.mean(v)
    se = statistics.stdev(v) / len(v) ** 0.5 if len(v) > 1 else 0.0
    return mu, se


print("-" * 36)
for name, i in (("composite", 1), ("tool", 2), ("memory", 3)):
    mu, se = stat([r[i] for r in rows])
    print(f"{name:>9}  {mu:.3f} +/- {se:.3f}   (min {min(r[i] for r in rows):.3f})")
print(f"\n{len(rows)} seeds. A change is only real if it exceeds ~2x the stderr above.")
PY
