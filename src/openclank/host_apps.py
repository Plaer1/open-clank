"""Platform selection for authorized host-file application dispatch."""

from __future__ import annotations

import sys
from typing import Any, Protocol

from src.openclank.macos_host_apps import MacOSHostApps, MacOSHostAppsError

# Preserve the existing error contract and macOS implementation verbatim.
HostAppsError = MacOSHostAppsError


class HostApps(Protocol):
    async def discover_async(self, path: str) -> list[dict[str, Any]]: ...
    async def launch_async(self, path: str, app_id: str) -> dict[str, str]: ...


def host_apps() -> HostApps:
    if sys.platform == "win32":
        from src.openclank.windows_host_apps import WindowsHostApps
        return WindowsHostApps()
    return MacOSHostApps()
