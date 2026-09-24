"""Owner-qualified durable engine-session mapping.

The map is deliberately independent of disposable engine generations.  Reads
are cheap, while mutations take a filesystem lock, reread, validate the
owner-wide inverse index, and publish one fsync'd replacement.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class SessionMapCollision(ValueError):
    pass


class SessionMapCorrupt(ValueError):
    pass


_EXPECTED_UNSET = object()


def _strict_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"managed session map {label} must be a nonempty canonical string")
    return value


try:
    import fcntl
except ImportError:  # pragma: no cover - Windows only
    fcntl = None
    import msvcrt


class OwnerSessionMap:
    def __init__(self, path: Path, owner: str, *, lifecycle_epoch: str | None = None):
        self.path = Path(path)
        if owner is None:
            self.owner = ""
        else:
            if not isinstance(owner, str) or owner != owner.strip():
                raise ValueError("managed session map owner must be a canonical string")
            self.owner = owner
        self.lock_path = self.path.parent / "session-map.lock"
        self.lifecycle_lock_path = self.path.parent.parent / f".session-map-{self.owner_hash}.lock"
        self.epoch_path = self.path.parent.parent / f".session-map-{self.owner_hash}.epoch"
        self.lifecycle_epoch = lifecycle_epoch
        self.quarantined: set[str] = set()

    @property
    def owner_hash(self) -> str:
        import hashlib
        return hashlib.sha256(self.owner.encode("utf-8")).hexdigest()[:24]

    def _empty(self) -> dict[str, Any]:
        return {"version": 2, "mapRevision": 0, "chats": {}}

    def _normalize(self, raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict) and raw.get("version") == 2 and isinstance(raw.get("chats"), dict):
            out = self._empty()
            map_revision = raw.get("mapRevision", 0)
            if isinstance(map_revision, bool) or not isinstance(map_revision, int) or map_revision < 0:
                raise SessionMapCorrupt("managed session map revision is malformed")
            out["mapRevision"] = map_revision
            quarantine = raw.get("quarantine", [])
            if not isinstance(quarantine, list):
                raise SessionMapCorrupt("managed session map quarantine is malformed")
            try:
                normalized_quarantine = [_strict_identifier(value, "quarantine chat") for value in quarantine]
            except ValueError as exc:
                raise SessionMapCorrupt("managed session map quarantine is malformed") from exc
            if len(set(normalized_quarantine)) != len(normalized_quarantine):
                raise SessionMapCorrupt("managed session map quarantine is duplicated")
            out["quarantine"] = normalized_quarantine
            for chat, item in raw["chats"].items():
                if not isinstance(chat, str) or not chat or chat != chat.strip() or not isinstance(item, dict):
                    raise SessionMapCorrupt("managed session map contains a malformed chat entry")
                owner = item.get("owner")
                current = item.get("current")
                revision = item.get("revision", 0)
                if owner != self.owner or not isinstance(owner, str) or not isinstance(current, str) or not current or current != current.strip() or isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                    raise SessionMapCorrupt("managed session map entry is malformed")
                raw_aliases = item.get("aliases", [])
                if not isinstance(raw_aliases, list):
                    raise SessionMapCorrupt("managed session map aliases are malformed")
                aliases = []
                for value in raw_aliases:
                    if not isinstance(value, str) or not value.strip():
                        raise SessionMapCorrupt("managed session map aliases are malformed")
                    if value != value.strip() or value == current or value in aliases:
                        raise SessionMapCorrupt("managed session map aliases are duplicated")
                    aliases.append(value)
                if len(aliases) > 16:
                    raise SessionMapCorrupt("managed session map aliases exceed the limit")
                out["chats"][chat] = {
                    "owner": owner,
                    "current": current,
                    "aliases": aliases,
                    "revision": revision,
                }
            return out
        # The original v1 format was a flat chat -> engine object.  An empty
        # object is therefore a valid empty v1 map and must be upgraded on the
        # first mutation just like a non-empty legacy map.
        if isinstance(raw, dict) and "version" not in raw:
            out = self._empty()
            for chat, current in raw.items():
                if not isinstance(chat, str) or not chat or chat != chat.strip() or not isinstance(current, str) or not current or current != current.strip():
                    raise SessionMapCorrupt("legacy managed session map contains a malformed entry")
                out["chats"][chat] = {
                    "owner": self.owner,
                    "current": current,
                    "aliases": [],
                    "revision": 0,
                }
            return out
        if raw is None:
            return self._empty()
        raise SessionMapCorrupt("managed session map has an invalid envelope")

    def _check_epoch(self, observed: str) -> None:
        if self.lifecycle_epoch is not None and observed != str(self.lifecycle_epoch):
            raise SessionMapCollision("owner lifecycle epoch is stale")

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            data = self._normalize(json.loads(self.path.read_text(encoding="utf-8")))
            self.quarantined = set(str(chat) for chat in data.get("quarantine", []))
            try:
                self._inverse(data)
            except SessionMapCollision:
                counts: dict[str, list[str]] = {}
                for chat, item in data["chats"].items():
                    for engine in [item.get("current"), *item.get("aliases", [])]:
                        if engine:
                            counts.setdefault(engine, []).append(chat)
                self.quarantined = {chat for chats in counts.values() if len(set(chats)) > 1 for chat in chats}
                data["quarantine"] = sorted(self.quarantined)
            return data
        except FileNotFoundError:
            return self._empty()
        except (OSError, json.JSONDecodeError, SessionMapCorrupt) as exc:
            raise SessionMapCorrupt("managed session map cannot be read safely") from exc

    def read(self) -> dict[str, Any]:
        with self._lock(self.lifecycle_lock_path):
            before_epoch = self._epoch()
            self._check_epoch(before_epoch)
            with self._lock(self.lock_path):
                data = self._read_unlocked()
            self._check_epoch(self._epoch())
            return data

    @staticmethod
    def _inverse(data: dict[str, Any]) -> dict[str, str]:
        inverse: dict[str, str] = {}
        for chat, item in data["chats"].items():
            for engine in [item.get("current"), *item.get("aliases", [])]:
                if not engine:
                    continue
                prior = inverse.get(engine)
                if prior is not None and prior != chat:
                    raise SessionMapCollision("engine session ID is present under multiple chats")
                inverse[engine] = chat
        return inverse

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @contextmanager
    def _lock(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise SessionMapCorrupt("managed session lock is not a regular file")
        acquired = False
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            else:  # pragma: no cover - Windows only
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            acquired = True
            try:
                yield
            finally:
                if acquired:
                    if fcntl is not None:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    else:  # pragma: no cover - Windows only
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(descriptor)

    def _epoch(self) -> str:
        try:
            return self.epoch_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return "0"

    def _mutate(self, callback):
        with self._lock(self.lifecycle_lock_path):
            before_epoch = self._epoch()
            self._check_epoch(before_epoch)
            with self._lock(self.lock_path):
                data = self._read_unlocked()
                result, changed = callback(data)
                if self._epoch() != before_epoch:
                    raise SessionMapCollision("owner lifecycle epoch changed during map mutation")
                if changed:
                    try:
                        self._write(data)
                    except OSError:
                        # A directory fsync can fail after replace has made
                        # the candidate visible. Re-read while still holding
                        # the lock and only report success when the exact
                        # candidate is durable enough to be observed.
                        observed = self._read_unlocked()
                        if observed != self._normalize(data):
                            raise
                return result

    def lookup(self, chat: str) -> dict[str, Any] | None:
        chat = _strict_identifier(chat, "chat")
        data = self.read()
        if chat in self.quarantined:
            raise SessionMapCollision("chat is quarantined due to an engine-session collision")
        item = data["chats"].get(chat)
        return copy.deepcopy(item) if item else None

    def bind(
        self,
        chat: str,
        engine: str,
        *,
        expected_map_revision: int | None = None,
        expected_current: str | None | object = _EXPECTED_UNSET,
        expected_mapping_revision: int | None = None,
    ) -> dict[str, Any]:
        chat = _strict_identifier(chat, "chat")
        engine = _strict_identifier(engine, "engine session")
        if not self.owner:
            raise ValueError("owner, stable chat and engine session are required")

        def mutate(data):
            if chat in self.quarantined:
                raise SessionMapCollision("chat is quarantined due to an engine-session collision")
            if expected_map_revision is not None and int(data.get("mapRevision") or 0) != int(expected_map_revision):
                raise ValueError("session map revision conflict")
            inverse = self._inverse(data)
            other = inverse.get(engine)
            if other is not None and other != chat:
                raise SessionMapCollision("engine session ID is already bound to another chat")
            item = data["chats"].get(chat)
            actual_current = item.get("current") if isinstance(item, dict) else None
            actual_mapping_revision = int(item.get("revision") or 0) if isinstance(item, dict) else 0
            if expected_current is not _EXPECTED_UNSET and actual_current != expected_current:
                raise ValueError("session map current-session conflict")
            if expected_mapping_revision is not None and actual_mapping_revision != int(expected_mapping_revision):
                raise ValueError("session map mapping revision conflict")
            if item is None:
                item = {"owner": self.owner, "current": engine, "aliases": [], "revision": 0}
                data["chats"][chat] = item
                changed = True
            elif item.get("owner") != self.owner:
                raise PermissionError("session map owner mismatch")
            else:
                changed = str(item.get("current") or "") != engine
                if not changed:
                    result = copy.deepcopy(item)
                    result["mapRevision"] = int(data.get("mapRevision") or 0)
                    result["mappingRevision"] = int(item.get("revision") or 0)
                    return result, False
            old = str(item.get("current") or "")
            aliases = [old, *item.get("aliases", [])] if old and old != engine else list(item.get("aliases", []))
            item["current"] = engine
            item["aliases"] = [value for value in dict.fromkeys(aliases) if value and value != engine][:16]
            item["revision"] = int(item.get("revision") or 0) + 1
            data["mapRevision"] = int(data.get("mapRevision") or 0) + 1
            self._inverse(data)
            result = copy.deepcopy(item)
            result["mapRevision"] = int(data.get("mapRevision") or 0)
            result["mappingRevision"] = int(item.get("revision") or 0)
            return result, True

        return self._mutate(mutate)

    def forget(
        self,
        chat: str,
        *,
        expected_map_revision: int | None = None,
        expected_current: str | None | object = _EXPECTED_UNSET,
        expected_mapping_revision: int | None = None,
    ) -> dict[str, int] | None:
        chat = _strict_identifier(chat, "chat")

        def mutate(data):
            if expected_map_revision is not None and int(data.get("mapRevision") or 0) != int(expected_map_revision):
                raise ValueError("session map revision conflict")
            if chat not in data["chats"]:
                if expected_current is not _EXPECTED_UNSET and expected_current is not None:
                    raise ValueError("session map current-session conflict")
                if expected_mapping_revision not in (None, 0):
                    raise ValueError("session map mapping revision conflict")
                return {"mapRevision": int(data.get("mapRevision") or 0), "mappingRevision": 0}, False
            item = data["chats"][chat]
            if expected_current is not _EXPECTED_UNSET and item.get("current") != expected_current:
                raise ValueError("session map current-session conflict")
            if expected_mapping_revision is not None and int(item.get("revision") or 0) != int(expected_mapping_revision):
                raise ValueError("session map mapping revision conflict")
            data["chats"].pop(chat, None)
            data["mapRevision"] = int(data.get("mapRevision") or 0) + 1
            return {"mapRevision": int(data.get("mapRevision") or 0), "mappingRevision": 0}, True
        return self._mutate(mutate)

    def flat_current(self) -> dict[str, str]:
        data = self.read()
        return {chat: item["current"] for chat, item in data["chats"].items() if item.get("current") and chat not in self.quarantined}

    def revisions(self, chat: str) -> tuple[int, int]:
        chat = _strict_identifier(chat, "chat")
        data = self.read()
        item = data["chats"].get(chat)
        if item is None or chat in self.quarantined:
            raise SessionMapCollision("chat is not admitted in the owner session map")
        return int(data.get("mapRevision") or 0), int(item.get("revision") or 0)

    def map_revision(self) -> int:
        return int(self.read().get("mapRevision") or 0)

    def fence(self, chat: str) -> None:
        """Quarantine a chat after an unconfirmed cross-store compensation.

        The row remains available for forensic recovery, but lookup/routing is
        fail-closed until an operator or a deterministic reconciler clears the
        fence under the same map lock.
        """
        chat = _strict_identifier(chat, "chat")
        def mutate(data):
            quarantine = list(data.get("quarantine") or [])
            if chat in quarantine:
                return None, False
            quarantine.append(chat)
            data["quarantine"] = sorted(set(quarantine))
            data["mapRevision"] = int(data.get("mapRevision") or 0) + 1
            return None, True

        self._mutate(mutate)
