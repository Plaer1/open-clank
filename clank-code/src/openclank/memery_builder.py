"""Resolve the packaged worker or build a missing source-install executable."""
import os
from pathlib import Path
import subprocess
import sys

from src.runtime_paths import RuntimeResolutionError, resolve_memery_worker


def memery_worker_path() -> str:
    try:
        return resolve_memery_worker().path
    except RuntimeResolutionError:
        auto_build = os.environ.get("OPEN_CLANK_MEMERY_AUTO_BUILD", os.environ.get("FM_AUTO_BUILD", "0"))
        if getattr(sys, "frozen", False) or auto_build.strip().lower() not in ("1", "true", "yes"):
            raise

    crate = Path(__file__).resolve().parents[2] / "native" / "memery"
    subprocess.run(
        ["cargo", "build", "--locked", "--release", "--bin", "memery-worker", "--bin", "memery-migrate"],
        cwd=crate, check=True,
    )
    return resolve_memery_worker().path
