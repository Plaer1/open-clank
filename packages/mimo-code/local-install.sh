#!/usr/bin/env bash

set -euo pipefail

vendor_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$vendor_dir/../.." && pwd)"
python_bin="${PYTHON:-python3}"

exec "$python_bin" "$repo_dir/scripts/openclank_engine.py" --repo-root "$repo_dir" build "$@"
