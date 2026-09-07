#!/usr/bin/env python3
"""Teach ditto-subnet's local-rehearsal.py to score an ALREADY-RUNNING harness.

`local-rehearsal.py` drives the real DittoBench generator, grader, staged
seeding and validator-visible `tool_endpoint` -- everything needed for a
faithful rehearsal. But it insists on building and launching the *Rust starter
kit* first, so it hard-requires `cargo` and can only ever score that harness.

The scorer itself never touches the harness except over HTTP. So the only thing
standing between it and a harness written in another language is that build
step. This adds `--harness-url`, which skips the build and the launch and
points the scorer at a URL you are already serving.

    python3 scripts/enable_external_harness.py ~/ditto-subnet
    cd ~/ditto-subnet && python3 miners/dittobench-starter-kit/scripts/local-rehearsal.py \
        --harness-url http://127.0.0.1:8080 --run-size small --seed 123 --bench-version 12

Idempotent: running it twice is a no-op. Keeps a .orig backup on first run.

This lives in the project on purpose. It was originally applied in a scratch
directory, which was wiped between sessions and took the whole measurement rig
with it.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

REL = Path("miners/dittobench-starter-kit/scripts/local-rehearsal.py")

# (anchor, replacement, description). Every anchor must appear exactly once.
PATCHES: list[tuple[str, str, str]] = [
    (
        '        help="pin the generated dataset; omit for a fresh random seed",\n    )',
        '        help="pin the generated dataset; omit for a fresh random seed",\n    )\n'
        '    parser.add_argument(\n'
        '        "--harness-url",\n'
        '        default=None,\n'
        '        help=(\n'
        '            "Score an ALREADY-RUNNING harness at this URL instead of building "\n'
        '            "and launching the Rust starter kit. Skips the cargo requirement, "\n'
        '            "which is what lets a harness in any other language be scored by "\n'
        '            "the real local scorer."\n'
        '        ),\n'
        '    )',
        "add --harness-url",
    ),
    (
        '    require_command("cargo")\n    require_command("go")',
        '    external_harness = getattr(args, "harness_url", None)\n'
        '    if not external_harness:\n'
        '        require_command("cargo")\n'
        '    require_command("go")',
        "cargo only when building the Rust harness",
    ),
    (
        '    if not (kit_dir / "Cargo.toml").is_file():',
        '    if not external_harness and not (kit_dir / "Cargo.toml").is_file():',
        "skip the Cargo.toml check for an external harness",
    ),
    (
        "    if process.poll() is not None:",
        "    if process is not None and process.poll() is not None:",
        "health check tolerates a harness we did not spawn",
    ),
]

BUILD_ANCHOR = '        print("building the miner harness and local v9 scorer...", flush=True)'
BUILD_BLOCK_START = "        subprocess.run(\n            [\"cargo\", \"build\""
LAUNCH_ANCHOR = (
    "                harness = subprocess.Popen(\n"
    '                    [str(harness_binary), "serve", "--port", str(harness_port)],'
)


def apply(root: Path) -> int:
    target = root / REL
    if not target.is_file():
        sys.exit(f"not a ditto-subnet checkout (missing {REL}): {root}")
    src = target.read_text()

    # Detect on a marker only THIS patch introduces. Upstream already has an
    # unrelated `--harness-urls` (plural, for the longmem path) and an internal
    # `harness_url` variable, so a substring test on "--harness-url" matches the
    # unpatched file and silently no-ops.
    if "external_harness" in src:
        print(f"already patched: {target}")
        return 0

    backup = target.with_suffix(".py.orig")
    if not backup.exists():
        shutil.copy2(target, backup)

    for anchor, repl, what in PATCHES:
        n = src.count(anchor)
        if n != 1:
            sys.exit(f"anchor for '{what}' matched {n} times; upstream changed shape")
        src = src.replace(anchor, repl)

    # Gate the cargo build block and the harness launch. These are indented
    # blocks rather than single lines, so they are rewritten structurally.
    i = src.index(BUILD_ANCHOR)
    j = src.index("        subprocess.run(\n            [\"go\", \"build\"", i)
    block = src[i:j]
    gated = (
        '        harness_binary = None\n'
        '        if external_harness:\n'
        '            harness_url = external_harness.rstrip("/")\n'
        '            print(f"scoring external harness at {harness_url}", flush=True)\n'
        '            print("building the local v9 scorer...", flush=True)\n'
        '        else:\n'
        + "".join("    " + ln if ln.strip() else ln for ln in block.splitlines(keepends=True))
    )
    src = src[:i] + gated + src[j:]

    k = src.index(LAUNCH_ANCHOR)
    end = src.index("                )\n", k) + len("                )\n")
    launch = src[k:end]
    src = (
        src[:k]
        + "                if not external_harness:\n"
        + "".join("    " + ln if ln.strip() else ln for ln in launch.splitlines(keepends=True))
        + src[end:]
    )

    target.write_text(src)
    print(f"patched {target}\nbackup at {backup}")
    return 0


if __name__ == "__main__":
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").expanduser().resolve()
    raise SystemExit(apply(root))
