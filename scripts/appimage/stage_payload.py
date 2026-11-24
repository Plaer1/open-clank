"""Stage current Beta 3 sources, a private CPython and the activated Linux helpers."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from scripts.windows_portable_build import stage_data
from scripts.linux_package import HELPERS, child, digest, verify_elf


def stage_vendor(out, vendor):
    from src.openclank.engine_build import _source_directory_excluded, _source_file_excluded, load_vendor_manifest
    def excluded(directory, entries):
        parent = Path(directory)
        skip = []
        for name in entries:
            path = parent / name
            relative = path.relative_to(vendor)
            if path.is_dir() and _source_directory_excluded(relative):
                skip.append(name)
            elif path.is_file() and _source_file_excluded(relative) and name != "openclank-vendor.json":
                skip.append(name)
        return skip
    shutil.copytree(vendor, out / "packages/mimo-code", symlinks=True, ignore=excluded)
    load_vendor_manifest(out)


def stage(out, python, engine, native, vendor):
    stage_data(out)
    stage_vendor(out, vendor)
    # The worker's host-app shim uses this package's interpreter path.
    binaries = out / "bin"
    binaries.mkdir()
    for name in HELPERS[:-1]:
        source = native / name
        verify_elf(source)
        shutil.copy2(source, binaries / name)
    pointer = json.loads((engine / "current.json").read_text())
    artifact = child(engine, pointer["artifact"])
    provenance = artifact / "provenance.json"
    record = json.loads(provenance.read_text())
    manifest = REPO / "packages/mimo-code/openclank-vendor.json"
    if (record["target"] != "linux-x64" or digest(provenance) != pointer["provenance_sha256"]
        or record["inputs"]["source_sha256"] != json.loads(manifest.read_text())["build"]["source_sha256"]
        or record["inputs"]["vendor_manifest_sha256"] != digest(manifest)
        or digest(artifact / "openclank-engine") != record["binary"]["sha256"]):
        raise ValueError("The active engine does not match the current checkout's pinned build.")
    engine_dest = out / "libexec/openclank/engine"
    shutil.copytree(artifact, engine_dest / pointer["artifact"])
    shutil.copy2(engine / "current.json", engine_dest)
    shutil.copytree(python, out / "python", symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # Console-script shebangs from pip refer to the build host. Use python -m.
    for tool in (out / "python/bin").iterdir():
        if tool.name not in {"python", "python3", "python3.13"}:
            tool.unlink()
    for node in (out / "python").rglob("*"):
        if node.is_symlink() and not node.resolve().is_relative_to((out / "python").resolve()):
            raise ValueError(f"External runtime link: {node.name}")
    launcher = out / "openclank"
    launcher.write_text('''#!/bin/sh
set -eu
here=$(dirname "$(readlink -f "$0")")
export OPEN_CLANK_FILES_SERVICE_BIN="$here/bin/odysseus-files-service"
export OPENCLANK_HISTORY_SERVICE_BIN="$here/bin/openclank-history-service"
export OPEN_CLANK_MEMERY_WORKER="$here/bin/memery-worker"
exec "$here/python/bin/python3" -B "$here/scripts/openclank" "$@"
''')
    launcher.chmod(0o755)
    host = binaries / HELPERS[-1]
    host.write_text('''#!/bin/sh
here=$(dirname "$(dirname "$(readlink -f "$0")")")
cd "$here" || exit 1
exec "$here/python/bin/python3" -B -m src.openclank.linux_host_apps --worker "$@"
''')
    host.chmod(0o755)
    identity = {"channel":"Beta 3", "source_commit":subprocess.check_output(
        ["git","rev-parse","HEAD"], cwd=REPO, text=True).strip(),
        "app_sha256":digest(out/"app.py"), "engine_sha256":record["binary"]["sha256"],
        "stylesheet_sha256":digest(out/"static/style.css")}
    (out / "release.json").write_text(json.dumps(identity, indent=2) + "\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("out", "python", "engine", "native", "vendor"):
        p.add_argument(key, type=Path)
    stage(**vars(p.parse_args()))
