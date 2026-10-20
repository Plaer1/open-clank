"""Account lifecycle for owner sidecars and SQL-owned file children.

The common SQL lifecycle deliberately knows nothing about bytes referenced by
its rows.  This adapter closes that gap without putting filenames, paths, user
text, or email content in the public account-operation manifest.  Destructive
staging uses a private, operation-token journal so a process crash can either
restore the exact files before the auth barrier or finish deleting them after
it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from core.atomic_io import AtomicFileChange, atomic_write_batch, atomic_write_json
from core.database import FilesImageResource, PublishedFile, Session
from services.memory.skill_lifecycle import locked


_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_JOB_SUFFIXES = (
    ".log",
    ".exit",
    ".child.pid",
    ".stdin",
    ".stdin.ready",
    ".spec.json",
    ".cmd.sh",
    ".sh",
)


class AccountAncillaryLifecycleError(RuntimeError):
    """Ancillary owner state is stale, unsafe, or cannot be converged."""


@dataclass(frozen=True)
class AccountAncillaryPaths:
    data_dir: Path
    gallery_root: Path
    published_root: Path
    background_jobs_file: Path
    background_jobs_dir: Path
    scheduled_email_db: Path
    agent_todos_dir: Path
    file_trash_dir: Path
    journal_path: Path

    def normalized(self) -> "AccountAncillaryPaths":
        return AccountAncillaryPaths(
            **{
                field: Path(getattr(self, field)).resolve()
                for field in self.__dataclass_fields__
            }
        )


def _owner(value: Any) -> str:
    result = str(value or "").strip().lower()
    if not result or "\x00" in result:
        raise AccountAncillaryLifecycleError("ancillary lifecycle owner is required")
    return result


def _slug(owner: str) -> str:
    return "".join(
        char if (char.isalnum() or char in "-_.@") else "_" for char in owner
    )


def _trash_key(owner: str) -> str:
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()[:20]


def _digest(items: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(sorted(items), separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _inventory(items: Mapping[str, list[str]]) -> dict[str, Any]:
    counts = {key: len(value) for key, value in sorted(items.items())}
    fingerprints = {key: _digest(value) for key, value in sorted(items.items())}
    return {
        "schema_version": 1,
        "count": sum(counts.values()),
        "counts": counts,
        "fingerprints": fingerprints,
        "fingerprint": _digest(
            [f"{key}:{counts[key]}:{fingerprints[key]}" for key in sorted(counts)]
        ),
    }


def _same(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return (
        int(left.get("count", -1)) == int(right.get("count", -2))
        and left.get("counts") == right.get("counts")
        and str(left.get("fingerprint") or "") == str(right.get("fingerprint") or "!")
    )


class AccountAncillaryLifecycle:
    """Converge noncanonical owner files, jobs, and cache rows.

    Active background jobs and actively sending/scheduled emails fail
    preflight.  Guessing how long either may run would make account deletion
    nondeterministic and could permit work to escape after its principal is
    removed.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        paths: AccountAncillaryPaths,
    ) -> None:
        self._session_factory = session_factory
        self.paths = paths.normalized()

    def _state_paths(self, owner: str) -> tuple[Path, Path, Path]:
        slug = _slug(owner)
        return (
            self.paths.data_dir / f"note_pings_{slug}.json",
            self.paths.data_dir / f"email_urgency_state_{slug}.json",
            self.paths.file_trash_dir / _trash_key(owner),
        )

    @staticmethod
    def _regular(path: Path, *, label: str) -> bool:
        if path.is_symlink():
            raise AccountAncillaryLifecycleError(f"{label} may not be a symlink")
        if not path.exists():
            return False
        if not (path.is_file() or path.is_dir()):
            raise AccountAncillaryLifecycleError(f"{label} has an unsafe file type")
        return True

    @staticmethod
    def _identity_token(domain: str, *values: Any) -> str:
        material = json.dumps(
            [domain, *[str(value) for value in values]],
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _sql_children(self, owner: str) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
        db = self._session_factory()
        try:
            gallery = [
                {"id": str(row.id), "name": str(row.locator)}
                for row in db.query(FilesImageResource.id, FilesImageResource.locator)
                .filter(
                    FilesImageResource.owner == owner,
                    FilesImageResource.kind == "image",
                    FilesImageResource.is_active.is_(True),
                )
                .order_by(FilesImageResource.id)
                .all()
                if row.locator
            ]
            published = [
                {"id": str(row.id)}
                for row in db.query(PublishedFile.id)
                .filter(PublishedFile.owner == owner)
                .order_by(PublishedFile.id)
                .all()
            ]
            sessions = [
                str(row.id)
                for row in db.query(Session.id)
                .filter(Session.owner == owner)
                .order_by(Session.id)
                .all()
            ]
            return gallery, published, sessions
        finally:
            db.close()

    def _gallery_path(self, filename: str) -> Path:
        if not filename or Path(filename).name != filename:
            raise AccountAncillaryLifecycleError("Gallery row has an unsafe filename")
        path = (self.paths.gallery_root / filename).resolve()
        if path.parent != self.paths.gallery_root:
            raise AccountAncillaryLifecycleError("Gallery filename escaped its root")
        return path

    def _published_path(self, file_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", file_id):
            raise AccountAncillaryLifecycleError("published-file row has an unsafe ID")
        path = (self.paths.published_root / file_id[:2] / file_id).resolve()
        if self.paths.published_root not in path.parents:
            raise AccountAncillaryLifecycleError("published-file ID escaped its root")
        return path

    def _todo_paths(self, session_ids: list[str]) -> list[Path]:
        result: list[Path] = []
        for session_id in session_ids:
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id)[:120] or "current"
            path = (self.paths.agent_todos_dir / f"{safe}.json").resolve()
            if path.parent != self.paths.agent_todos_dir:
                raise AccountAncillaryLifecycleError("agent todo escaped its root")
            if self._regular(path, label="agent todo"):
                result.append(path)
        return result

    def _load_jobs(self) -> dict[str, dict[str, Any]]:
        path = self.paths.background_jobs_file
        if path.is_symlink():
            raise AccountAncillaryLifecycleError("background-jobs authority may not be a symlink")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AccountAncillaryLifecycleError("background-jobs authority is unreadable") from exc
        if not isinstance(payload, dict) or any(not isinstance(row, dict) for row in payload.values()):
            raise AccountAncillaryLifecycleError("background-jobs authority is malformed")
        return {str(key): dict(value) for key, value in payload.items()}

    @contextmanager
    def _job_store_lock(self):
        # Mirror src.bg_jobs._store_lock against the injected authority path.
        # Tests and recovery tools may use a temporary data root, so importing
        # the module-global lock would serialize the wrong file.
        lock_path = self.paths.background_jobs_file.with_suffix(
            self.paths.background_jobs_file.suffix + ".lock"
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(lock_path, "a+b") as handle:
            if os.name == "nt":
                import msvcrt

                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _job_rows(self, owner: str) -> list[tuple[str, dict[str, Any]]]:
        rows = [
            (job_id, row)
            for job_id, row in self._load_jobs().items()
            if str(row.get("owner") or "").strip().lower() == owner
        ]
        for job_id, row in rows:
            if not _SAFE_ID_RE.fullmatch(job_id):
                raise AccountAncillaryLifecycleError("background job has an unsafe ID")
            if str(row.get("status") or "").lower() in {"running", "launching"}:
                raise AccountAncillaryLifecycleError(
                    "finish or cancel active background jobs before changing this account"
                )
        return sorted(rows)

    def _email_tables(self) -> list[str]:
        path = self.paths.scheduled_email_db
        if not path.exists():
            return []
        if path.is_symlink() or not path.is_file():
            raise AccountAncillaryLifecycleError("scheduled-email database is unsafe")
        with sqlite3.connect(str(path)) as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            result: list[str] = []
            for (table,) in rows:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(table)):
                    raise AccountAncillaryLifecycleError("scheduled-email table name is unsafe")
                columns = {
                    str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
                }
                if "owner" in columns:
                    result.append(str(table))
            return sorted(result)

    @staticmethod
    def _email_identities(connection: sqlite3.Connection, table: str, owner: str) -> list[str]:
        info = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        primary = [str(row[1]) for row in sorted(info, key=lambda row: int(row[5]) or 999) if row[5]]
        identity_columns = [column for column in primary if column != "owner"]
        if identity_columns:
            select = ",".join(f'"{column}"' for column in identity_columns)
            rows = connection.execute(
                f'SELECT {select} FROM "{table}" WHERE lower(trim(owner))=?',
                (owner,),
            ).fetchall()
            return sorted(json.dumps([str(value) for value in row], separators=(",", ":")) for row in rows)
        count = int(
            connection.execute(
                f'SELECT count(*) FROM "{table}" WHERE lower(trim(owner))=?',
                (owner,),
            ).fetchone()[0]
        )
        return [str(index) for index in range(count)]

    def _email_items(self, owner: str) -> dict[str, list[str]]:
        path = self.paths.scheduled_email_db
        if not path.exists():
            return {}
        with sqlite3.connect(str(path)) as connection:
            result: dict[str, list[str]] = {}
            for table in self._email_tables():
                identities = self._email_identities(connection, table, owner)
                result[f"email:{table}"] = [
                    self._identity_token(f"email:{table}", identity)
                    for identity in identities
                ]
            active = connection.execute(
                "SELECT count(*) FROM scheduled_emails "
                "WHERE lower(trim(owner))=? AND lower(status) IN ('pending','sending')",
                (owner,),
            ).fetchone()[0] if "scheduled_emails" in self._email_tables() else 0
            if active:
                raise AccountAncillaryLifecycleError(
                    "cancel scheduled or sending emails before changing this account"
                )
            return result

    def _items(self, owner: str) -> dict[str, list[str]]:
        gallery, published, sessions = self._sql_children(owner)
        jobs = self._job_rows(owner)
        items: dict[str, list[str]] = {
            "gallery_bytes": [
                self._identity_token("gallery", row["id"], row["name"])
                for row in gallery
            ],
            "published_bytes": [
                self._identity_token("published", row["id"]) for row in published
            ],
            "agent_todos": [
                self._identity_token("todo", path.name)
                for path in self._todo_paths(sessions)
            ],
            "background_jobs": [
                self._identity_token("job", job_id) for job_id, _row in jobs
            ],
        }
        for label, path in zip(("note_state", "email_state", "file_trash"), self._state_paths(owner)):
            # These filenames/directories are derived from the mutable owner;
            # use an owner-neutral identity so a successful rename preserves
            # the frozen inventory fingerprint.
            items[label] = [self._identity_token(label, "present")] if self._regular(path, label=label) else []
        items.update(self._email_items(owner))
        return items

    def owner_inventory(self, owner: str) -> dict[str, Any]:
        owner = _owner(owner)
        with locked(str(self.paths.journal_path)):
            return _inventory(self._items(owner))

    def preview_owner_rename(self, source_owner: str, target_owner: str) -> dict[str, Any]:
        source = _owner(source_owner)
        target = _owner(target_owner)
        if source == target:
            raise AccountAncillaryLifecycleError("source and target owners must differ")
        with locked(str(self.paths.journal_path)):
            source_inventory = _inventory(self._items(source))
            target_inventory = _inventory(self._items(target))
            if target_inventory["count"]:
                raise AccountAncillaryLifecycleError("ancillary rename target already contains state")
            return {
                "schema_version": 1,
                "source": source_inventory,
                "target": target_inventory,
            }

    @staticmethod
    def _validate_manifest(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
            raise AccountAncillaryLifecycleError("ancillary lifecycle manifest is invalid")
        source = dict(manifest.get("source") or {})
        target = dict(manifest.get("target") or {})
        if not source.get("fingerprint") or int(target.get("count", -1)) != 0:
            raise AccountAncillaryLifecycleError("ancillary lifecycle manifest is malformed")
        return source, target

    def _rename_jobs(self, source: str, target: str) -> None:
        path = self.paths.background_jobs_file
        with self._job_store_lock():
            jobs = self._load_jobs()
            changes: list[AtomicFileChange] = []
            changed = False
            for job_id, row in jobs.items():
                if str(row.get("owner") or "").strip().lower() != source:
                    continue
                row["owner"] = target
                jobs[job_id] = row
                changed = True
                spec = self.paths.background_jobs_dir / f"{job_id}.spec.json"
                if spec.exists():
                    if spec.is_symlink() or not spec.is_file():
                        raise AccountAncillaryLifecycleError("background-job spec is unsafe")
                    try:
                        payload = json.loads(spec.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                        raise AccountAncillaryLifecycleError("background-job spec is unreadable") from exc
                    if not isinstance(payload, dict):
                        raise AccountAncillaryLifecycleError("background-job spec is malformed")
                    payload["owner"] = target
                    changes.append(
                        AtomicFileChange(str(spec), json.dumps(payload, indent=2).encode("utf-8"))
                    )
            if changed:
                changes.append(
                    AtomicFileChange(str(path), json.dumps(jobs, indent=2).encode("utf-8"))
                )
            if changes:
                atomic_write_batch(changes)

    def _purge_jobs(self, owner: str) -> int:
        """Remove only one owner's already-quiesced durable job rows."""
        with self._job_store_lock():
            jobs = self._load_jobs()
            removed = [
                job_id
                for job_id, row in jobs.items()
                if str(row.get("owner") or "").strip().lower() == owner
            ]
            if not removed:
                return 0
            retained = {key: row for key, row in jobs.items() if key not in set(removed)}
            atomic_write_json(
                str(self.paths.background_jobs_file), retained, indent=2
            )
            return len(removed)

    @staticmethod
    def _validate_tree(path: Path) -> None:
        if not path.is_dir():
            return
        for child in path.rglob("*"):
            if child.is_symlink():
                raise AccountAncillaryLifecycleError(
                    "ancillary owner directory contains a symlink"
                )

    def _rename_email(self, source: str, target: str) -> None:
        if not self.paths.scheduled_email_db.exists():
            return
        with sqlite3.connect(str(self.paths.scheduled_email_db)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                for table in self._email_tables():
                    connection.execute(
                        f'UPDATE "{table}" SET owner=? WHERE lower(trim(owner))=?',
                        (target, source),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _rename_state_paths(self, source: str, target: str) -> None:
        for old_path, new_path in zip(self._state_paths(source), self._state_paths(target)):
            old_exists = self._regular(old_path, label="ancillary owner state")
            new_exists = self._regular(new_path, label="ancillary target state")
            if old_exists and new_exists:
                raise AccountAncillaryLifecycleError("ancillary source and target paths both exist")
            if old_exists:
                self._validate_tree(old_path)
                new_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.replace(old_path, new_path)

    def reconcile_owner_rename(
        self,
        source_owner: str,
        target_owner: str,
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        source = _owner(source_owner)
        target = _owner(target_owner)
        expected, _empty = self._validate_manifest(manifest)
        with locked(str(self.paths.journal_path)):
            source_items = self._items(source)
            target_items = self._items(target)
            movable = {
                key for key in expected.get("counts", {})
                if key.startswith("email:")
                or key in {"background_jobs", "note_state", "email_state", "file_trash"}
            }
            empty_fingerprint = _digest([])
            for key in movable:
                expected_count = int(expected.get("counts", {}).get(key, 0))
                expected_fingerprint = str(
                    expected.get("fingerprints", {}).get(key) or ""
                )
                source_values = source_items.get(key, [])
                target_values = target_items.get(key, [])
                source_matches = (
                    len(source_values) == expected_count
                    and _digest(source_values) == expected_fingerprint
                )
                target_matches = (
                    len(target_values) == expected_count
                    and _digest(target_values) == expected_fingerprint
                )
                source_empty = not source_values and _digest(source_values) == empty_fingerprint
                target_empty = not target_values and _digest(target_values) == empty_fingerprint
                if not (
                    (source_matches and target_empty)
                    or (source_empty and target_matches)
                ):
                    raise AccountAncillaryLifecycleError(
                        f"ancillary {key} owner state conflicts with preflight"
                    )
            # SQL-owned byte children keep stable IDs and paths during rename.
            # Their owner changes in the common-SQL transaction, so only the
            # independently owner-keyed domains move here.
            self._rename_jobs(source, target)
            self._rename_email(source, target)
            self._rename_state_paths(source, target)
            current_source = _inventory(self._items(source))
            current_target = _inventory(self._items(target))
            for key in movable:
                if (
                    int(current_source.get("counts", {}).get(key, 0))
                    or int(current_target.get("counts", {}).get(key, -1))
                    != int(expected.get("counts", {}).get(key, 0))
                    or current_target.get("fingerprints", {}).get(key)
                    != expected.get("fingerprints", {}).get(key)
                ):
                    raise AccountAncillaryLifecycleError("ancillary owner rename did not converge")
            return {
                "state": "staged",
                "moved_counts": {
                    key: int(expected.get("counts", {}).get(key, 0)) for key in sorted(movable)
                },
                "source_fingerprint": current_source["fingerprint"],
                "target_fingerprint": current_target["fingerprint"],
            }

    rename_owner = reconcile_owner_rename

    def compensate_owner_rename(
        self,
        source_owner: str,
        target_owner: str,
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._validate_manifest(manifest)
        receipt = self.reconcile_owner_rename(target_owner, source_owner, {
            "schema_version": 1,
            "source": self.owner_inventory(target_owner),
            "target": {"count": 0, "fingerprint": "empty"},
        })
        return {**receipt, "state": "restored"}

    def _journal(self) -> dict[str, Any]:
        if self.paths.journal_path.is_symlink():
            raise AccountAncillaryLifecycleError(
                "ancillary lifecycle journal may not be a symlink"
            )
        try:
            payload = json.loads(self.paths.journal_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "operations": {}}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AccountAncillaryLifecycleError("ancillary lifecycle journal is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("operations"), dict):
            raise AccountAncillaryLifecycleError("ancillary lifecycle journal is malformed")
        return payload

    def _save_journal(self, payload: Mapping[str, Any]) -> None:
        self.paths.journal_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_json(str(self.paths.journal_path), dict(payload), indent=2)

    def _validated_mapping(
        self,
        item: Mapping[str, Any],
        token: str,
    ) -> tuple[Path, Path]:
        source = Path(str(item.get("source") or "")).resolve()
        target = Path(str(item.get("target") or "")).resolve()
        allowed_source = (
            source.parent == self.paths.gallery_root
            or source.parent == self.paths.agent_todos_dir
            or source.parent == self.paths.background_jobs_dir
            or self.paths.published_root in source.parents
        )
        quarantine = (
            self.paths.journal_path.parent / "bytes" / token
        ).resolve()
        if (
            not allowed_source
            or target.parent != quarantine
            or not re.fullmatch(r"[0-9]{8}", target.name)
        ):
            raise AccountAncillaryLifecycleError(
                "ancillary lifecycle journal contains an unsafe path"
            )
        return source, target

    def _asset_paths(self, owner: str) -> list[Path]:
        gallery, published, sessions = self._sql_children(owner)
        paths: list[Path] = []
        for row in gallery:
            path = self._gallery_path(row["name"])
            if self._regular(path, label="Gallery asset"):
                paths.append(path)
        for row in published:
            path = self._published_path(row["id"])
            if self._regular(path, label="published asset"):
                paths.append(path)
        paths.extend(self._todo_paths(sessions))
        for job_id, _row in self._job_rows(owner):
            for suffix in _JOB_SUFFIXES:
                path = (self.paths.background_jobs_dir / f"{job_id}{suffix}").resolve()
                if path.parent != self.paths.background_jobs_dir:
                    raise AccountAncillaryLifecycleError("background-job file escaped its root")
                if self._regular(path, label="background-job file"):
                    paths.append(path)
        return sorted(set(paths))

    def stage_owner_to_tombstone(
        self,
        source_owner: str,
        tombstone_owner: str,
        manifest: Mapping[str, Any],
        *,
        operation_token: str,
    ) -> dict[str, Any]:
        source = _owner(source_owner)
        target = _owner(tombstone_owner)
        expected, _empty = self._validate_manifest(manifest)
        token = str(operation_token or "")
        if not _TOKEN_RE.fullmatch(token):
            raise AccountAncillaryLifecycleError("ancillary lifecycle token is invalid")
        with locked(str(self.paths.journal_path)):
            journal = self._journal()
            operation = journal["operations"].get(token)
            if operation is None:
                actual = _inventory(self._items(source))
                if not _same(actual, expected):
                    raise AccountAncillaryLifecycleError("ancillary owner changed after preflight")
                quarantine = self.paths.journal_path.parent / "bytes" / token
                mappings = []
                for index, path in enumerate(self._asset_paths(source)):
                    mappings.append({"source": str(path), "target": str(quarantine / f"{index:08d}")})
                operation = {
                    "source_owner": source,
                    "target_owner": target,
                    "state": "prepared",
                    "inventory": expected,
                    "files": mappings,
                }
                journal["operations"][token] = operation
                self._save_journal(journal)
            elif operation.get("source_owner") != source or operation.get("target_owner") != target:
                raise AccountAncillaryLifecycleError("ancillary lifecycle token belongs to another owner")

            mappings = [
                self._validated_mapping(item, token)
                for item in operation.get("files", [])
            ]
            for source_path, target_path in mappings:
                if source_path.exists() and target_path.exists():
                    raise AccountAncillaryLifecycleError("ancillary source and quarantine both exist")
                if source_path.exists():
                    if source_path.is_symlink():
                        raise AccountAncillaryLifecycleError("ancillary lifecycle refused a symlink")
                    target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.replace(source_path, target_path)
                elif not target_path.exists() and operation.get("state") == "prepared":
                    raise AccountAncillaryLifecycleError("ancillary staged asset was lost")
            operation["state"] = "bytes_staged"
            self._save_journal(journal)
            self.reconcile_owner_rename(source, target, manifest)
            operation["state"] = "staged"
            self._save_journal(journal)
            return {
                "state": "staged",
                "operation_token": token,
                "asset_count": len(operation.get("files", [])),
                "inventory": expected,
            }

    stage_to_tombstone = stage_owner_to_tombstone

    def compensate(
        self,
        source_owner: str,
        tombstone_owner: str,
        manifest: Mapping[str, Any],
        *,
        operation_token: str,
    ) -> dict[str, Any]:
        source = _owner(source_owner)
        target = _owner(tombstone_owner)
        token = str(operation_token or "")
        with locked(str(self.paths.journal_path)):
            journal = self._journal()
            operation = journal["operations"].get(token)
            mappings = (
                [
                    self._validated_mapping(item, token)
                    for item in operation.get("files", [])
                ]
                if isinstance(operation, dict)
                else []
            )
            self.compensate_owner_rename(source, target, manifest)
            restored = 0
            if isinstance(operation, dict):
                for source_path, target_path in reversed(mappings):
                    if source_path.exists() and target_path.exists():
                        raise AccountAncillaryLifecycleError("ancillary restore paths both exist")
                    if target_path.exists():
                        source_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        os.replace(target_path, source_path)
                        restored += 1
                journal["operations"].pop(token, None)
                self._save_journal(journal)
            return {"state": "restored", "operation_token": token, "restored_assets": restored}

    def purge_owner(
        self,
        owner: str,
        *,
        expected: Mapping[str, Any] | None = None,
        operation_token: str | None = None,
    ) -> dict[str, Any]:
        target = _owner(owner)
        token = str(operation_token or "")
        if not _TOKEN_RE.fullmatch(token):
            raise AccountAncillaryLifecycleError("ancillary lifecycle token is invalid")
        with locked(str(self.paths.journal_path)):
            journal = self._journal()
            operation = journal["operations"].get(token)
            if not isinstance(operation, dict) or operation.get("target_owner") != target:
                raise AccountAncillaryLifecycleError("ancillary staged deletion was not found")
            mappings = [
                self._validated_mapping(item, token)
                for item in operation.get("files", [])
            ]
            removed_jobs = self._purge_jobs(target)
            if self.paths.scheduled_email_db.exists():
                with sqlite3.connect(str(self.paths.scheduled_email_db)) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    for table in self._email_tables():
                        connection.execute(
                            f'DELETE FROM "{table}" WHERE lower(trim(owner))=?',
                            (target,),
                        )
                    connection.commit()
            for path in self._state_paths(target):
                if path.is_symlink():
                    raise AccountAncillaryLifecycleError("ancillary purge refused a symlink")
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    # File-trash contains only recoverable copies; remove
                    # bottom-up without following directory symlinks.
                    for child in sorted(path.rglob("*"), reverse=True):
                        if child.is_symlink():
                            raise AccountAncillaryLifecycleError("file trash contains a symlink")
                        if child.is_dir():
                            child.rmdir()
                        else:
                            child.unlink()
                    path.rmdir()
            deleted = 0
            for _source, staged in mappings:
                if staged.is_symlink():
                    raise AccountAncillaryLifecycleError("ancillary purge refused a symlink")
                if staged.is_file():
                    staged.unlink()
                    deleted += 1
                elif staged.is_dir():
                    raise AccountAncillaryLifecycleError("ancillary staged asset is not a file")
            journal["operations"].pop(token, None)
            self._save_journal(journal)
            return {
                "state": "purged",
                "operation_token": token,
                "deleted_assets": deleted,
                "deleted_jobs": removed_jobs,
                "inventory": dict(expected or operation.get("inventory") or {}),
            }


def build_account_ancillary_lifecycle(
    session_factory: Callable[[], Any],
) -> AccountAncillaryLifecycle:
    """Build the production adapter from the canonical application roots."""
    from src.constants import (
        BG_JOBS_DIR,
        BG_JOBS_FILE,
        DATA_DIR,
        GENERATED_IMAGES_DIR,
        SCHEDULED_EMAILS_DB,
        UPLOAD_DIR,
    )

    data_root = Path(DATA_DIR)
    return AccountAncillaryLifecycle(
        session_factory=session_factory,
        paths=AccountAncillaryPaths(
            data_dir=data_root,
            gallery_root=Path(GENERATED_IMAGES_DIR),
            published_root=Path(UPLOAD_DIR) / ".published",
            background_jobs_file=Path(BG_JOBS_FILE),
            background_jobs_dir=Path(BG_JOBS_DIR),
            scheduled_email_db=Path(SCHEDULED_EMAILS_DB),
            agent_todos_dir=data_root / "agent_todos",
            file_trash_dir=data_root / "file-trash",
            journal_path=data_root / ".account-lifecycle" / "ancillary.json",
        ),
    )


__all__ = [
    "AccountAncillaryLifecycle",
    "AccountAncillaryLifecycleError",
    "AccountAncillaryPaths",
    "build_account_ancillary_lifecycle",
]
