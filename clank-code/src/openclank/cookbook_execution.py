"""Shared saved-host environment construction for Cookbook launch callers."""

from __future__ import annotations

import shlex
import sys
from typing import Any, Mapping


def cookbook_launch_environment(env_root: Mapping[str, Any], host: str = "") -> dict[str, Any]:
    """Resolve the target profile without confusing a remote OS with this OS."""
    servers = env_root.get("servers") or []
    selected = next(
        (item for item in servers if isinstance(item, dict)
         and (item.get("host") or "") == (host or "")),
        None,
    )
    if selected is None and host:
        selected = next(
            (item for item in servers if isinstance(item, dict) and item.get("name") == host),
            None,
        )
    selected = selected or {}
    target = str(selected.get("host") or host or "")
    kind = selected.get("env") or env_root.get("env") or "none"
    path = str(selected.get("envPath") or env_root.get("envPath") or "")
    platform = selected.get("platform") or env_root.get("platform")
    if not platform:
        platform = "linux" if target else ("windows" if sys.platform == "win32" else sys.platform)
    platform = str(platform).lower()
    prefix = ""
    if kind == "venv" and path:
        if platform == "windows":
            activation = path if path.replace("/", "\\").lower().endswith("\\scripts\\activate.ps1") else path.rstrip("/\\") + "\\Scripts\\Activate.ps1"
            prefix = "& '" + activation.replace("'", "''") + "'"
        else:
            activation = path if path.endswith("/bin/activate") else path.rstrip("/") + "/bin/activate"
            prefix = "source " + shlex.quote(activation)
    elif kind == "conda" and path:
        if platform == "windows":
            prefix = "conda activate '" + path.replace("'", "''") + "'"
        else:
            prefix = 'eval "$(conda shell.bash hook)" && conda activate ' + shlex.quote(path)
    return {
        "env_prefix": prefix,
        "env_type": kind,
        "env_path": path,
        "gpus": env_root.get("gpus") or "",
        "platform": platform,
        "ssh_port": selected.get("sshPort") or selected.get("port") or env_root.get("sshPort") or "",
        "remote_host": target,
    }
