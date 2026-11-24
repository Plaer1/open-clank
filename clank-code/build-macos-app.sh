#!/bin/bash
# Build only on native Apple Silicon macOS; does not modify an installed app.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"
exec "${OPENCLANK_BUILD_PYTHON:-python3}" -B scripts/macos_package.py build "$@"
