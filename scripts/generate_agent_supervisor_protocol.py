#!/usr/bin/env python3
"""Generate/check the checked-in Python supervisor protocol stubs."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROTO_DIR = ROOT / "packages/openclank-agent-supervisor/proto"
PROTO = PROTO_DIR / "openclank_agent_supervisor_v1.proto"
OUTPUT_DIR = ROOT / "src/openclank/generated"
FILES = (
    "openclank_agent_supervisor_v1_pb2.py",
    "openclank_agent_supervisor_v1_pb2_grpc.py",
)


def _generate(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{PROTO_DIR}",
        f"--python_out={destination}",
        f"--grpc_python_out={destination}",
        str(PROTO),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    for name in FILES:
        path = destination / name
        text = path.read_text()
        text = text.replace(
            "import openclank_agent_supervisor_v1_pb2 as openclank__agent__supervisor__v1__pb2",
            "from . import openclank_agent_supervisor_v1_pb2 as openclank__agent__supervisor__v1__pb2",
        )
        path.write_text(text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.write == args.check:
        parser.error("choose exactly one of --write or --check")
    if args.write:
        _generate(OUTPUT_DIR)
        return 0
    with tempfile.TemporaryDirectory(prefix="openclank-agent-supervisor-proto-") as temp:
        destination = Path(temp)
        _generate(destination)
        for name in FILES:
            expected = destination / name
            actual = OUTPUT_DIR / name
            if not actual.exists() or actual.read_bytes() != expected.read_bytes():
                print(f"generated protocol drift: {actual}", file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
