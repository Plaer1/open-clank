"""Actions-only text log of a desktop-control session (owner decision M2-P05): off by default, plain text, one line per action.

Built only from the receipts the control path already produces. A line holds time, action, result, whether a person approved it
and the target as receipts already allow (app, element role, hashes of names). It never holds typed, set or pasted text, key
names, clipboard content or screenshots. When the setting is Off nothing is written. The file lives beside the application log
(`<data>/logs/desktop-actions.log`), so it is covered by the same data directory and the same admin diagnostics surface.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Mapping, Optional

MAX_BYTES = 1 << 20
_LOCK = threading.Lock()


def _path() -> str:
    from core.constants import DATA_DIR
    return os.path.join(DATA_DIR, "logs", "desktop-actions.log")


PATH_OVERRIDE: Optional[str] = None      # tests point this at a temp file


def path() -> str:
    return PATH_OVERRIDE or _path()


def _clean(v: Any) -> str:
    return "".join(c if c.isprintable() and c not in "\n\r" else " " for c in str(v))[:80]


def format_line(receipt: Mapping[str, Any], when: Optional[float] = None) -> str:
    """One line from a receipt (plain or ObservationReceiptV1 with its `detail`). No private field can reach it."""
    r = receipt.get("detail") if isinstance(receipt.get("detail"), Mapping) else receipt
    t = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() if when is None else when)) + "Z"
    parts = [t, f"action={_clean(r.get('action', 'invalid'))}", f"result={_clean(receipt.get('result', r.get('outcome', '')))}",
             f"approved={'yes' if r.get('human_approved') else 'no'}"]
    for key in ("app", "role", "name_sha256", "window_sha256", "element", "snapshot_id", "direction", "amount", "chars", "button", "x", "y"):
        if key in r:
            parts.append(f"{key.replace('_sha256', '#')}={_clean(r[key])}" if key.endswith("sha256") else f"{key}={_clean(r[key])}")
    if "keys" in r:
        parts.append(f"keys={len(r['keys'])}")        # a count, never the key names
    return " ".join(parts)


def _append(line: str) -> None:
    p = path()
    with _LOCK:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if os.path.exists(p) and os.path.getsize(p) > MAX_BYTES:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                keep = f.readlines()[-2000:]
            with open(p, "w", encoding="utf-8") as f:
                f.writelines(keep)
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def record(result: Mapping[str, Any], allow: Optional[Mapping[str, Any]]) -> None:
    """Log one finished tool result when the owner's setting is On (`allow` comes from the Host; None means not known: nothing is written)."""
    if not allow or allow.get("action_log") is not True:
        return
    receipt = result.get("receipt") if isinstance(result, Mapping) else None
    if isinstance(receipt, Mapping):
        try:
            _append(format_line(receipt))
        except OSError:
            pass


def record_event(event: str, enabled: bool) -> None:
    """A human Stop (or similar) event; same rule: only when the setting is On."""
    if enabled:
        try:
            _append(format_line({"action": event, "outcome": "stopped", "human_approved": True}))
        except OSError:
            pass


def read(limit: int = 200) -> list[str]:
    p = path()
    if not os.path.exists(p):
        return []
    with _LOCK, open(p, "r", encoding="utf-8", errors="ignore") as f:
        return [x.rstrip("\r\n") for x in f.readlines()[-max(1, min(limit, 1000)):]]


def clear() -> None:
    p = path()
    with _LOCK:
        if os.path.exists(p):
            open(p, "w").close()
