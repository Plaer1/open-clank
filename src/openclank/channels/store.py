"""One encrypted JSON file for channel state (integrations pattern): switches, secrets, pairing, allow-lists, conversation to session map.

Secrets are encrypted with the app key (``src.secret_storage``) and are never returned by any method except ``secret``,
which only the runner and adapters call. The conversation map is bounded: 200 entries, oldest evicted."""
from __future__ import annotations
import json
import os
import threading
import time
from typing import Any, Callable

from core.atomic_io import atomic_write_json
from core.platform_compat import safe_chmod

MAX_CONVERSATIONS = 200
MAX_ALLOWED = 50
MAX_NOTIFIED = 200
REQUIRED = {"telegram": ("token",), "matrix": ("homeserver", "access_token"), "discord": ("token",), "feishu": ("app_id", "app_secret"), "wecom": ("bot_id", "bot_secret")}   # settings names that must be present


class ChannelStore:
    def __init__(self, path: str, *, enc: Callable[[str], str] | None = None, dec: Callable[[str], str] | None = None,
                 clock: Callable[[], float] = time.time):
        if enc is None or dec is None:
            from src.secret_storage import encrypt, decrypt
            enc, dec = enc or encrypt, dec or decrypt
        self.path, self._enc, self._dec, self._clock, self._lock = path, enc, dec, clock, threading.RLock()
        self._data = self._load()

    # -- persistence
    def _load(self) -> dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and data.get("version") == 1:
                return data
        except (OSError, ValueError):
            pass
        return {"version": 1, "owner": None, "token": None, "channels": {}, "bindings": {}, "notified": []}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        atomic_write_json(self.path, self._data)
        safe_chmod(self.path, 0o600)

    def _channel(self, name: str) -> dict[str, Any]:
        return self._data["channels"].setdefault(name, {"enabled": False, "secrets": {}, "settings": {}, "senders": [], "groups": [],
                                                       "pending": [], "approve_in_chat": None})

    # -- owner and the channel API token (encrypted)
    def owner(self) -> str | None:
        return self._data.get("owner")

    def set_owner(self, owner: str) -> None:
        with self._lock:
            if self._data.get("owner") not in (None, owner):
                raise PermissionError("channels belong to another owner")
            self._data["owner"] = owner
            self._save()

    def token(self) -> str | None:
        raw = self._data.get("token")
        return self._dec(raw) if raw else None

    def set_token(self, value: str | None) -> None:
        with self._lock:
            self._data["token"] = self._enc(value) if value else None
            self._save()

    # -- switches, secrets, settings
    def enabled(self, name: str) -> bool:
        return bool(self._data["channels"].get(name, {}).get("enabled"))

    def set_enabled(self, name: str, value: bool) -> None:
        with self._lock:
            self._channel(name)["enabled"] = bool(value)
            self._save()

    def set_secret(self, name: str, key: str, value: str) -> None:
        with self._lock:
            self._channel(name)["secrets"][key] = self._enc(value) if value else None
            self._save()

    def secret(self, name: str, key: str) -> str | None:
        raw = self._data["channels"].get(name, {}).get("secrets", {}).get(key)
        return self._dec(raw) if raw else None

    def set_setting(self, name: str, key: str, value: str) -> None:
        with self._lock:
            self._channel(name)["settings"][key] = str(value)[:512]
            self._save()

    def setting(self, name: str, key: str) -> str | None:
        return self._data["channels"].get(name, {}).get("settings", {}).get(key)

    def configured(self, name: str) -> bool:
        """True when every required value is present. Secrets are never revealed, only counted."""
        cfg = self._data["channels"].get(name)
        if not cfg or name not in REQUIRED:
            return False
        return all(bool(cfg["secrets"].get(k)) or bool(cfg["settings"].get(k)) for k in REQUIRED[name])

    # -- pairing and allow-lists
    def _prune(self, cfg: dict) -> None:
        now = self._clock()
        cfg["pending"] = [p for p in cfg["pending"] if p["expires"] > now]

    def pending(self, name: str) -> list[dict[str, Any]]:
        with self._lock:
            cfg = self._channel(name); self._prune(cfg)
            return [dict(p) for p in cfg["pending"]]

    def add_pending(self, name: str, sender: str, label: str, code: str, expires: float) -> None:
        with self._lock:
            cfg = self._channel(name); self._prune(cfg)
            cfg["pending"].append({"sender": sender, "label": label[:80], "code": code, "expires": expires})
            self._save()

    def resolve_pending(self, name: str, code: str, approve: bool) -> str | None:
        with self._lock:
            cfg = self._channel(name); self._prune(cfg)
            for entry in cfg["pending"]:
                if entry["code"] == code.strip().upper():
                    cfg["pending"].remove(entry)
                    if approve and entry["sender"] not in cfg["senders"] and len(cfg["senders"]) < MAX_ALLOWED:
                        cfg["senders"].append(entry["sender"])
                    self._save()
                    return entry["sender"]
            return None

    def senders(self, name: str) -> list[str]:
        return list(self._data["channels"].get(name, {}).get("senders", []))

    def groups(self, name: str) -> list[str]:
        return list(self._data["channels"].get(name, {}).get("groups", []))

    def allow(self, name: str, kind: str, ident: str, allowed: bool) -> None:
        field = {"sender": "senders", "group": "groups"}[kind]
        with self._lock:
            items = self._channel(name)[field]
            if allowed and ident not in items and len(items) < MAX_ALLOWED:
                items.append(ident)
            if not allowed and ident in items:
                items.remove(ident)
            self._save()

    def approve_in_chat(self, name: str) -> str | None:
        return self._data["channels"].get(name, {}).get("approve_in_chat")

    def set_approve_in_chat(self, name: str, sender: str | None) -> None:
        with self._lock:
            self._channel(name)["approve_in_chat"] = sender
            self._save()

    # -- conversation to session (bounded, oldest evicted)
    def binding(self, key: str) -> str | None:
        entry = self._data["bindings"].get(key)
        return entry["session_id"] if entry else None

    def bind(self, key: str, session_id: str) -> None:
        with self._lock:
            bindings = self._data["bindings"]
            bindings.pop(key, None)
            bindings[key] = {"session_id": session_id, "at": self._clock()}
            while len(bindings) > MAX_CONVERSATIONS:
                bindings.pop(next(iter(bindings)))
            self._save()

    def unbind(self, key: str) -> None:
        with self._lock:
            self._data["bindings"].pop(key, None)
            self._save()

    def binding_count(self) -> int:
        return len(self._data["bindings"])

    # -- one-time notices (encrypted rooms)
    def notified(self, key: str) -> bool:
        return key in self._data["notified"]

    def mark_notified(self, key: str) -> None:
        with self._lock:
            if key not in self._data["notified"]:
                self._data["notified"].append(key)
                del self._data["notified"][:-MAX_NOTIFIED]
                self._save()

    def public_status(self, name: str) -> dict[str, Any]:
        """Safe for any response: booleans, counts and pairing codes the owner must see; never a secret."""
        cfg = self._data["channels"].get(name, {})
        now = self._clock()
        return {"enabled": bool(cfg.get("enabled")), "configured": self.configured(name),
                "senders": list(cfg.get("senders", [])), "groups": list(cfg.get("groups", [])),
                "group_mention_evidence": ("unavailable" if name == "wecom" else
                    "supported" if name in ("telegram", "matrix", "discord", "feishu") else "unknown"),
                "approve_in_chat": cfg.get("approve_in_chat"),
                "pending": [{"code": p["code"], "label": p["label"], "sender": p["sender"], "expires_in": max(0, int(p["expires"] - now))}
                            for p in cfg.get("pending", []) if p["expires"] > now],
                "settings": dict(cfg.get("settings", {}))}
