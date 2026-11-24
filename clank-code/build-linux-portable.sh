#!/bin/sh
set -eu
# Supply a task-owned standalone Python 3.13.17 with requirements and
# PyInstaller 6.16.0 installed. This script performs no global installation.
exec "${OPENCLANK_BUILD_PYTHON:-python3}" -B "$(dirname "$0")/scripts/linux_package.py" build "$@"
