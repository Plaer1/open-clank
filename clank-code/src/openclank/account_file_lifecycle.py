"""Crash-replayable owner lifecycle for shared JSON and skill files.

This adapter deliberately has no knowledge of the authentication route or of
live application globals.  Every authority path is injected, which makes the
same implementation usable by a persisted account saga and by isolated
verification.  Manifests contain only counts and cryptographic item tokens;
preference, research, memory, and skill content never enters a durable receipt.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from core.atomic_io import (
    AtomicFileChange,
    atomic_write_batch,
    fingerprint_bytes,
)
from services.memory.skill_lifecycle import locked


_SCHEMA_VERSION = 1
_DOMAINS = (
    "preferences",
    "completed_research",
    "legacy_memory",
    "skill_documents",
    "skill_usage",
)
_TOKEN_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FRONTMATTER_OPEN_RE = re.compile(r"\A---[ \t]*\r?\n")
_FRONTMATTER_CLOSE_RE = re.compile(r"(?m)^---[ \t]*\r?$", re.MULTILINE)
_OWNER_LINE_RE = re.compile(
    r"(?mi)^(?P<prefix>[ \t]*owner[ \t]*:[ \t]*)"
    r"(?P<value>[^\r\n#]*?)"
    r"(?P<suffix>[ \t]*(?:#[^\r\n]*)?)(?P<cr>\r?)$"
)


class AccountFileLifecycleError(RuntimeError):
    """The file-backed owner closure is malformed, stale, or conflicting."""


@dataclass(frozen=True)
class AccountFileLifecyclePaths:
    """All file authorities used by :class:`AccountFileOwnerLifecycle`."""

    preferences_file: Path
    completed_research_dir: Path
    legacy_memory_file: Path
    skills_dir: Path
    lock_marker: Path | None = None
    include_skills: bool = True

    def normalized(self) -> "AccountFileLifecyclePaths":
        preferences = Path(self.preferences_file).resolve()
        research = Path(self.completed_research_dir).resolve()
        memory = Path(self.legacy_memory_file).resolve()
        skills = Path(self.skills_dir).resolve()
        marker = Path(
            self.lock_marker
            or (preferences.parent / ".account-file-lifecycle" / "state")
        ).resolve()
        return AccountFileLifecyclePaths(
            preferences_file=preferences,
            completed_research_dir=research,
            legacy_memory_file=memory,
            skills_dir=skills,
            lock_marker=marker,
            include_skills=bool(self.include_skills),
        )


@dataclass(frozen=True)
class _Item:
    domain: str
    token: str


@dataclass(frozen=True)
class _JsonDocument:
    path: Path
    value: Any
    fingerprint: str


@dataclass(frozen=True)
class _SkillDocument:
    path: Path
    text: str
    fingerprint: str
    owner: str | None
    owner_span: tuple[int, int] | None
    quote: str


@dataclass(frozen=True)
class _Snapshot:
    preferences: _JsonDocument | None
    research: tuple[_JsonDocument, ...]
    memory: _JsonDocument | None
    skills: tuple[_SkillDocument, ...]
    usage: _JsonDocument | None


def _owner(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized or "\x00" in normalized:
        raise AccountFileLifecycleError("file lifecycle owner is required")
    return normalized


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AccountFileLifecycleError(
            "file lifecycle authority is not canonical JSON"
        ) from exc


def _json_output(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def _token(domain: str, locator: str, value: Any) -> str:
    digest = hashlib.sha256()
    digest.update(domain.encode("utf-8"))
    digest.update(b"\0")
    digest.update(locator.encode("utf-8", errors="surrogatepass"))
    digest.update(b"\0")
    digest.update(_canonical_json(value))
    return "sha256:" + digest.hexdigest()


def _inventory(items: Iterable[_Item]) -> dict[str, Any]:
    material = list(items)
    counts = {domain: 0 for domain in _DOMAINS}
    tokens: list[str] = []
    for item in material:
        counts[item.domain] += 1
        tokens.append(item.token)
    tokens.sort()
    fingerprint_material = {"counts": counts, "items": tokens}
    return {
        "schema_version": _SCHEMA_VERSION,
        "count": len(tokens),
        "counts": counts,
        "fingerprint": "sha256:"
        + hashlib.sha256(_canonical_json(fingerprint_material)).hexdigest(),
        "items": tokens,
    }


def _validate_inventory(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema_version") != _SCHEMA_VERSION:
        raise AccountFileLifecycleError("file lifecycle inventory schema is invalid")
    raw_counts = value.get("counts")
    raw_items = value.get("items")
    if not isinstance(raw_counts, Mapping) or not isinstance(raw_items, list):
        raise AccountFileLifecycleError("file lifecycle inventory is malformed")
    if set(raw_counts) != set(_DOMAINS):
        raise AccountFileLifecycleError("file lifecycle inventory domains are invalid")
    try:
        counts = {domain: int(raw_counts[domain]) for domain in _DOMAINS}
    except (TypeError, ValueError) as exc:
        raise AccountFileLifecycleError("file lifecycle counts are invalid") from exc
    if any(value < 0 for value in counts.values()):
        raise AccountFileLifecycleError("file lifecycle counts are invalid")
    items = [str(item) for item in raw_items]
    if any(not _TOKEN_RE.fullmatch(item) for item in items):
        raise AccountFileLifecycleError("file lifecycle item token is invalid")
    # Recompute directly because an item token intentionally does not expose
    # its domain; the persisted per-domain counts remain independently exact.
    candidate_counts = counts
    candidate_items = sorted(items)
    material = {"counts": candidate_counts, "items": candidate_items}
    fingerprint = "sha256:" + hashlib.sha256(_canonical_json(material)).hexdigest()
    if (
        int(value.get("count", -1)) != len(candidate_items)
        or sum(candidate_counts.values()) != len(candidate_items)
        or value.get("fingerprint") != fingerprint
    ):
        raise AccountFileLifecycleError("file lifecycle inventory fingerprint is invalid")
    return {
        "schema_version": _SCHEMA_VERSION,
        "count": len(candidate_items),
        "counts": candidate_counts,
        "fingerprint": fingerprint,
        "items": candidate_items,
    }


def _combine_inventories(*inventories: Mapping[str, Any]) -> dict[str, Any]:
    validated = [_validate_inventory(value) for value in inventories]
    counts = {domain: 0 for domain in _DOMAINS}
    tokens: list[str] = []
    for inventory in validated:
        for domain in _DOMAINS:
            counts[domain] += int(inventory["counts"][domain])
        tokens.extend(inventory["items"])
    tokens.sort()
    material = {"counts": counts, "items": tokens}
    return {
        "schema_version": _SCHEMA_VERSION,
        "count": len(tokens),
        "counts": counts,
        "fingerprint": "sha256:"
        + hashlib.sha256(_canonical_json(material)).hexdigest(),
        "items": tokens,
    }


def _same_inventory(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return _validate_inventory(left) == _validate_inventory(right)


def _read_json(path: Path, *, expected: type, label: str) -> _JsonDocument | None:
    if path.is_symlink():
        raise AccountFileLifecycleError(f"{label} authority may not be a symlink")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AccountFileLifecycleError(f"{label} authority is unreadable") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AccountFileLifecycleError(f"{label} authority is malformed") from exc
    if not isinstance(value, expected):
        raise AccountFileLifecycleError(f"{label} authority has the wrong schema")
    # Bind compare-and-swap to the bytes that were actually parsed.  Reading
    # the path a second time for its fingerprint would permit a replacement
    # between reads and could apply a transform derived from stale content.
    fingerprint = fingerprint_bytes(raw)
    return _JsonDocument(path=path, value=value, fingerprint=fingerprint)


def _files(
    root: Path,
    pattern: str,
    *,
    label: str,
    recursive: bool = True,
) -> Sequence[Path]:
    if root.is_symlink():
        raise AccountFileLifecycleError(f"{label} root may not be a symlink")
    if not root.exists():
        return ()
    if not root.is_dir():
        raise AccountFileLifecycleError(f"{label} root is not a directory")
    result: list[Path] = []
    resolved_root = root.resolve()
    candidates = root.rglob(pattern) if recursive else root.glob(pattern)
    for path in sorted(candidates):
        if path.is_symlink() or not path.is_file():
            raise AccountFileLifecycleError(f"{label} authority is not a regular file")
        try:
            path.resolve().relative_to(resolved_root)
        except ValueError as exc:  # pragma: no cover - symlink guard is primary
            raise AccountFileLifecycleError(f"{label} authority escaped its root") from exc
        result.append(path)
    return tuple(result)


def _skill_owner(text: str) -> tuple[str | None, tuple[int, int] | None, str]:
    opening = _FRONTMATTER_OPEN_RE.match(text)
    if opening is None:
        return None, None, ""
    closing = _FRONTMATTER_CLOSE_RE.search(text, opening.end())
    if closing is None:
        raise AccountFileLifecycleError("SKILL.md frontmatter is unterminated")
    matches = list(_OWNER_LINE_RE.finditer(text, opening.end(), closing.start()))
    if not matches:
        return None, None, ""
    if len(matches) != 1:
        raise AccountFileLifecycleError("SKILL.md has ambiguous owner frontmatter")
    match = matches[0]
    raw = match.group("value")
    leading = len(raw) - len(raw.lstrip())
    trailing = len(raw) - len(raw.rstrip())
    start = match.start("value") + leading
    end = match.end("value") - trailing
    scalar = text[start:end]
    quote = ""
    if len(scalar) >= 2 and scalar[0] in {"'", '"'} and scalar[-1] == scalar[0]:
        quote = scalar[0]
        scalar = scalar[1:-1]
        start += 1
        end -= 1
    if not scalar.strip() or "\x00" in scalar:
        raise AccountFileLifecycleError("SKILL.md owner frontmatter is invalid")
    return scalar.strip().lower(), (start, end), quote


class AccountFileOwnerLifecycle:
    """Exact rename, tombstone, compensation, purge, and replay adapter."""

    def __init__(self, paths: AccountFileLifecyclePaths):
        self.paths = paths.normalized()

    def _snapshot(self) -> _Snapshot:
        preferences = _read_json(
            self.paths.preferences_file,
            expected=dict,
            label="preferences",
        )
        if preferences is not None:
            users = preferences.value.get("_users")
            if users is None and preferences.value:
                raise AccountFileLifecycleError(
                    "legacy flat preferences have no exact owner authority"
                )
            if users is not None and not isinstance(users, dict):
                raise AccountFileLifecycleError("preferences owner map is malformed")

        research: list[_JsonDocument] = []
        for path in _files(
            self.paths.completed_research_dir,
            "*.json",
            label="completed research",
            recursive=False,
        ):
            document = _read_json(path, expected=dict, label="completed research")
            assert document is not None
            research.append(document)

        memory = _read_json(
            self.paths.legacy_memory_file,
            expected=list,
            label="legacy memory",
        )

        skills: list[_SkillDocument] = []
        skill_paths = (
            _files(self.paths.skills_dir, "SKILL.md", label="skills")
            if self.paths.include_skills
            else ()
        )
        for path in skill_paths:
            try:
                raw = path.read_bytes()
                text = raw.decode("utf-8")
            except (OSError, UnicodeError) as exc:
                raise AccountFileLifecycleError("SKILL.md is unreadable") from exc
            fingerprint = fingerprint_bytes(raw)
            skill_owner, span, quote = _skill_owner(text)
            skills.append(
                _SkillDocument(path, text, fingerprint, skill_owner, span, quote)
            )

        usage_path = self.paths.skills_dir / "_usage.json"
        usage = (
            _read_json(usage_path, expected=dict, label="skill usage")
            if self.paths.include_skills
            else None
        )
        return _Snapshot(
            preferences=preferences,
            research=tuple(research),
            memory=memory,
            skills=tuple(skills),
            usage=usage,
        )

    def _items(self, snapshot: _Snapshot, owner: str) -> list[_Item]:
        owner = _owner(owner)
        items: list[_Item] = []

        if snapshot.preferences is not None:
            users = snapshot.preferences.value.get("_users") or {}
            matching = [
                (str(key), value)
                for key, value in users.items()
                if str(key).strip().lower() == owner
            ]
            if len(matching) > 1:
                raise AccountFileLifecycleError("preferences contain duplicate owner keys")
            for _key, value in matching:
                items.append(_Item("preferences", _token("preferences", "record", value)))

        research_root = self.paths.completed_research_dir
        for document in snapshot.research:
            value = document.value
            if str(value.get("owner") or "").strip().lower() != owner:
                continue
            normalized = dict(value)
            normalized["owner"] = "@owner"
            relative = document.path.relative_to(research_root).as_posix()
            items.append(
                _Item(
                    "completed_research",
                    _token("completed_research", relative, normalized),
                )
            )

        if snapshot.memory is not None:
            for entry in snapshot.memory.value:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("owner") or "").strip().lower() != owner:
                    continue
                normalized = dict(entry)
                normalized["owner"] = "@owner"
                items.append(
                    _Item(
                        "legacy_memory",
                        # A list index is not identity: purging an earlier
                        # owner's row must not change another owner's receipt.
                        # Duplicate equal rows remain exact because inventory
                        # item tokens are a multiset rather than a set.
                        _token("legacy_memory", "entry", normalized),
                    )
                )

        skills_root = self.paths.skills_dir
        for document in snapshot.skills:
            if document.owner != owner:
                continue
            assert document.owner_span is not None
            start, end = document.owner_span
            normalized = document.text[:start] + "@owner" + document.text[end:]
            relative = document.path.relative_to(skills_root).as_posix()
            items.append(
                _Item("skill_documents", _token("skill_documents", relative, normalized))
            )

        if snapshot.usage is not None:
            for key, value in snapshot.usage.value.items():
                owner_part, separator, skill_part = str(key).partition("::")
                if separator and owner_part.strip().lower() == owner:
                    items.append(
                        _Item(
                            "skill_usage",
                            _token("skill_usage", "@owner::" + skill_part, value),
                        )
                    )
        return items

    def owner_inventory(self, owner: str) -> dict[str, Any]:
        """Return content-silent exact counts and item fingerprints."""

        owner = _owner(owner)
        with locked(str(self.paths.lock_marker)):
            return _inventory(self._items(self._snapshot(), owner))

    def preview_rename(self, source_owner: str, target_owner: str) -> dict[str, Any]:
        """Freeze an exact rename closure; a populated target fails closed."""

        source_owner = _owner(source_owner)
        target_owner = _owner(target_owner)
        if source_owner == target_owner:
            raise AccountFileLifecycleError("source and target owners must differ")
        with locked(str(self.paths.lock_marker)):
            snapshot = self._snapshot()
            source = _inventory(self._items(snapshot, source_owner))
            target = _inventory(self._items(snapshot, target_owner))
            if target["count"]:
                raise AccountFileLifecycleError(
                    "target file owner already contains durable state"
                )
            return {
                "schema_version": _SCHEMA_VERSION,
                "source": source,
                "target": target,
                "closure": _combine_inventories(source, target),
            }

    @staticmethod
    def _manifest(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping) or value.get("schema_version") != _SCHEMA_VERSION:
            raise AccountFileLifecycleError("file lifecycle manifest schema is invalid")
        source = _validate_inventory(value.get("source"))
        target = _validate_inventory(value.get("target"))
        closure = _validate_inventory(value.get("closure"))
        if target["count"] or not _same_inventory(
            closure, _combine_inventories(source, target)
        ):
            raise AccountFileLifecycleError("file lifecycle manifest is inconsistent")
        return {
            "schema_version": _SCHEMA_VERSION,
            "source": source,
            "target": target,
            "closure": closure,
        }

    def _validated_pair(
        self,
        snapshot: _Snapshot,
        source_owner: str,
        target_owner: str,
        manifest: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        source = _inventory(self._items(snapshot, source_owner))
        target = _inventory(self._items(snapshot, target_owner))
        current = _combine_inventories(source, target)
        if not _same_inventory(current, manifest["closure"]):
            raise AccountFileLifecycleError(
                "source and target file state conflicts with the frozen preview"
            )
        return source, target

    def _changes(
        self,
        snapshot: _Snapshot,
        source_owner: str,
        target_owner: str | None,
    ) -> list[AtomicFileChange]:
        changes: list[AtomicFileChange] = []

        if snapshot.preferences is not None:
            data = dict(snapshot.preferences.value)
            users = dict(data.get("_users") or {})
            source_keys = [
                key
                for key in users
                if str(key).strip().lower() == source_owner
            ]
            if len(source_keys) > 1:
                raise AccountFileLifecycleError("preferences contain duplicate owner keys")
            if source_keys:
                source_key = source_keys[0]
                if target_owner is None:
                    users.pop(source_key)
                else:
                    if any(
                        str(key).strip().lower() == target_owner
                        for key in users
                        if key != source_key
                    ):
                        raise AccountFileLifecycleError(
                            "source and target preferences both exist"
                        )
                    users[target_owner] = users.pop(source_key)
                data["_users"] = users
                changes.append(
                    AtomicFileChange(
                        str(snapshot.preferences.path),
                        _json_output(data),
                        expected_fingerprint=snapshot.preferences.fingerprint,
                    )
                )

        for document in snapshot.research:
            if str(document.value.get("owner") or "").strip().lower() != source_owner:
                continue
            if target_owner is None:
                data = None
            else:
                value = dict(document.value)
                value["owner"] = target_owner
                data = _json_output(value)
            changes.append(
                AtomicFileChange(
                    str(document.path),
                    data,
                    expected_fingerprint=document.fingerprint,
                )
            )

        if snapshot.memory is not None:
            changed = False
            entries: list[Any] = []
            for entry in snapshot.memory.value:
                if (
                    isinstance(entry, dict)
                    and str(entry.get("owner") or "").strip().lower() == source_owner
                ):
                    changed = True
                    if target_owner is not None:
                        replacement = dict(entry)
                        replacement["owner"] = target_owner
                        entries.append(replacement)
                else:
                    entries.append(entry)
            if changed:
                changes.append(
                    AtomicFileChange(
                        str(snapshot.memory.path),
                        _json_output(entries),
                        expected_fingerprint=snapshot.memory.fingerprint,
                    )
                )

        for document in snapshot.skills:
            if document.owner != source_owner:
                continue
            if target_owner is None:
                data = None
            else:
                assert document.owner_span is not None
                start, end = document.owner_span
                data = (
                    document.text[:start]
                    + target_owner
                    + document.text[end:]
                ).encode("utf-8")
            changes.append(
                AtomicFileChange(
                    str(document.path),
                    data,
                    expected_fingerprint=document.fingerprint,
                )
            )

        if snapshot.usage is not None:
            changed = False
            usage: dict[str, Any] = {}
            for raw_key, value in snapshot.usage.value.items():
                key = str(raw_key)
                owner_part, separator, skill_part = key.partition("::")
                if separator and owner_part.strip().lower() == source_owner:
                    changed = True
                    if target_owner is None:
                        continue
                    replacement = target_owner + "::" + skill_part
                    if replacement in snapshot.usage.value or replacement in usage:
                        raise AccountFileLifecycleError(
                            "source and target skill usage both exist"
                        )
                    usage[replacement] = value
                else:
                    if key in usage:
                        raise AccountFileLifecycleError("skill usage keys are ambiguous")
                    usage[key] = value
            if changed:
                changes.append(
                    AtomicFileChange(
                        str(snapshot.usage.path),
                        _json_output(usage),
                        expected_fingerprint=snapshot.usage.fingerprint,
                    )
                )
        return sorted(changes, key=lambda change: change.path)

    def reconcile_rename(
        self,
        source_owner: str,
        target_owner: str,
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Resume a rename from any validated partial source/target split."""

        source_owner = _owner(source_owner)
        target_owner = _owner(target_owner)
        if source_owner == target_owner:
            raise AccountFileLifecycleError("source and target owners must differ")
        frozen = self._manifest(manifest)
        with locked(str(self.paths.lock_marker)):
            snapshot = self._snapshot()
            self._validated_pair(snapshot, source_owner, target_owner, frozen)
            changes = self._changes(snapshot, source_owner, target_owner)
            if changes:
                atomic_write_batch(changes)
            return self._verify_pair(
                source_owner,
                target_owner,
                frozen,
                expected="staged",
            )

    def rename_owner(
        self,
        source_owner: str,
        target_owner: str,
        manifest: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        frozen = manifest or self.preview_rename(source_owner, target_owner)
        return self.reconcile_rename(source_owner, target_owner, frozen)

    def stage_to_tombstone(
        self,
        source_owner: str,
        tombstone_owner: str,
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self.reconcile_rename(source_owner, tombstone_owner, manifest)

    def compensate(
        self,
        source_owner: str,
        target_owner: str,
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Restore a staged target to the original owner, idempotently."""

        source_owner = _owner(source_owner)
        target_owner = _owner(target_owner)
        frozen = self._manifest(manifest)
        with locked(str(self.paths.lock_marker)):
            snapshot = self._snapshot()
            self._validated_pair(snapshot, source_owner, target_owner, frozen)
            changes = self._changes(snapshot, target_owner, source_owner)
            if changes:
                atomic_write_batch(changes)
            return self._verify_pair(
                source_owner,
                target_owner,
                frozen,
                expected="restored",
            )

    @staticmethod
    def _purge_inventory(value: Mapping[str, Any]) -> dict[str, Any]:
        if "closure" in value:
            return AccountFileOwnerLifecycle._manifest(value)["closure"]
        return _validate_inventory(value)

    def purge_owner(
        self,
        owner: str,
        expected: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Purge one exact owner; a replay may contain any expected subset."""

        owner = _owner(owner)
        with locked(str(self.paths.lock_marker)):
            snapshot = self._snapshot()
            before = _inventory(self._items(snapshot, owner))
            frozen = self._purge_inventory(expected or before)
            if not (
                Counter(before["items"]) <= Counter(frozen["items"])
                and all(
                    before["counts"][domain] <= frozen["counts"][domain]
                    for domain in _DOMAINS
                )
            ):
                raise AccountFileLifecycleError(
                    "owner file state conflicts with the frozen purge preview"
                )
            changes = self._changes(snapshot, owner, None)
            if changes:
                atomic_write_batch(changes)
            after = _inventory(self._items(self._snapshot(), owner))
            if after["count"]:
                raise AccountFileLifecycleError("owner file purge did not converge")
            return {
                "state": "purged",
                "before": before,
                "after": after,
                "deleted_now_counts": {
                    domain: before["counts"][domain] for domain in _DOMAINS
                },
                # A retry may begin after some standalone files were already
                # removed.  Preserve the frozen operation total so the final
                # durable receipt does not under-report prior attempts.
                "deleted_total_counts": {
                    domain: frozen["counts"][domain] for domain in _DOMAINS
                },
            }

    def _verify_pair(
        self,
        source_owner: str,
        target_owner: str,
        manifest: Mapping[str, Any],
        *,
        expected: str,
    ) -> dict[str, Any]:
        snapshot = self._snapshot()
        source = _inventory(self._items(snapshot, source_owner))
        target = _inventory(self._items(snapshot, target_owner))
        empty = _inventory(())
        closure = _validate_inventory(manifest["closure"])
        if expected == "staged":
            valid = _same_inventory(source, empty) and _same_inventory(target, closure)
        elif expected == "restored":
            valid = _same_inventory(source, closure) and _same_inventory(target, empty)
        else:  # pragma: no cover - private call contract
            raise ValueError(expected)
        if not valid:
            raise AccountFileLifecycleError(
                f"file lifecycle verification did not reach {expected} state"
            )
        return {"state": expected, "source": source, "target": target}

    def verify(
        self,
        source_owner: str,
        target_owner: str,
        manifest: Mapping[str, Any],
        *,
        expected: str,
    ) -> dict[str, Any]:
        """Verify a staged or restored exact closure without mutation."""

        source_owner = _owner(source_owner)
        target_owner = _owner(target_owner)
        frozen = self._manifest(manifest)
        with locked(str(self.paths.lock_marker)):
            return self._verify_pair(
                source_owner,
                target_owner,
                frozen,
                expected=expected,
            )


__all__ = [
    "AccountFileLifecycleError",
    "AccountFileLifecyclePaths",
    "AccountFileOwnerLifecycle",
]
