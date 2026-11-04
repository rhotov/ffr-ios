#!/usr/bin/env bash
set -euo pipefail
FFR_PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$FFR_PROJECT"
export CARGO_TARGET_DIR="$FFR_PROJECT/target"
export TMPDIR="$FFR_PROJECT/data/tools/tmp"
mkdir -p "$TMPDIR"
FFR_PYTHON="${FFR_PYTHON:-$FFR_PROJECT/.venv/bin/python3}"
[[ -x "$FFR_PYTHON" ]] || FFR_PYTHON=python3
cargo build --workspace --release --offline
"$FFR_PYTHON" scripts/prepare_macos.py "$@"
