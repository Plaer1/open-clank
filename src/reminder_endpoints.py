"""Versioned reminder endpoint configuration and delivery receipts.

The settings file is user preference storage, so endpoint records contain only
opaque integration references and delivery addresses.  Secrets remain in the
existing integration/email stores.  This module keeps migration deterministic
and makes concurrent scanner/frontend delivery observable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from src.constants import DATA_DIR

CONFIG_VERSION = 1
CLAIM_LEASE_SECONDS = 300
CHANNELS = frozenset({"browser", "email", "ntfy", "webhook"})
_ID_NAMESPACE = uuid.UUID("f89f1f33-9ea6-4ea2-9f2f-1d90dc4f5a6a")
_ENDPOINT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ReminderEndpointError(ValueError):
    """A user-editable endpoint draft is invalid."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _canonical_endpoint(raw: dict[str, Any], *, index: int = 0) -> dict[str, Any]:
    if not isinstance(raw, dict) or isinstance(raw, list):
        raise ReminderEndpointError(f"Endpoint {index + 1} must be an object")
    channel = _text(raw.get("channel") or raw.get("reminder_channel")).lower()
    if channel not in CHANNELS:
        raise ReminderEndpointError(
            f"Endpoint {index + 1} channel must be browser, email, ntfy, or webhook"
        )
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ReminderEndpointError(f"Endpoint {index + 1} enabled must be boolean")

    endpoint: dict[str, Any] = {
        "channel": channel,
        "enabled": enabled,
    }
    aliases = {
        "email_to": ("email_to", "reminder_email_to", "recipient"),
        "email_account_id": ("email_account_id", "reminder_email_account_id"),
        "ntfy_topic": ("ntfy_topic", "reminder_ntfy_topic", "topic"),
        "ntfy_integration_id": (
            "ntfy_integration_id",
            "reminder_ntfy_integration_id",
        ),
        "webhook_integration_id": (
            "webhook_integration_id",
            "reminder_webhook_integration_id",
            "integration_id",
        ),
        "webhook_payload_template": (
            "webhook_payload_template",
            "reminder_webhook_payload_template",
            "payload_template",
        ),
    }
    for target, names in aliases.items():
        for name in names:
            if name in raw:
                value = raw[name]
                endpoint[target] = value if isinstance(value, bool) else _text(value)
                break

    if channel == "email":
        recipient = _text(endpoint.get("email_to"))
        if "@" not in recipient or recipient.startswith("@") or recipient.endswith("@"):
            raise ReminderEndpointError(f"Endpoint {index + 1} needs a valid email address")
        endpoint["email_to"] = recipient
    elif channel == "ntfy":
        topic = _text(endpoint.get("ntfy_topic"))
        if not topic or any(ch in topic for ch in "/?#"):
            raise ReminderEndpointError(f"Endpoint {index + 1} needs a valid ntfy topic")
        endpoint["ntfy_topic"] = topic
        if _text(endpoint.get("ntfy_integration_id")):
            endpoint["ntfy_integration_id"] = _text(endpoint["ntfy_integration_id"])
    elif channel == "webhook":
        if not _text(endpoint.get("webhook_integration_id")):
            raise ReminderEndpointError(f"Endpoint {index + 1} needs an integration")
        template = _text(endpoint.get("webhook_payload_template"))
        if template:
            try:
                json.loads(template)
            except (TypeError, ValueError) as exc:
                raise ReminderEndpointError(
                    f"Endpoint {index + 1} payload template must be valid JSON"
                ) from exc
            endpoint["webhook_payload_template"] = template

    # Display labels are optional and are never part of duplicate identity.
    if _text(raw.get("label")):
        endpoint["label"] = _text(raw["label"])[:120]

    identity = endpoint_identity(endpoint)
    supplied_id = _text(raw.get("id") or raw.get("endpoint_id"))
    if supplied_id and not _ENDPOINT_ID_RE.fullmatch(supplied_id):
        raise ReminderEndpointError(
            f"Endpoint {index + 1} id must be 1-128 letters, numbers, ., _, :, or -"
        )
    endpoint["id"] = supplied_id or str(uuid.uuid5(_ID_NAMESPACE, identity))
    return endpoint


def endpoint_identity(endpoint: dict[str, Any]) -> str:
    """Return a stable destination identity for duplicate detection."""
    channel = _text(endpoint.get("channel") or endpoint.get("reminder_channel")).lower()
    fields = {
        "channel": channel,
        "email_to": _text(endpoint.get("email_to") or endpoint.get("reminder_email_to")).lower(),
        "email_account_id": _text(endpoint.get("email_account_id") or endpoint.get("reminder_email_account_id")),
        "ntfy_topic": _text(endpoint.get("ntfy_topic") or endpoint.get("reminder_ntfy_topic")).lower(),
        "ntfy_integration_id": _text(endpoint.get("ntfy_integration_id") or endpoint.get("reminder_ntfy_integration_id")),
        "webhook_integration_id": _text(endpoint.get("webhook_integration_id") or endpoint.get("reminder_webhook_integration_id")),
    }
    return json.dumps(fields, sort_keys=True, separators=(",", ":"))


def normalize_endpoints(value: Any, *, allow_invalid: bool = False) -> dict[str, Any]:
    """Migrate supported list/dict aliases into the v1 envelope.

    ``allow_invalid`` is used only by the GET projection so a previously saved
    bad row remains visible to the user.  POST validation always rejects the
    invalid update before replacing the saved value.
    """
    if value in (None, ""):
        rows: list[Any] = []
    elif isinstance(value, list):
        rows = value
    elif isinstance(value, dict):
        version = value.get("version", value.get("schema_version", CONFIG_VERSION))
        if version != CONFIG_VERSION:
            raise ReminderEndpointError(f"Unsupported reminder endpoint version: {version}")
        rows = value.get("endpoints", value.get("targets", value.get("items", [])))
        if not isinstance(rows, list):
            raise ReminderEndpointError("reminder_endpoints.endpoints must be a list")
    else:
        raise ReminderEndpointError("reminder_endpoints must be a list or versioned object")

    result: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    seen_ids: dict[str, int] = {}
    errors: list[str] = []
    for index, raw in enumerate(rows):
        try:
            endpoint = _canonical_endpoint(raw, index=index)
            identity = endpoint_identity(endpoint)
            if identity in seen:
                raise ReminderEndpointError(
                    f"Endpoint {index + 1} duplicates endpoint {seen[identity] + 1}"
                )
            seen[identity] = index
            endpoint_id = endpoint["id"]
            if endpoint_id in seen_ids:
                raise ReminderEndpointError(
                    f"Endpoint {index + 1} id collides with endpoint {seen_ids[endpoint_id] + 1}"
                )
            seen_ids[endpoint_id] = index
            result.append(endpoint)
        except ReminderEndpointError as exc:
            if not allow_invalid:
                raise
            errors.append(str(exc))
            if isinstance(raw, dict):
                # Preserve the draft shape and mark it for the UI.  It stays
                # visible without entering the executable endpoint list.
                result.append({**raw, "invalid": str(exc)})
    return {"version": CONFIG_VERSION, "endpoints": result, **({"errors": errors} if errors else {})}


def endpoint_rows(value: Any) -> list[dict[str, Any]]:
    """Read both legacy and v1 settings values without losing invalid rows."""
    return normalize_endpoints(value, allow_invalid=True).get("endpoints", [])


def receipt_path(owner: str) -> Path:
    slug = "".join(c if c.isalnum() or c in "-_.@" else "_" for c in (_text(owner) or "default"))
    return Path(DATA_DIR) / f"reminder_receipts_{slug}.json"


@contextmanager
def _locked_receipts(path: Path) -> Iterator[dict[str, Any]]:
    """Lock and atomically rewrite one owner's receipt ledger."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            if lock.tell() == 0 and lock.read(1) == "":
                lock.seek(0); lock.write("0"); lock.flush()
            lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            yield data
            fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        finally:
            if os.name == "nt":
                import msvcrt
                lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def claim_receipt(owner: str, occurrence: str, endpoint_id: str, *, retry_unknown: bool = False) -> dict[str, Any]:
    """Claim a destination once, returning any existing outcome.

    A stale ``sending`` claim is intentionally surfaced as ``unknown`` after a
    process restart.  Callers must pass ``retry_unknown=True`` to resend it.
    """
    key = hashlib.sha256(f"{_text(owner)}\0{_text(occurrence)}\0{_text(endpoint_id)}".encode()).hexdigest()
    path = receipt_path(owner)
    with _locked_receipts(path) as data:
        existing = data.get(key)
        if isinstance(existing, dict):
            status = existing.get("status")
            if status == "sent":
                return {"key": key, **existing, "claimed": False, "unknown": False}
            if status == "sending":
                try:
                    age = time.time() - datetime.fromisoformat(str(existing.get("claimed_at"))).timestamp()
                except (TypeError, ValueError, OSError):
                    age = CLAIM_LEASE_SECONDS + 1
                if age < CLAIM_LEASE_SECONDS or not retry_unknown:
                    return {"key": key, **existing, "claimed": False, "unknown": age >= CLAIM_LEASE_SECONDS}
                existing.setdefault("attempts", []).append({"token": existing.get("attempt_token"), "status": "unknown", "updated_at": datetime.now(timezone.utc).isoformat()})
            elif status == "unknown" and not retry_unknown:
                return {"key": key, **existing, "claimed": False, "unknown": True}
            # Errors are safe to retry; an explicit unknown retry creates a
            # fresh claim while retaining the prior attempt for auditability.
        now = datetime.now(timezone.utc).isoformat()
        attempt_token = str(uuid.uuid4())
        prior_attempts = list(existing.get("attempts", [])) if isinstance(existing, dict) else []
        data[key] = {
            "owner": _text(owner),
            "occurrence": _text(occurrence),
            "endpoint_id": _text(endpoint_id),
            "status": "sending",
            "claimed_at": now,
            "updated_at": now,
            "attempt_token": attempt_token,
            "attempts": prior_attempts,
        }
        return {"key": key, **data[key], "claimed": True, "unknown": False}


def finish_receipt(claim: dict[str, Any], status: str, *, error: str = "") -> None:
    if status not in {"sent", "error", "unknown"}:
        raise ValueError(f"Unsupported receipt status {status!r}")
    path = receipt_path(str(claim.get("owner") or ""))
    with _locked_receipts(path) as data:
        row = data.get(claim.get("key"))
        if not isinstance(row, dict) or row.get("attempt_token") != claim.get("attempt_token"):
            return
        row.update({"status": status, "updated_at": datetime.now(timezone.utc).isoformat()})
        if error:
            row["error"] = str(error)[:500]
        elif "error" in row:
            row.pop("error", None)
