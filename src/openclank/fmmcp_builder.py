"""Auto-build helper for fm-mcp binary.

At startup, checks if the FM_MCP_COMMAND binary exists on disk.
If missing and FM_AUTO_BUILD=1, runs cargo build --release from
mcp_servers/frankenmemory/ so fm-mcp is always available.
"""

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FM_CRATE_DIR = _REPO_ROOT / "mcp_servers" / "frankenmemory"


async def ensure_fmmcp_built() -> bool:
    """Check if fm-mcp binary exists; auto-build if missing and allowed.

    Returns True if the binary exists (after build or already present),
    False if it's still missing.
    """
    fm_command = os.environ.get("FM_MCP_COMMAND", "fm-mcp")
    binary = Path(fm_command)

    if binary.is_file():
        logger.info("fm-mcp binary found at %s (skip build)", binary)
        return True

    auto_build = os.environ.get("FM_AUTO_BUILD", "0").strip().lower()
    if auto_build not in ("1", "true", "yes"):
        logger.warning(
            "fm-mcp binary not found at %s — set FM_AUTO_BUILD=1 to auto-build",
            binary,
        )
        return False

    if not _FM_CRATE_DIR.is_dir():
        logger.error(
            "fm-mcp crate directory not found: %s — cannot auto-build",
            _FM_CRATE_DIR,
        )
        return False

    logger.info(
        "fm-mcp binary missing at %s — building from %s",
        binary,
        _FM_CRATE_DIR,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "cargo",
            "build",
            "--release",
            cwd=str(_FM_CRATE_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            logger.info("fm-mcp build succeeded")
            return binary.is_file()
        logger.error(
            "fm-mcp build failed (exit code %d). stderr: %s",
            proc.returncode,
            stderr.decode(errors="replace")[-500:],
        )
        return False
    except Exception as e:
        logger.error("fm-mcp build error: %s", e)
        return False
