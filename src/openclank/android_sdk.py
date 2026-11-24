"""Read-only metadata for a standard Android SDK on the app host."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys


def _sdk_candidates() -> tuple[list[Path], str]:
    configured = [os.environ.get(key, '').strip() for key in ('ANDROID_HOME', 'ANDROID_SDK_ROOT')]
    configured = [value for value in configured if value]
    if configured:
        roots = [Path(value).expanduser() for value in configured]
        if any(not root.is_absolute() or str(root).startswith(('//', '\\\\')) for root in roots):
            raise ValueError('configured_root_invalid')
        return roots, 'environment'
    if sys.platform == 'darwin':
        return [Path.home() / 'Library' / 'Android' / 'sdk'], 'standard_location'
    if sys.platform == 'win32':
        data = os.environ.get('LOCALAPPDATA')
        base = Path(data) if data else Path.home() / 'AppData' / 'Local'
        if not base.is_absolute() or str(base).startswith(('//', '\\\\')):
            raise ValueError('standard_location_unavailable')
        return [base / 'Android' / 'Sdk'], 'standard_location'
    if sys.platform.startswith('linux'):
        return [Path.home() / 'Android' / 'Sdk'], 'standard_location'
    raise ValueError('host_platform_unsupported')


def _tool_state(root: Path, relative: str) -> str:
    try:
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root):
            return 'unavailable'
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode) or (sys.platform != 'win32' and not mode & 0o111):
            return 'unavailable'
        return 'located'
    except FileNotFoundError:
        return 'missing'
    except (OSError, RuntimeError):
        return 'unavailable'


def android_sdk_status() -> dict:
    """Report located files without invoking tools, exposing paths, or authorizing devices."""
    result = {
        'sdk': {'state': 'unavailable', 'source': None, 'reason': None},
        'platformTools': 'unavailable', 'emulator': 'unavailable',
        'deviceDiscovery': 'unsupported',
    }
    sdk = result['sdk']
    try:
        roots, sdk['source'] = _sdk_candidates()
        normalized = {path.resolve() for path in roots}
        if len(normalized) != 1:
            sdk['reason'] = 'configured_roots_disagree'
            return result
        root = normalized.pop()
        if str(root).startswith(('//', '\\\\')):
            sdk['reason'] = 'configured_root_invalid'
            return result
        if not stat.S_ISDIR(root.stat().st_mode):
            sdk['reason'] = 'sdk_root_not_directory'
            return result
    except FileNotFoundError:
        sdk.update(state='missing', reason='sdk_root_missing')
        return result
    except ValueError as exc:
        reason = str(exc)
        sdk['reason'] = reason if reason in {'configured_root_invalid', 'standard_location_unavailable',
                                            'host_platform_unsupported'} else 'sdk_metadata_unavailable'
        return result
    except (OSError, RuntimeError):
        sdk['reason'] = 'sdk_metadata_unavailable'
        return result

    suffix = '.exe' if sys.platform == 'win32' else ''
    sdk['state'] = 'located'
    result['platformTools'] = _tool_state(root, 'platform-tools/adb' + suffix)
    result['emulator'] = _tool_state(root, 'emulator/emulator' + suffix)
    return result
