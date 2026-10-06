"""Safe, declarative Open Clank Hexes contract discovery and activation.

Discovery is parse-only. It never imports custom checks, executes commands, or
activates policy; the mutation middleware must perform the separate owner
activation step before enforcement.
"""

from __future__ import annotations

import hashlib
import difflib
import fnmatch
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping, Optional

import yaml
from src.clanker_paths import (
    CLANKER_ARCHIVE_DIR,
    CLANKER_FUTURES_DIR,
    CLANKER_HEXES_DIR,
    CLANKER_ROBONOTES_DIR,
    CLANKER_REFERENCES_DIR,
    CLANKER_TOOLS_DIR,
    GLOBAL_HEX_CONTRACT,
    GLOBAL_HEX_CONTRACT_PATHS,
    is_reference_path,
)


CONTRACT_NAMES = (".hex", "henxels.yaml", ".henxels.yaml", "henxels.yml", ".henxels.yml")
SUPPORTED_FIRST_PARTY_CONTRACTS = frozenset({"open-clank-hexes/v1", "open-clank-hexes/v2"})
MAX_BYTES = 1 * 1024 * 1024
MAX_NODES = 25_000
MAX_DEPTH = 128
MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
MAX_POLICY_FILES = 100_000
ENGINE_VERSION = "open-clank-hexes/1"
ENGINE_VERSION_V2 = "open-clank-hexes/2"


class HexResolutionError(ValueError):
    pass


def _engine_version_for_contract(value: Mapping[str, Any]) -> str:
    return ENGINE_VERSION_V2 if value.get("contract") == "open-clank-hexes/v2" else ENGINE_VERSION


@dataclass(frozen=True)
class HexResolution:
    project_root: str
    contract_path: Optional[str]
    contract_hash: Optional[str]
    contract: Optional[dict[str, Any]]
    state: str
    diagnostics: tuple[str, ...] = ()
    engine_version: str = ENGINE_VERSION


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def ensure_policy_schema(db_path: str) -> None:
    """Initialize or validate the current native policy schema without upgrades."""
    expected = {'fm_v2_projects': ['owner_id', 'project_id', 'workspace_id', 'created_at', 'updated_at'], 'fm_v2_project_locators': ['owner_id', 'project_id', 'locator_revision', 'canonical_root', 'root_identity', 'git_identity', 'worktree_identity', 'active', 'authorized_at'], 'fm_v2_policy_projections': ['owner_id', 'project_id', 'contract_path', 'contract_hash', 'engine_version', 'summary_json', 'source_revision', 'state', 'updated_at', 'workspace_id', 'canonical_root', 'root_identity', 'git_identity', 'worktree_identity', 'activation_revision', 'activated_by', 'activated_at'], 'fm_v2_spells': ['owner_id', 'spell_id', 'project_id', 'title', 'suggestion_json', 'source_evidence_json', 'status', 'created_at', 'updated_at', 'workspace_id', 'path_scope', 'revision', 'review_state', 'lifecycle', 'reviewed_by', 'reviewed_at', 'expires_at', 'rationale', 'confidence', 'promoted_contract_hash', 'promoted_transition_id'], 'fm_v2_policy_transitions': ['owner_id', 'transition_id', 'project_id', 'phase', 'contract_hash', 'actor_id', 'payload_hash', 'created_at', 'contract_path', 'old_contract_hash', 'new_contract_hash', 'old_bytes', 'new_bytes', 'spell_id', 'spell_revision', 'payload_json', 'updated_at', 'operation_id', 'sequence', 'state', 'previous_transition_id', 'previous_event_hash', 'event_hash', 'old_bytes_ref', 'new_bytes_ref', 'error_json'], 'fm_v2_policy_trust': ['owner_id', 'project_id', 'trust_id', 'revision', 'canonical_root', 'worktree_identity', 'branch', 'contract_hash', 'engine_version', 'manifest_hash', 'capability_hash', 'expires_at', 'revoked_at', 'created_by', 'created_reason', 'revoked_by', 'revoked_reason', 'created_at'], 'fm_v2_policy_profiles': ['owner_id', 'project_id', 'profile_id', 'os_uid', 'credential_hash', 'activation_revision', 'allowed_actions_json', 'expires_at', 'revoked_at', 'created_by', 'created_reason', 'revoked_by', 'revoked_reason', 'created_at', 'updated_at']}
    with sqlite3.connect(db_path, timeout=30) as conn:
        present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if present.intersection(expected):
            for table, columns in expected.items():
                found = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if not set(columns).issubset(found):
                    raise HexResolutionError("Hex policy store does not match the current native schema")
            return
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS fm_v2_projects (
                owner_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (owner_id, project_id)
            );
            CREATE TABLE IF NOT EXISTS fm_v2_project_locators (
                owner_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                locator_revision INTEGER NOT NULL,
                canonical_root TEXT NOT NULL,
                root_identity TEXT NOT NULL,
                git_identity TEXT NOT NULL,
                worktree_identity TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                authorized_at TEXT NOT NULL,
                PRIMARY KEY (owner_id, project_id, locator_revision),
                FOREIGN KEY (owner_id, project_id)
                    REFERENCES fm_v2_projects(owner_id, project_id),
                CHECK (locator_revision > 0),
                CHECK (active IN (0,1))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_fm_v2_project_active_root
                ON fm_v2_project_locators(owner_id, canonical_root)
                WHERE active=1;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_fm_v2_project_active_locator
                ON fm_v2_project_locators(owner_id, project_id)
                WHERE active=1;

            CREATE TABLE IF NOT EXISTS fm_v2_policy_projections (
                owner_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                contract_path TEXT NOT NULL,
                contract_hash TEXT NOT NULL,
                engine_version TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                source_revision INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'never_activated',
                updated_at TEXT NOT NULL,
                workspace_id TEXT NOT NULL DEFAULT 'global',
                canonical_root TEXT NOT NULL DEFAULT '',
                root_identity TEXT NOT NULL DEFAULT '',
                git_identity TEXT NOT NULL DEFAULT '',
                worktree_identity TEXT NOT NULL DEFAULT '',
                activation_revision INTEGER NOT NULL DEFAULT 0,
                activated_by TEXT,
                activated_at TEXT,
                PRIMARY KEY (owner_id, project_id),
                CHECK (state IN ('never_activated','active','drifted','deactivated'))
            );
            CREATE TABLE IF NOT EXISTS fm_v2_spells (
                owner_id TEXT NOT NULL,
                spell_id TEXT NOT NULL,
                project_id TEXT,
                title TEXT NOT NULL,
                suggestion_json TEXT NOT NULL,
                source_evidence_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'proposed',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                workspace_id TEXT NOT NULL DEFAULT 'global',
                path_scope TEXT NOT NULL DEFAULT '**/*',
                revision INTEGER NOT NULL DEFAULT 1,
                review_state TEXT NOT NULL DEFAULT 'proposed',
                lifecycle TEXT NOT NULL DEFAULT 'proposed',
                reviewed_by TEXT,
                reviewed_at TEXT,
                expires_at TEXT,
                rationale TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0.5,
                promoted_contract_hash TEXT,
                promoted_transition_id TEXT,
                PRIMARY KEY (owner_id, spell_id),
                CHECK (status IN ('proposed','dismissed','expired','edited','promoted'))
            );
            CREATE TABLE IF NOT EXISTS fm_v2_policy_transitions (
                owner_id TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                contract_hash TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                contract_path TEXT,
                old_contract_hash TEXT,
                new_contract_hash TEXT,
                old_bytes BLOB,
                new_bytes BLOB,
                spell_id TEXT,
                spell_revision INTEGER,
                payload_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT,
                operation_id TEXT,
                sequence INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'pending',
                previous_transition_id TEXT,
                previous_event_hash TEXT,
                event_hash TEXT,
                old_bytes_ref TEXT,
                new_bytes_ref TEXT,
                error_json TEXT,
                PRIMARY KEY (owner_id, transition_id),
                CHECK (phase IN ('prepared','blessing_consumed','file_published','activation_advanced','validated','projection_enqueued','spell_linked','committed','rollback_required','rolled_back'))
            );
CREATE TABLE IF NOT EXISTS fm_v2_policy_trust (
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            trust_id TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 1,
            canonical_root TEXT NOT NULL,
            worktree_identity TEXT NOT NULL,
            branch TEXT NOT NULL,
            contract_hash TEXT NOT NULL,
            engine_version TEXT NOT NULL,
            manifest_hash TEXT NOT NULL,
            capability_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            created_by TEXT NOT NULL DEFAULT '',
            created_reason TEXT NOT NULL DEFAULT '',
            revoked_by TEXT,
            revoked_reason TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (
                owner_id, project_id, canonical_root, worktree_identity, branch,
                contract_hash, engine_version, manifest_hash, capability_hash
            ),
            FOREIGN KEY (owner_id, project_id)
                REFERENCES fm_v2_projects(owner_id, project_id)
        );
CREATE TABLE IF NOT EXISTS fm_v2_policy_profiles (
            owner_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            os_uid INTEGER NOT NULL,
            credential_hash TEXT NOT NULL DEFAULT '',
            activation_revision INTEGER NOT NULL DEFAULT 0,
            allowed_actions_json TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            created_by TEXT NOT NULL DEFAULT '',
            created_reason TEXT NOT NULL DEFAULT '',
            revoked_by TEXT,
            revoked_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, project_id, profile_id),
            FOREIGN KEY (owner_id, project_id)
                REFERENCES fm_v2_projects(owner_id, project_id),
            CHECK (os_uid >= 0)
        );
CREATE UNIQUE INDEX IF NOT EXISTS idx_fm_v2_policy_transition_sequence ON fm_v2_policy_transitions(owner_id,operation_id,sequence) WHERE operation_id IS NOT NULL;
        """)



def _canonical_project_root(root: str | os.PathLike[str]) -> Path:
    path = Path(root).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise HexResolutionError("project root must be an existing directory")
    return path


def _project_identities(root: Path) -> tuple[str, str, str]:
    info = root.stat()
    root_identity = f"{info.st_dev}:{info.st_ino}"
    marker = root / ".git"
    marker_payload = b""
    git_dir = marker
    if marker.is_file():
        marker_payload = _read_contract_bytes(marker) or b""
        line = marker_payload.decode("utf-8", errors="replace").strip()
        if line.lower().startswith("gitdir:"):
            candidate = Path(line.split(":", 1)[1].strip())
            git_dir = (
                candidate if candidate.is_absolute() else marker.parent / candidate
            ).resolve(strict=False)
    elif marker.is_dir():
        git_dir = marker.resolve(strict=False)
    head = git_dir / "HEAD"
    head_payload = (_read_contract_bytes(head) or b"") if head.exists() else b""
    ref_payload = b""
    head_text = head_payload.decode("utf-8", errors="replace").strip()
    if head_text.startswith("ref:"):
        ref_path = git_dir / head_text.split(":", 1)[1].strip()
        if ref_path.exists():
            ref_payload = _read_contract_bytes(ref_path) or b""
    git_identity = hashlib.sha256(
        str(git_dir).encode("utf-8")
        + b"\0"
        + marker_payload
        + b"\0"
        + head_payload
        + b"\0"
        + ref_payload
    ).hexdigest()
    try:
        git_info = git_dir.stat()
        worktree_identity = f"{git_info.st_dev}:{git_info.st_ino}"
    except OSError:
        worktree_identity = root_identity
    return root_identity, git_identity, worktree_identity


def _git_branch(root: Path) -> str:
    marker = root / ".git"
    git_dir = marker
    if marker.is_file():
        payload = _read_contract_bytes(marker) or b""
        line = payload.decode("utf-8", errors="replace").strip()
        if line.lower().startswith("gitdir:"):
            candidate = Path(line.split(":", 1)[1].strip())
            git_dir = (
                candidate if candidate.is_absolute() else marker.parent / candidate
            ).resolve(strict=False)
    head = git_dir / "HEAD"
    payload = (_read_contract_bytes(head) or b"") if head.exists() else b""
    text = payload.decode("utf-8", errors="replace").strip()
    return text[4:].strip() if text.startswith("ref:") else text


def _project_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "owner": row["owner_id"],
        "project_id": row["project_id"],
        "workspace_id": row["workspace_id"],
        "canonical_root": row["canonical_root"],
        "locator_revision": int(row["locator_revision"]),
        "root_identity": row["root_identity"],
        "git_identity": row["git_identity"],
        "worktree_identity": row["worktree_identity"],
        "authorized_at": row["authorized_at"],
    }


def get_project(
    project_id: str,
    *,
    owner: str,
    db_path: str,
) -> Optional[dict[str, Any]]:
    owner = str(owner or "").strip()
    project_id = str(project_id or "").strip()
    if not owner or not project_id:
        return None
    ensure_policy_schema(db_path)
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT p.owner_id,p.project_id,p.workspace_id,l.canonical_root,
                   l.locator_revision,l.root_identity,l.git_identity,
                   l.worktree_identity,l.authorized_at
              FROM fm_v2_projects p
              JOIN fm_v2_project_locators l
                ON l.owner_id=p.owner_id AND l.project_id=p.project_id
             WHERE p.owner_id=? AND p.project_id=? AND l.active=1
            """,
            (owner, project_id),
        ).fetchone()
    return _project_row(row) if row else None


def list_projects(*, owner: str, db_path: str) -> list[dict[str, Any]]:
    owner = str(owner or "").strip()
    if not owner:
        return []
    ensure_policy_schema(db_path)
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT p.owner_id,p.project_id,p.workspace_id,l.canonical_root,
                   l.locator_revision,l.root_identity,l.git_identity,
                   l.worktree_identity,l.authorized_at
              FROM fm_v2_projects p
              JOIN fm_v2_project_locators l
                ON l.owner_id=p.owner_id AND l.project_id=p.project_id
             WHERE p.owner_id=? AND l.active=1
             ORDER BY p.updated_at DESC,p.project_id
            """,
            (owner,),
        ).fetchall()
    return [_project_row(row) for row in rows]


def project_for_root(
    root: str | os.PathLike[str],
    *,
    owner: str,
    db_path: str,
) -> Optional[dict[str, Any]]:
    owner = str(owner or "").strip()
    if not owner:
        return None
    canonical = str(_canonical_project_root(root))
    return next(
        (project for project in list_projects(owner=owner, db_path=db_path)
         if project["canonical_root"] == canonical),
        None,
    )


def register_project(
    root: str | os.PathLike[str],
    *,
    owner: str,
    workspace_id: str,
    db_path: str,
    project_id: Optional[str] = None,
) -> dict[str, Any]:
    owner = str(owner or "").strip()
    workspace_id = str(workspace_id or "").strip()
    if not owner or not workspace_id:
        raise HexResolutionError("owner and workspace_id are required")
    canonical = _canonical_project_root(root)
    ensure_policy_schema(db_path)
    existing = project_for_root(canonical, owner=owner, db_path=db_path)
    if existing:
        if project_id and existing["project_id"] != str(project_id).strip():
            raise HexResolutionError("project root already belongs to another project identity")
        if existing["workspace_id"] != workspace_id:
            raise HexResolutionError("project root is already bound to a different Workspace identity")
        return existing
    project_id = str(project_id or "").strip() or "project_" + uuid.uuid4().hex
    root_identity, git_identity, worktree_identity = _project_identities(canonical)
    now = datetime.now(timezone.utc).isoformat()
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT workspace_id FROM fm_v2_projects WHERE owner_id=? AND project_id=?",
                (owner, project_id),
            ).fetchone()
            if row:
                raise HexResolutionError(
                    "project identity already exists; use the relocation operation"
                )
            conn.execute(
                "INSERT INTO fm_v2_projects(owner_id,project_id,workspace_id,created_at,updated_at) VALUES (?,?,?,?,?)",
                (owner, project_id, workspace_id, now, now),
            )
            conn.execute(
                "INSERT INTO fm_v2_project_locators(owner_id,project_id,locator_revision,canonical_root,root_identity,git_identity,worktree_identity,active,authorized_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (owner, project_id, 1, str(canonical), root_identity, git_identity, worktree_identity, 1, now),
            )
        project = get_project(project_id, owner=owner, db_path=db_path)
        if not project:
            raise HexResolutionError("project identity was not persisted")
        return project
    except sqlite3.IntegrityError as exc:
        raise HexResolutionError("project identity conflicts with an existing locator") from exc


def relocate_project(
    project_id: str,
    root: str | os.PathLike[str],
    *,
    owner: str,
    expected_revision: int,
    db_path: str,
) -> dict[str, Any]:
    current = get_project(project_id, owner=owner, db_path=db_path)
    if not current:
        raise HexResolutionError("project identity not found")
    if int(expected_revision) != current["locator_revision"]:
        raise HexResolutionError("project locator revision conflict")
    canonical = _canonical_project_root(root)
    root_identity, git_identity, worktree_identity = _project_identities(canonical)
    revision = current["locator_revision"] + 1
    now = datetime.now(timezone.utc).isoformat()
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE fm_v2_project_locators SET active=0 WHERE owner_id=? AND project_id=? AND locator_revision=? AND active=1",
                (owner, project_id, expected_revision),
            ).rowcount
            if changed != 1:
                raise HexResolutionError("project locator revision conflict")
            conn.execute(
                "INSERT INTO fm_v2_project_locators(owner_id,project_id,locator_revision,canonical_root,root_identity,git_identity,worktree_identity,active,authorized_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (owner, project_id, revision, str(canonical), root_identity, git_identity, worktree_identity, 1, now),
            )
            conn.execute(
                "UPDATE fm_v2_projects SET updated_at=? WHERE owner_id=? AND project_id=?",
                (now, owner, project_id),
            )
            conn.execute(
                "UPDATE fm_v2_policy_projections SET state='drifted',updated_at=? WHERE owner_id=? AND project_id=? AND state='active'",
                (now, owner, project_id),
            )
        project = get_project(project_id, owner=owner, db_path=db_path)
        if not project:
            raise HexResolutionError("project relocation was not persisted")
        return project
    except sqlite3.IntegrityError as exc:
        raise HexResolutionError("project root already belongs to another project identity") from exc


def require_hex_activation(
    target: str | os.PathLike[str],
    *,
    owner: str,
    project_id: str,
    db_path: str,
    workspace_root: str | os.PathLike[str] | None = None,
) -> HexResolution:
    """Resolve a project contract and fail closed unless its exact hash is active."""
    resolution = resolve_hex(target, workspace_root=workspace_root)
    if resolution.contract_path is None:
        return resolution
    if not hex_activation_current(
        resolution,
        owner=owner,
        project_id=project_id,
        db_path=db_path,
    ):
        raise HexResolutionError(
            "project contract is not activated for this owner/project or has drifted"
        )
    return resolution


def hex_check_argv(
    resolution: HexResolution,
) -> list[str]:
    """Build the first-party Hexes check command for a trusted resolution.

    The exact selected path is passed explicitly so a contained worker never
    rediscovers a different legacy basename.
    """
    if not resolution.contract_path or not Path(resolution.contract_path).is_file():
        raise HexResolutionError("a readable project contract is required")
    repository = Path(__file__).resolve().parent.parent
    return [
        str(Path(sys.executable).resolve(strict=True)),
        "-c",
        "import sys;sys.path.insert(0," + repr(str(repository))
        + ");from src.hex_contract_cli import main;raise SystemExit(main(sys.argv[1:]))",
        "check",
        resolution.project_root,
        "--config",
        resolution.contract_path,
    ]




@contextmanager
def verified_contract_snapshot(resolution: HexResolution) -> Iterator[BinaryIO]:
    """Yield unnamed storage containing the exact trusted contract bytes."""
    if not resolution.contract_path or not resolution.contract_hash:
        raise HexResolutionError("a verified project contract is required")
    payload = _read_contract_bytes(Path(resolution.contract_path))
    if payload is None or hashlib.sha256(payload).hexdigest() != resolution.contract_hash:
        raise HexResolutionError("project contract changed after activation verification")
    with tempfile.TemporaryFile(mode="w+b") as snapshot:
        snapshot.write(payload)
        snapshot.flush()
        snapshot.seek(0)
        yield snapshot


def _hash_executable(path: Path) -> str:
    """Hash one resolved regular executable without following a final link."""
    resolved = path.resolve(strict=True)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_EXECUTABLE_BYTES:
            raise HexResolutionError("policy toolchain executable is not a bounded regular file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _command_toolchain(commands: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Bind every executable-looking command token plus the containing shell."""
    names = {"sh"}
    ignored = {
        "case", "do", "done", "elif", "else", "esac", "export", "fi", "for",
        "function", "if", "in", "then", "while",
    }
    for entry in commands:
        raw_values = entry.get("value")
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        for value in values:
            for token in re.findall(r"[A-Za-z][A-Za-z0-9_.+-]*", str(value or "")):
                if token.lower() not in ignored and shutil.which(token):
                    names.add(token)
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for name in sorted(names):
        found = shutil.which(name)
        if not found:
            raise HexResolutionError(f"policy toolchain executable is unavailable: {name}")
        resolved = str(Path(found).resolve(strict=True))
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(
            {"name": name, "path": resolved, "sha256": _hash_executable(Path(resolved))}
        )
    return result


def _policy_file_list(root: Path) -> list[str]:
    marker = root / ".git"
    if marker.exists():
        try:
            completed = subprocess.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            completed = None
        if completed is not None and completed.returncode == 0:
            paths = [
                value.decode("utf-8", errors="surrogateescape")
                for value in completed.stdout.split(b"\0")
                if value and not is_reference_path(value.decode("utf-8", errors="surrogateescape"))
            ]
            if len(paths) > MAX_POLICY_FILES:
                raise HexResolutionError("project policy file set exceeds the 100,000-file limit")
            return sorted(set(paths))
    skipped = {
        ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
        ".mypy_cache", ".ruff_cache", "dist", "build",
    }
    paths: list[str] = []
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = [name for name in names if name not in skipped
                    and not is_reference_path((Path(directory) / name).relative_to(root))]
        base = Path(directory)
        for name in files:
            paths.append((base / name).relative_to(root).as_posix())
            if len(paths) > MAX_POLICY_FILES:
                raise HexResolutionError("project policy file set exceeds the 100,000-file limit")
    return sorted(set(paths))


def _candidate_relative(root: Path, value: str | os.PathLike[str]) -> str:
    raw = Path(value)
    candidate = raw if raw.is_absolute() else root / raw
    lexical = Path(os.path.abspath(candidate))
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise HexResolutionError("candidate policy path escapes the project") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise HexResolutionError("candidate policy path is invalid")
    return relative.as_posix()


def _hex_worker_runtime() -> tuple[Path, tuple[Path, ...]]:
    interpreter = Path(sys.executable).resolve(strict=True)
    repository = Path(__file__).resolve().parent.parent
    roots = {
        repository,
        Path(sys.prefix).resolve(strict=True),
        Path(sys.base_prefix).resolve(strict=True),
        interpreter.parent.parent,
    }
    return interpreter, tuple(sorted(roots, key=str))


def validate_project_file_candidates(
    resolution: HexResolution,
    *,
    owner: str,
    project_id: str,
    db_path: str,
    candidates: Mapping[str, Optional[bytes]],
    stage: str = "check",
) -> dict[str, Any]:
    """Validate exact candidate bytes in a read-only, no-network shadow tree."""
    if stage not in {"check", "pre-commit", "pre-push"}:
        raise HexResolutionError("unsupported project policy stage")
    if not hex_activation_current(
        resolution, owner=owner, project_id=project_id, db_path=db_path
    ):
        raise HexResolutionError("exact declarative policy activation is required")
    require_executable_trust(
        resolution, owner=owner, project_id=project_id, db_path=db_path
    )
    root = Path(resolution.project_root).resolve(strict=True)
    contract_path = Path(resolution.contract_path or "").resolve(strict=True)
    contract_relative = contract_path.relative_to(root).as_posix()
    normalized: dict[str, Optional[bytes]] = {}
    for raw_path, payload in candidates.items():
        relative = _candidate_relative(root, raw_path)
        if Path(relative).name in CONTRACT_NAMES or any(
            relative == contract or relative.endswith("/" + contract)
            for contract in GLOBAL_HEX_CONTRACT_PATHS
        ):
            raise HexResolutionError(
                "project contract bytes may change only through the policy transition journal"
            )
        normalized[relative] = None if payload is None else bytes(payload)
    source_files = _policy_file_list(root)
    if os.name == "nt":
        from src.openclank.hex_windows import validate_windows_candidates

        def verify_native_authority() -> None:
            fresh = require_hex_activation(
                root, owner=owner, project_id=project_id, db_path=db_path,
                workspace_root=root,
            )
            if fresh.contract_path != resolution.contract_path or fresh.contract_hash != resolution.contract_hash:
                raise HexResolutionError("project policy changed during native candidate validation")
            require_executable_trust(
                fresh, owner=owner, project_id=project_id, db_path=db_path
            )
            if _policy_file_list(root) != source_files:
                raise HexResolutionError("project file set changed during native candidate validation")

        executable_manifest = policy_executable_manifest(resolution)
        native_source_files = sorted(set(source_files) | {contract_relative} | {
            Path(item["path"]).as_posix() for item in executable_manifest["files"]
        })
        try:
            return validate_windows_candidates(
                root, contract_relative=contract_relative, source_files=native_source_files,
                candidates=normalized, stage=stage, verify_authority=verify_native_authority,
            )
        except HexResolutionError:
            raise
        except Exception as exc:
            raise HexResolutionError(f"native project policy validation failed: {exc}") from exc
    source_state: dict[str, tuple[int, int, int, int]] = {}
    with tempfile.TemporaryDirectory(prefix="open-clank-policy-candidate-") as temporary:
        shadow = Path(temporary).resolve()
        files: set[str] = set()
        for relative in source_files:
            if relative in normalized and normalized[relative] is None:
                continue
            source = root / relative
            if source.is_symlink():
                try:
                    target = source.resolve(strict=True)
                    target_relative = target.relative_to(root).as_posix()
                except (OSError, ValueError) as exc:
                    raise HexResolutionError(
                        f"governed project symlink must resolve inside the project: {relative}"
                    ) from exc
                target_is_tracked_file = target.is_file() and target_relative in source_files
                target_has_tracked_descendants = target.is_dir() and any(
                    path.startswith(target_relative.rstrip("/") + "/")
                    for path in source_files
                )
                if not (target_is_tracked_file or target_has_tracked_descendants):
                    raise HexResolutionError(
                        f"governed project symlink target is not a tracked project path: {relative}"
                    )
                info = source.lstat()
                source_state[relative] = (
                    int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns)
                )
                destination = shadow / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shadow_target = shadow / target_relative
                destination.symlink_to(os.path.relpath(shadow_target, destination.parent))
                files.add(relative)
                continue
            if not source.exists():
                continue
            if not source.is_file():
                continue
            info = source.stat(follow_symlinks=False)
            source_state[relative] = (
                int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns)
            )
            destination = shadow / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if relative in normalized:
                destination.write_bytes(normalized[relative] or b"")
            elif relative == contract_relative:
                payload = _read_contract_bytes(source)
                if payload is None or hashlib.sha256(payload).hexdigest() != resolution.contract_hash:
                    raise HexResolutionError("project contract changed before candidate validation")
                destination.write_bytes(payload)
            else:
                try:
                    os.link(source, destination, follow_symlinks=False)
                except OSError:
                    shutil.copyfile(source, destination, follow_symlinks=False)
            files.add(relative)
        for relative, payload in normalized.items():
            if payload is None or relative in files:
                continue
            destination = shadow / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            files.add(relative)
        shadow_contract = shadow / contract_relative
        if not shadow_contract.is_file():
            raise HexResolutionError("selected project contract is absent from candidate validation")

        changes = {
            "added": sorted(
                relative for relative, payload in normalized.items()
                if payload is not None and not (root / relative).exists()
            ),
            "modified": sorted(
                relative for relative, payload in normalized.items()
                if payload is not None and (root / relative).exists()
            ),
            "deleted": sorted(
                relative for relative, payload in normalized.items() if payload is None
            ),
        }
        spec = {
            "candidate_root": str(shadow),
            "source_root": str(root),
            "contract_path": str(shadow_contract),
            "files": sorted(files),
            "changes": changes,
            "stage": stage,
        }
        interpreter, runtime_roots = _hex_worker_runtime()
        worker = Path(__file__).with_name("hex_policy_worker.py").resolve(strict=True)
        repository = worker.parent.parent
        from src.shell_policy import (
            ShellContainmentError,
            contained_parser_argv,
            minimal_shell_env,
        )

        with tempfile.TemporaryFile(mode="w+b") as input_file:
            input_file.write(json.dumps(spec, sort_keys=True).encode("utf-8"))
            input_file.flush()
            input_file.seek(0)
            try:
                argv, _ = contained_parser_argv(
                    [
                        str(interpreter),
                        "-c",
                        "import runpy,sys;sys.path.insert(0," + repr(str(repository))
                        + ");runpy.run_path(" + repr(str(worker)) + ",run_name='__main__')",
                    ],
                    input_descriptor=input_file.fileno(),
                    runtime_roots=(
                        *(str(path) for path in runtime_roots),
                        str(shadow),
                        str(root),
                        str(repository),
                    ),
                )
            except ShellContainmentError as exc:
                raise HexResolutionError("policy parser containment is unavailable") from exc
            completed = subprocess.run(
                argv,
                cwd=str(shadow),
                env=minimal_shell_env(cwd=str(shadow)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
                pass_fds=(input_file.fileno(),),
            )
        if completed.returncode != 0:
            detail = (completed.stdout or completed.stderr or "policy worker failed")[:4096]
            raise HexResolutionError(f"contained project policy validation failed: {detail}")
        try:
            result = json.loads(completed.stdout)
        except (TypeError, ValueError) as exc:
            raise HexResolutionError("contained project policy returned malformed output") from exc
        for relative, expected in source_state.items():
            source = root / relative
            try:
                info = source.lstat() if source.is_symlink() else source.stat(follow_symlinks=False)
            except OSError as exc:
                raise HexResolutionError("project changed during candidate validation") from exc
            observed = (int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns))
            if observed != expected:
                raise HexResolutionError("project changed during candidate validation")
        findings = list(result.get("findings") or [])
        blocks = [finding for finding in findings if finding.get("level") == "block"]
        warnings = [finding for finding in findings if finding.get("level") != "block"]
        return {"allowed": not blocks, "findings": findings, "warnings": warnings}


def policy_executable_manifest(resolution: HexResolution) -> dict[str, Any]:
    """Hash every project-controlled executable policy input without loading it."""
    from src.hex_contract import HexContractError, custom_check_references, load_contract

    if not resolution.contract_path or resolution.contract is None:
        raise HexResolutionError("a parsed project contract is required")
    root = Path(resolution.project_root).resolve()
    contract_path = Path(resolution.contract_path)
    payload = _read_contract_bytes(contract_path)
    if payload is None or hashlib.sha256(payload).hexdigest() != resolution.contract_hash:
        raise HexResolutionError("project contract changed before executable manifest")
    try:
        refs = custom_check_references(load_contract(contract_path), root)
    except (HexContractError, OSError, ValueError) as exc:
        raise HexResolutionError(f"executable policy discovery failed: {exc}") from exc
    uses_hexes = "hexes" in resolution.contract

    files: list[dict[str, str]] = []
    unresolved_modules: list[str] = []
    for ref in sorted(set(refs)):
        if not (ref.endswith(".py") or "/" in ref or "\\" in ref):
            unresolved_modules.append(ref)
            continue
        candidate = (root / ref).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise HexResolutionError("executable policy import escapes the project") from exc
        if candidate.suffix != ".py":
            raise HexResolutionError(f"executable policy import must be a Python file: {ref}")
        payload = _read_contract_bytes(candidate)
        if payload is None:
            raise HexResolutionError(f"executable policy import is missing: {ref}")
        files.append(
            {
                "path": str(candidate.relative_to(root)),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )

    commands: list[dict[str, Any]] = []
    rules = resolution.contract.get("hexes") if uses_hexes else resolution.contract.get("henxels")
    if rules is not None and not isinstance(rules, list):
        raise HexResolutionError("project contract rules must be a list")
    for index, rule in enumerate(rules or []):
        if not isinstance(rule, Mapping):
            continue
        for key in ("run_before_commit", "run_before_push"):
            if key in rule:
                commands.append({"rule": index, "kind": key, "value": rule[key]})
    manifest = {
        "engine_version": resolution.engine_version,
        "contract_hash": resolution.contract_hash,
        "files": files,
        "unresolved_modules": sorted(set(unresolved_modules)),
        "commands": commands,
        "toolchain": _command_toolchain(commands) if commands else [],
    }
    manifest["manifest_hash"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest["executable"] = bool(files or unresolved_modules or commands)
    return manifest


def _trust_binding(
    *,
    owner: str,
    project: Mapping[str, Any],
    resolution: HexResolution,
    manifest: Mapping[str, Any],
    capabilities: list[str],
) -> dict[str, Any]:
    return {
        "owner": owner,
        "project_id": project["project_id"],
        "canonical_root": project["canonical_root"],
        "worktree_identity": project["worktree_identity"],
        "branch": _git_branch(Path(project["canonical_root"])),
        "contract_hash": resolution.contract_hash,
        "engine_version": resolution.engine_version,
        "manifest_hash": manifest["manifest_hash"],
        "capabilities": sorted(set(capabilities)),
    }


def _append_trust_event(
    conn: sqlite3.Connection,
    *,
    owner: str,
    project_id: str,
    action: str,
    binding_hash: str,
    payload: Mapping[str, Any],
) -> str:
    previous = conn.execute(
        "SELECT event_hash FROM fm_v2_policy_trust_events "
        "WHERE owner_id=? AND project_id=? ORDER BY created_at DESC,event_id DESC LIMIT 1",
        (owner, project_id),
    ).fetchone()
    previous_hash = str(previous[0]) if previous else ""
    body = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
    event_hash = hashlib.sha256(
        (previous_hash + "\0" + action + "\0" + binding_hash + "\0" + body).encode(
            "utf-8"
        )
    ).hexdigest()
    event_id = "trust_event_" + uuid.uuid4().hex
    conn.execute(
        "INSERT INTO fm_v2_policy_trust_events("
        "owner_id,event_id,project_id,action,binding_hash,payload_json,"
        "previous_event_hash,event_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            owner,
            event_id,
            project_id,
            action,
            binding_hash,
            body,
            previous_hash or None,
            event_hash,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return event_id


def grant_executable_trust(
    resolution: HexResolution,
    *,
    owner: str,
    project_id: str,
    db_path: str,
    expires_at: str,
    capabilities: Optional[list[str]] = None,
    actor_id: Optional[str] = None,
    reason: str = "explicit executable-policy approval",
) -> dict[str, Any]:
    """Grant exact, expiring executable-policy trust after owner activation."""
    owner = str(owner or "").strip()
    project_id = str(project_id or "").strip()
    if not owner or not project_id:
        raise HexResolutionError("owner and project_id are required")
    try:
        expiry = datetime.fromisoformat(str(expires_at))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HexResolutionError("executable trust expiry must be ISO-8601") from exc
    if expiry <= datetime.now(timezone.utc):
        raise HexResolutionError("executable trust expiry must be in the future")
    if not hex_activation_current(
        resolution, owner=owner, project_id=project_id, db_path=db_path
    ):
        raise HexResolutionError("exact declarative policy activation is required")
    project = get_project(project_id, owner=owner, db_path=db_path)
    if not project:
        raise HexResolutionError("project identity not found")
    manifest = policy_executable_manifest(resolution)
    if manifest["unresolved_modules"]:
        raise HexResolutionError(
            "executable policy imports must resolve to explicit project Python files: "
            + ", ".join(manifest["unresolved_modules"])
        )
    requested = sorted(set(capabilities or ["hexes_check"]))
    binding = _trust_binding(
        owner=owner,
        project=project,
        resolution=resolution,
        manifest=manifest,
        capabilities=requested,
    )
    binding_hash = hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    capability_hash = hashlib.sha256(
        json.dumps(requested, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    trust_id = "trust_" + uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    ensure_policy_schema(db_path)
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO fm_v2_policy_trust("
            "owner_id,project_id,canonical_root,worktree_identity,branch,"
            "contract_hash,engine_version,manifest_hash,capability_hash,"
            "expires_at,revoked_at,created_at,trust_id,created_by,reason,"
            "revoked_by,revocation_reason) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,NULL,NULL) "
            "ON CONFLICT(owner_id,project_id,canonical_root,worktree_identity,branch,"
            "contract_hash,engine_version,manifest_hash,capability_hash) DO UPDATE SET "
            "expires_at=excluded.expires_at,revoked_at=NULL,created_at=excluded.created_at,"
            "trust_id=excluded.trust_id,created_by=excluded.created_by,reason=excluded.reason,"
            "revoked_by=NULL,revocation_reason=NULL",
            (
                owner,
                project_id,
                binding["canonical_root"],
                binding["worktree_identity"],
                binding["branch"],
                binding["contract_hash"],
                binding["engine_version"],
                binding["manifest_hash"],
                capability_hash,
                expiry.isoformat(),
                now,
                trust_id,
                str(actor_id or owner),
                str(reason or "").strip()[:500],
            ),
        )
        event_id = _append_trust_event(
            conn,
            owner=owner,
            project_id=project_id,
            action="grant",
            binding_hash=binding_hash,
            payload={
                "trust_id": trust_id,
                "binding": binding,
                "capability_hash": capability_hash,
                "expires_at": expiry.isoformat(),
                "actor_id": str(actor_id or owner),
                "reason": str(reason or "").strip()[:500],
            },
        )
    return {
        "trust_id": trust_id,
        "event_id": event_id,
        "binding": binding,
        "expires_at": expiry.isoformat(),
    }


def require_executable_trust(
    resolution: HexResolution,
    *,
    owner: str,
    project_id: str,
    db_path: str,
    capabilities: Optional[list[str]] = None,
) -> dict[str, Any]:
    manifest = policy_executable_manifest(resolution)
    if manifest["unresolved_modules"]:
        raise HexResolutionError(
            "executable policy imports must resolve to explicit project Python files: "
            + ", ".join(manifest["unresolved_modules"])
        )
    if not manifest["executable"]:
        return {"required": False, "manifest": manifest}
    if not hex_activation_current(
        resolution, owner=owner, project_id=project_id, db_path=db_path
    ):
        raise HexResolutionError("exact declarative policy activation is required")
    project = get_project(project_id, owner=owner, db_path=db_path)
    if not project:
        raise HexResolutionError("project identity not found")
    requested = sorted(set(capabilities or ["hexes_check"]))
    binding = _trust_binding(
        owner=owner,
        project=project,
        resolution=resolution,
        manifest=manifest,
        capabilities=requested,
    )
    capability_hash = hashlib.sha256(
        json.dumps(requested, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    ensure_policy_schema(db_path)
    with sqlite3.connect(db_path, timeout=30) as conn:
        row = conn.execute(
            "SELECT trust_id,expires_at,created_by,created_reason FROM fm_v2_policy_trust "
            "WHERE owner_id=? AND project_id=? AND canonical_root=? "
            "AND worktree_identity=? AND branch=? AND contract_hash=? "
            "AND engine_version=? AND manifest_hash=? AND capability_hash=? "
            "AND revoked_at IS NULL",
            (
                owner,
                project_id,
                binding["canonical_root"],
                binding["worktree_identity"],
                binding["branch"],
                binding["contract_hash"],
                binding["engine_version"],
                binding["manifest_hash"],
                capability_hash,
            ),
        ).fetchone()
    if not row:
        raise HexResolutionError("executable project policy has not been explicitly trusted")
    try:
        expiry = datetime.fromisoformat(str(row[1]))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HexResolutionError("executable project policy trust is malformed") from exc
    if expiry <= datetime.now(timezone.utc):
        raise HexResolutionError("executable project policy trust has expired")
    return {
        "required": True,
        "trust_id": row[0],
        "expires_at": expiry.isoformat(),
        "created_by": row[2],
        "reason": row[3],
        "binding": binding,
        "manifest": manifest,
    }


def revoke_executable_trust(
    trust_id: str,
    *,
    owner: str,
    project_id: str,
    db_path: str,
    actor_id: Optional[str] = None,
    reason: str = "revoked by owner",
) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT canonical_root,worktree_identity,branch,contract_hash,"
            "engine_version,manifest_hash,capability_hash FROM fm_v2_policy_trust "
            "WHERE owner_id=? AND project_id=? AND trust_id=? AND revoked_at IS NULL",
            (owner, project_id, trust_id),
        ).fetchone()
        if not row:
            return False
        changed = conn.execute(
            "UPDATE fm_v2_policy_trust SET revoked_at=?,revoked_by=?,"
            "revocation_reason=? WHERE owner_id=? AND project_id=? AND trust_id=? "
            "AND revoked_at IS NULL",
            (
                now,
                str(actor_id or owner),
                str(reason or "").strip()[:500],
                owner,
                project_id,
                trust_id,
            ),
        ).rowcount
        if changed != 1:
            return False
        binding_hash = hashlib.sha256(
            json.dumps(list(row), separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        _append_trust_event(
            conn,
            owner=owner,
            project_id=project_id,
            action="revoke",
            binding_hash=binding_hash,
            payload={
                "trust_id": trust_id,
                "actor_id": str(actor_id or owner),
                "reason": str(reason or "").strip()[:500],
            },
        )
    return True


def _spell_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        suggestion = json.loads(row["suggestion_json"] or "{}")
    except json.JSONDecodeError:
        suggestion = {}
    try:
        evidence = json.loads(row["source_evidence_json"] or "[]")
    except json.JSONDecodeError:
        evidence = []
    return {
        "owner": row["owner_id"],
        "spell_id": row["spell_id"],
        "project_id": row["project_id"],
        "workspace_id": row["workspace_id"],
        "title": row["title"],
        "suggestion": suggestion,
        "source_evidence": evidence,
        "path_scope": row["path_scope"],
        "revision": int(row["revision"]),
        "review_state": row["review_state"],
        "lifecycle": row["lifecycle"],
        "status": row["status"],
        "reviewed_by": row["reviewed_by"],
        "reviewed_at": row["reviewed_at"],
        "expires_at": row["expires_at"],
        "rationale": row["rationale"],
        "confidence": float(row["confidence"]),
        "promoted_contract_hash": row["promoted_contract_hash"],
        "promoted_transition_id": row["promoted_transition_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _normalized_suggestion(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = {"sentence": value}
    if not isinstance(value, Mapping):
        raise HexResolutionError("Spell suggestion must be an object or sentence")
    result = dict(value)
    sentence = str(
        result.get("sentence") or result.get("hex") or result.get("henxel") or ""
    ).strip()
    if not sentence:
        raise HexResolutionError("Spell suggestion requires a sentence")
    result.pop("henxel", None)
    result["sentence"] = sentence
    # JSON round-trip rejects non-serializable values before they reach SQLite.
    try:
        json.dumps(result, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise HexResolutionError("Spell suggestion is not valid JSON") from exc
    return result


def _normalized_spell_scope(value: Any) -> str:
    scope = str(value or "**/*").strip().replace("\\", "/")
    parts = tuple(part for part in scope.split("/") if part not in {"", "."})
    if not scope or scope.startswith("/") or ".." in parts:
        raise HexResolutionError("Spell path scope must be project-relative")
    return scope


def list_spells(
    *,
    owner: str,
    project_id: str,
    db_path: str,
) -> list[dict[str, Any]]:
    owner = str(owner or "").strip()
    project_id = str(project_id or "").strip()
    if not owner or not project_id:
        return []
    ensure_policy_schema(db_path)
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM fm_v2_spells WHERE owner_id=? AND project_id=? ORDER BY updated_at DESC,spell_id",
            (owner, project_id),
        ).fetchall()
    return [_spell_row(row) for row in rows]


def create_spell(
    *,
    owner: str,
    project_id: str,
    title: str,
    suggestion: Any,
    db_path: str,
    path_scope: str = "**/*",
    source_evidence: Optional[list[Any]] = None,
    rationale: str = "",
    confidence: float = 0.5,
    expires_at: Optional[str] = None,
) -> dict[str, Any]:
    owner = str(owner or "").strip()
    project_id = str(project_id or "").strip()
    title = str(title or "").strip()
    path_scope = _normalized_spell_scope(path_scope)
    if not owner or not project_id or not title or not path_scope:
        raise HexResolutionError("owner, project, title, and path scope are required")
    project = get_project(project_id, owner=owner, db_path=db_path)
    if not project:
        raise HexResolutionError("project identity not found")
    normalized = _normalized_suggestion(suggestion)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError) as exc:
        raise HexResolutionError("Spell confidence must be a number") from exc
    spell_id = "spell_" + uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            INSERT INTO fm_v2_spells(
                owner_id,spell_id,project_id,title,suggestion_json,
                source_evidence_json,status,created_at,updated_at,workspace_id,
                path_scope,revision,review_state,lifecycle,expires_at,rationale,
                confidence
            ) VALUES (?,?,?,?,?,?, 'proposed', ?,?,?,?,1,'proposed','proposed',?,?,?)
            """,
            (
                owner,
                spell_id,
                project_id,
                title,
                json.dumps(normalized, sort_keys=True),
                json.dumps(source_evidence or [], sort_keys=True),
                now,
                now,
                project["workspace_id"],
                path_scope,
                str(expires_at or "").strip() or None,
                str(rationale or "").strip(),
                confidence,
            ),
        )
    return next(
        spell for spell in list_spells(owner=owner, project_id=project_id, db_path=db_path)
        if spell["spell_id"] == spell_id
    )


def update_spell(
    spell_id: str,
    *,
    owner: str,
    project_id: str,
    expected_revision: int,
    db_path: str,
    title: Optional[str] = None,
    suggestion: Any = None,
    path_scope: Optional[str] = None,
    rationale: Optional[str] = None,
    confidence: Optional[float] = None,
    expires_at: Optional[str] = None,
) -> dict[str, Any]:
    spells = list_spells(owner=owner, project_id=project_id, db_path=db_path)
    current = next((item for item in spells if item["spell_id"] == spell_id), None)
    if not current:
        raise HexResolutionError("Spell not found in this owner/project scope")
    if current["lifecycle"] == "superseded":
        raise HexResolutionError("a promoted Spell is immutable")
    if int(expected_revision) != current["revision"]:
        raise HexResolutionError("Spell revision conflict")
    next_title = str(title if title is not None else current["title"]).strip()
    next_scope = _normalized_spell_scope(
        path_scope if path_scope is not None else current["path_scope"]
    )
    next_suggestion = _normalized_suggestion(
        current["suggestion"] if suggestion is None else suggestion
    )
    next_rationale = str(rationale if rationale is not None else current["rationale"]).strip()
    try:
        next_confidence = max(
            0.0,
            min(1.0, float(current["confidence"] if confidence is None else confidence)),
        )
    except (TypeError, ValueError) as exc:
        raise HexResolutionError("Spell confidence must be a number") from exc
    if not next_title or not next_scope:
        raise HexResolutionError("Spell title and path scope are required")
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path, timeout=30) as conn:
        changed = conn.execute(
            """
            UPDATE fm_v2_spells
               SET title=?,suggestion_json=?,path_scope=?,rationale=?,confidence=?,
                   expires_at=?,revision=revision+1,status='edited',
                   review_state='proposed',lifecycle='proposed',reviewed_by=NULL,
                   reviewed_at=NULL,updated_at=?
             WHERE owner_id=? AND spell_id=? AND project_id=? AND revision=?
            """,
            (
                next_title,
                json.dumps(next_suggestion, sort_keys=True),
                next_scope,
                next_rationale,
                next_confidence,
                expires_at if expires_at is not None else current["expires_at"],
                now,
                owner,
                spell_id,
                project_id,
                expected_revision,
            ),
        ).rowcount
        if changed != 1:
            raise HexResolutionError("Spell revision conflict")
    return next(
        spell for spell in list_spells(owner=owner, project_id=project_id, db_path=db_path)
        if spell["spell_id"] == spell_id
    )


def review_spell(
    spell_id: str,
    *,
    owner: str,
    project_id: str,
    expected_revision: int,
    accept: bool,
    db_path: str,
    actor_id: Optional[str] = None,
    reason: str = "",
) -> dict[str, Any]:
    ensure_policy_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    status = "edited" if accept else "dismissed"
    review_state = "accepted" if accept else "rejected"
    lifecycle = "active" if accept else "retracted"
    with sqlite3.connect(db_path, timeout=30) as conn:
        changed = conn.execute(
            """
            UPDATE fm_v2_spells
               SET status=?,review_state=?,lifecycle=?,reviewed_by=?,reviewed_at=?,
                   rationale=CASE WHEN ?='' THEN rationale ELSE ? END,
                   revision=revision+1,updated_at=?
             WHERE owner_id=? AND spell_id=? AND project_id=? AND revision=?
               AND lifecycle IN ('proposed','active')
            """,
            (
                status,
                review_state,
                lifecycle,
                str(actor_id or owner),
                now,
                str(reason or "").strip(),
                str(reason or "").strip(),
                now,
                owner,
                spell_id,
                project_id,
                expected_revision,
            ),
        ).rowcount
        if changed != 1:
            raise HexResolutionError("Spell review revision conflict")
    return next(
        spell for spell in list_spells(owner=owner, project_id=project_id, db_path=db_path)
        if spell["spell_id"] == spell_id
    )


def expire_spell(
    spell_id: str,
    *,
    owner: str,
    project_id: str,
    expected_revision: int,
    db_path: str,
) -> dict[str, Any]:
    ensure_policy_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path, timeout=30) as conn:
        changed = conn.execute(
            """
            UPDATE fm_v2_spells
               SET status='expired',lifecycle='retracted',revision=revision+1,
                   updated_at=?
             WHERE owner_id=? AND spell_id=? AND project_id=? AND revision=?
               AND lifecycle!='superseded'
            """,
            (now, owner, spell_id, project_id, expected_revision),
        ).rowcount
        if changed != 1:
            raise HexResolutionError("Spell expiry revision conflict")
    return next(
        spell for spell in list_spells(owner=owner, project_id=project_id, db_path=db_path)
        if spell["spell_id"] == spell_id
    )


def active_spells(
    *,
    owner: str,
    project_id: str,
    db_path: str,
    relative_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return exact-project advisory Spells that are active and unexpired."""
    path = None
    if relative_path is not None:
        path = str(relative_path).strip().replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        if not path or path.startswith("/") or ".." in path.split("/"):
            raise HexResolutionError("Spell lookup path must be project-relative")
    now = datetime.now(timezone.utc)
    result: list[dict[str, Any]] = []
    for spell in list_spells(owner=owner, project_id=project_id, db_path=db_path):
        if spell["review_state"] != "accepted" or spell["lifecycle"] != "active":
            continue
        expires_at = spell.get("expires_at")
        if expires_at:
            try:
                expiry = datetime.fromisoformat(str(expires_at))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if expiry <= now:
                continue
        scope = _normalized_spell_scope(spell.get("path_scope"))
        if path is not None and scope not in {"*", "**", "**/*"}:
            if not fnmatch.fnmatchcase(path, scope):
                continue
        result.append(spell)
    return result


def _spell_rule(spell: Mapping[str, Any]) -> dict[str, Any]:
    suggestion = dict(spell.get("suggestion") or {})
    sentence = str(suggestion.pop("sentence", "")).strip()
    rule: dict[str, Any] = {"hex": sentence}
    if spell.get("path_scope") and "in" not in suggestion:
        rule["in"] = spell["path_scope"]
    rule.update(suggestion)
    return rule


def _append_rule_bytes(raw: bytes, contract: Mapping[str, Any], rule: Mapping[str, Any]) -> bytes:
    rules_key = "hexes" if "hexes" in contract else "henxels"
    current_rules = contract.get(rules_key)
    if current_rules is not None and not isinstance(current_rules, list):
        raise HexResolutionError("project contract rules must be a list")
    normalized_rule = dict(rule)
    if rules_key == "henxels" and "hex" in normalized_rule:
        normalized_rule["henxel"] = normalized_rule.pop("hex")
    text = raw.decode("utf-8")
    top_level = [
        line.split(":", 1)[0]
        for line in text.splitlines()
        if line and not line[0].isspace() and ":" in line and not line.lstrip().startswith("#")
    ]
    dumped = yaml.safe_dump(
        [normalized_rule],
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).rstrip()
    if current_rules is None:
        suffix = f"\n{rules_key}:\n" + "\n".join("  " + line for line in dumped.splitlines()) + "\n"
        candidate = text.rstrip() + suffix
    elif top_level and top_level[-1] == rules_key:
        candidate = text.rstrip() + "\n" + "\n".join(
            "  " + line for line in dumped.splitlines()
        ) + "\n"
    else:
        rewritten = dict(contract)
        rewritten[rules_key] = [*(current_rules or []), normalized_rule]
        candidate = yaml.safe_dump(
            rewritten,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
    payload = candidate.encode("utf-8")
    parsed = _safe_yaml(payload)
    rules = parsed.get(rules_key)
    if not isinstance(rules, list) or rules[-1] != normalized_rule:
        raise HexResolutionError("Spell promotion did not produce the reviewed rule")
    return payload


def preview_spell_promotion(
    spell_id: str,
    *,
    owner: str,
    project_id: str,
    expected_revision: int,
    db_path: str,
) -> dict[str, Any]:
    project = get_project(project_id, owner=owner, db_path=db_path)
    if not project:
        raise HexResolutionError("project identity not found")
    resolution = require_hex_activation(
        project["canonical_root"],
        owner=owner,
        project_id=project_id,
        db_path=db_path,
        workspace_root=project["canonical_root"],
    )
    spell = next(
        (item for item in list_spells(owner=owner, project_id=project_id, db_path=db_path)
         if item["spell_id"] == spell_id),
        None,
    )
    if not spell or spell["review_state"] != "accepted" or spell["lifecycle"] != "active":
        raise HexResolutionError("Spell promotion requires an active reviewed Spell")
    if spell["revision"] != int(expected_revision):
        raise HexResolutionError("Spell revision conflict")
    raw = _read_contract_bytes(Path(resolution.contract_path or ""))
    if raw is None:
        raise HexResolutionError("project contract disappeared")
    candidate = _append_rule_bytes(raw, resolution.contract or {}, _spell_rule(spell))
    before = raw.decode("utf-8").splitlines(keepends=True)
    after = candidate.decode("utf-8").splitlines(keepends=True)
    return {
        "project_id": project_id,
        "spell_id": spell_id,
        "spell_revision": spell["revision"],
        "contract_path": resolution.contract_path,
        "contract_hash": resolution.contract_hash,
        "candidate_hash": hashlib.sha256(candidate).hexdigest(),
        "candidate_text": candidate.decode("utf-8"),
        "diff": "".join(
            difflib.unified_diff(before, after, fromfile="current/.hex", tofile="candidate/.hex")
        ),
    }


def _read_contract_at(directory_fd: int, name: str) -> tuple[bytes, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise HexResolutionError("project contract must be a bounded regular file")
        payload = bytearray()
        while len(payload) <= MAX_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, MAX_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > MAX_BYTES:
            raise HexResolutionError("project contract exceeds the 1 MiB limit")
        return bytes(payload), stat.S_IMODE(info.st_mode)
    finally:
        os.close(descriptor)


def _atomic_replace_contract(path: Path, *, expected_hash: str, payload: bytes) -> str:
    if len(payload) > MAX_BYTES:
        raise HexResolutionError("project contract exceeds the 1 MiB limit")
    _safe_yaml(payload)
    parent = path.parent
    directory_fd = os.open(
        parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    temporary = f".open-clank-policy-{uuid.uuid4().hex}.tmp"
    try:
        current, mode = _read_contract_at(directory_fd, path.name)
        if hashlib.sha256(current).hexdigest() != expected_hash:
            raise HexResolutionError("project contract changed before publication")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        output = os.open(temporary, flags, mode=0o600, dir_fd=directory_fd)
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(output, payload[offset:])
            os.fchmod(output, mode)
            os.fsync(output)
        finally:
            os.close(output)
        current, _ = _read_contract_at(directory_fd, path.name)
        if hashlib.sha256(current).hexdigest() != expected_hash:
            raise HexResolutionError("project contract changed before publication")
        os.replace(
            temporary,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
        observed, _ = _read_contract_at(directory_fd, path.name)
        observed_hash = hashlib.sha256(observed).hexdigest()
        if observed_hash != hashlib.sha256(payload).hexdigest():
            raise HexResolutionError("published project contract hash mismatch")
        return observed_hash
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _atomic_rename_contract(source: Path, target: Path, *, expected_hash: str) -> str:
    """Rename one recognized contract within its directory without a duplicate window."""
    source = source.resolve(strict=False)
    target = target.resolve(strict=False)
    if source.parent != target.parent:
        raise HexResolutionError("project contract rename must stay in one directory")
    if source.name not in CONTRACT_NAMES or target.name not in CONTRACT_NAMES:
        raise HexResolutionError("project contract rename uses an unsupported filename")
    if source == target:
        raise HexResolutionError("project contract rename has no changes")
    directory_fd = os.open(
        source.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        current, _ = _read_contract_at(directory_fd, source.name)
        if hashlib.sha256(current).hexdigest() != expected_hash:
            raise HexResolutionError("project contract changed before rename")
        try:
            os.stat(target.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise HexResolutionError("project contract rename target already exists")
        os.rename(
            source.name,
            target.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
        try:
            os.stat(source.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise HexResolutionError("project contract rename left a duplicate source")
        observed, _ = _read_contract_at(directory_fd, target.name)
        observed_hash = hashlib.sha256(observed).hexdigest()
        if observed_hash != expected_hash:
            raise HexResolutionError("renamed project contract hash mismatch")
        return observed_hash
    finally:
        os.close(directory_fd)


_TERMINAL_TRANSITION_PHASES = {"committed", "rolled_back"}


def _transition_state(phase: str) -> str:
    if phase in _TERMINAL_TRANSITION_PHASES:
        return "complete"
    if phase == "rollback_required":
        return "blocked"
    return "pending"


def _latest_transition_row(
    conn: sqlite3.Connection,
    *,
    owner: str,
    operation_id: str,
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM fm_v2_policy_transitions "
        "WHERE owner_id=? AND (operation_id=? OR "
        "(operation_id IS NULL AND transition_id=?)) "
        "ORDER BY sequence DESC,created_at DESC,transition_id DESC LIMIT 1",
        (owner, operation_id, operation_id),
    ).fetchone()


def _append_transition_event(
    conn: sqlite3.Connection,
    *,
    owner: str,
    operation_id: str,
    project_id: str,
    phase: str,
    values: Mapping[str, Any],
) -> str:
    """Append one hash-chained policy state; never rewrite prior evidence."""
    conn.row_factory = sqlite3.Row
    previous = _latest_transition_row(
        conn, owner=owner, operation_id=operation_id
    )
    sequence = int(previous["sequence"] or 0) + 1 if previous else 0
    previous_transition_id = str(previous["transition_id"]) if previous else None
    previous_event_hash = None
    if previous:
        previous_event_hash = str(previous["event_hash"] or "") or hashlib.sha256(
            "\0".join(
                (
                    "legacy-policy-transition",
                    str(previous["transition_id"]),
                    str(previous["phase"]),
                    str(previous["payload_hash"]),
                )
            ).encode("utf-8")
        ).hexdigest()
    event_id = operation_id if sequence == 0 else "transition_event_" + uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()

    def carried(name: str, default: Any = None) -> Any:
        if name in values:
            return values[name]
        if previous is not None and name in previous.keys():
            return previous[name]
        return default

    old_bytes = carried("old_bytes")
    new_bytes = carried("new_bytes")
    event_payload = {
        "event_id": event_id,
        "operation_id": operation_id,
        "owner": owner,
        "project_id": project_id,
        "sequence": sequence,
        "phase": phase,
        "contract_hash": str(carried("contract_hash", "")),
        "payload_hash": str(carried("payload_hash", "")),
        "old_contract_hash": str(carried("old_contract_hash", "") or ""),
        "new_contract_hash": str(carried("new_contract_hash", "") or ""),
        "old_bytes_hash": hashlib.sha256(bytes(old_bytes)).hexdigest()
        if old_bytes is not None
        else None,
        "new_bytes_hash": hashlib.sha256(bytes(new_bytes)).hexdigest()
        if new_bytes is not None
        else None,
        "previous_event_hash": previous_event_hash,
    }
    event_hash = hashlib.sha256(
        json.dumps(event_payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    conn.execute(
        """
        INSERT INTO fm_v2_policy_transitions(
            owner_id,transition_id,project_id,phase,contract_hash,actor_id,
            payload_hash,created_at,contract_path,old_contract_hash,
            new_contract_hash,old_bytes,new_bytes,spell_id,spell_revision,
            payload_json,updated_at,operation_id,sequence,state,
            previous_transition_id,previous_event_hash,event_hash,
            old_bytes_ref,new_bytes_ref,error_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            owner,
            event_id,
            project_id,
            phase,
            carried("contract_hash", ""),
            carried("actor_id", owner),
            carried("payload_hash", ""),
            now,
            carried("contract_path"),
            carried("old_contract_hash"),
            carried("new_contract_hash"),
            old_bytes,
            new_bytes,
            carried("spell_id"),
            carried("spell_revision"),
            carried("payload_json", "{}"),
            now,
            operation_id,
            sequence,
            _transition_state(phase),
            previous_transition_id,
            previous_event_hash,
            event_hash,
            carried("old_bytes_ref"),
            carried("new_bytes_ref"),
            carried("error_json"),
        ),
    )
    return event_id


def _set_transition_phase(
    db_path: str,
    *,
    owner: str,
    transition_id: str,
    phase: str,
) -> None:
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.row_factory = sqlite3.Row
        previous = _latest_transition_row(
            conn, owner=owner, operation_id=transition_id
        )
        if previous is None:
            raise HexResolutionError("policy transition disappeared")
        _append_transition_event(
            conn,
            owner=owner,
            operation_id=transition_id,
            project_id=str(previous["project_id"]),
            phase=phase,
            values=dict(previous),
        )


def _pending_transition_rows(
    conn: sqlite3.Connection,
    *,
    owner: str,
    project_id: str,
) -> list[sqlite3.Row]:
    """Return only the newest event for each unfinished logical operation."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        WITH ranked AS (
            SELECT t.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(t.operation_id,t.transition_id)
                       ORDER BY t.sequence DESC,t.created_at DESC,t.transition_id DESC
                   ) AS event_rank
              FROM fm_v2_policy_transitions AS t
             WHERE t.owner_id=? AND t.project_id=?
        )
        SELECT * FROM ranked
         WHERE event_rank=1 AND phase NOT IN ('committed','rolled_back')
         ORDER BY created_at,transition_id
        """,
        (owner, project_id),
    ).fetchall()


def recover_policy_transitions(
    *,
    owner: str,
    project_id: str,
    db_path: str,
) -> list[str]:
    """Conservatively roll back every interrupted filesystem transition."""
    ensure_policy_schema(db_path)
    recovered: list[str] = []
    with sqlite3.connect(db_path, timeout=30) as conn:
        rows = _pending_transition_rows(
            conn, owner=owner, project_id=project_id
        )
    for row in rows:
        operation_id = str(row["operation_id"] or row["transition_id"])
        try:
            transition_payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, ValueError):
            transition_payload = {}
        is_rename = transition_payload.get("action") == "rename_contract"
        path = Path(str(row["contract_path"] or ""))
        old_bytes = row["old_bytes"]
        old_hash = str(row["old_contract_hash"] or "")
        new_hash = str(row["new_contract_hash"] or row["contract_hash"] or "")
        if not path.is_absolute() or old_bytes is None or not old_hash:
            _set_transition_phase(
                db_path,
                owner=owner,
                transition_id=operation_id,
                phase="rollback_required",
            )
            raise HexResolutionError("policy transition needs manual recovery")
        restored_path = path
        if is_rename:
            old_path = Path(str(transition_payload.get("old_path") or ""))
            new_path = Path(str(transition_payload.get("new_path") or ""))
            project = get_project(project_id, owner=owner, db_path=db_path)
            try:
                root = Path(str(project["canonical_root"])).resolve(strict=True) if project else None
                if (
                    root is None
                    or not old_path.is_absolute()
                    or not new_path.is_absolute()
                    or old_path.parent.resolve(strict=True) != new_path.parent.resolve(strict=True)
                    or old_path.name not in CONTRACT_NAMES
                    or new_path.name not in CONTRACT_NAMES
                ):
                    raise ValueError
                old_path.parent.resolve(strict=True).relative_to(root)
            except (KeyError, OSError, ValueError):
                _set_transition_phase(
                    db_path,
                    owner=owner,
                    transition_id=operation_id,
                    phase="rollback_required",
                )
                raise HexResolutionError("policy rename transition needs manual recovery")
            old_live = _read_contract_bytes(old_path)
            new_live = _read_contract_bytes(new_path)
            old_live_hash = hashlib.sha256(old_live).hexdigest() if old_live is not None else ""
            new_live_hash = hashlib.sha256(new_live).hexdigest() if new_live is not None else ""
            if new_live_hash == new_hash and old_live is None:
                _atomic_rename_contract(new_path, old_path, expected_hash=new_hash)
            elif old_live_hash != old_hash or new_live is not None:
                _set_transition_phase(
                    db_path,
                    owner=owner,
                    transition_id=operation_id,
                    phase="rollback_required",
                )
                raise HexResolutionError("policy transition found unrecognized live bytes")
            restored_path = old_path
        else:
            current = _read_contract_bytes(path)
            current_hash = hashlib.sha256(current).hexdigest() if current is not None else ""
            if current_hash == new_hash:
                _atomic_replace_contract(path, expected_hash=new_hash, payload=bytes(old_bytes))
            elif current_hash != old_hash:
                _set_transition_phase(
                    db_path,
                    owner=owner,
                    transition_id=operation_id,
                    phase="rollback_required",
                )
                raise HexResolutionError("policy transition found unrecognized live bytes")
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.row_factory = sqlite3.Row
            conn.execute(
                "UPDATE fm_v2_policy_projections SET contract_path=?,contract_hash=?,state='active',activation_revision=activation_revision+1,updated_at=? WHERE owner_id=? AND project_id=?",
                (str(restored_path), old_hash, now, owner, project_id),
            )
            _append_transition_event(
                conn,
                owner=owner,
                operation_id=operation_id,
                project_id=project_id,
                phase="rolled_back",
                values=dict(row),
            )
        recovered.append(operation_id)
    return recovered


def publish_contract_update(
    *,
    owner: str,
    project_id: str,
    expected_contract_hash: str,
    candidate_text: str,
    db_path: str,
    actor_id: Optional[str] = None,
    spell_id: Optional[str] = None,
    spell_revision: Optional[int] = None,
    general_hex_source: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    owner = str(owner or "").strip()
    project_id = str(project_id or "").strip()
    if not owner or not project_id or not expected_contract_hash:
        raise HexResolutionError("owner, project, and expected contract hash are required")
    ensure_policy_schema(db_path)
    recover_policy_transitions(owner=owner, project_id=project_id, db_path=db_path)
    project = get_project(project_id, owner=owner, db_path=db_path)
    if not project:
        raise HexResolutionError("project identity not found")
    resolution = require_hex_activation(
        project["canonical_root"],
        owner=owner,
        project_id=project_id,
        db_path=db_path,
        workspace_root=project["canonical_root"],
    )
    if resolution.contract_hash != expected_contract_hash:
        raise HexResolutionError("project contract hash conflict")
    candidate = str(candidate_text).encode("utf-8")
    _safe_yaml(candidate)
    new_hash = hashlib.sha256(candidate).hexdigest()
    if new_hash == expected_contract_hash:
        raise HexResolutionError("project contract update has no changes")
    old_bytes = _read_contract_bytes(Path(resolution.contract_path or ""))
    if old_bytes is None:
        raise HexResolutionError("project contract disappeared")
    now = datetime.now(timezone.utc).isoformat()
    transition_id = "transition_" + uuid.uuid4().hex
    payload_hash = hashlib.sha256(
        json.dumps(
            {
                "owner": owner,
                "project_id": project_id,
                "old_hash": expected_contract_hash,
                "new_hash": new_hash,
                "spell_id": spell_id,
                "spell_revision": spell_revision,
                "general_hex_source": general_hex_source,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("BEGIN IMMEDIATE")
        projection = conn.execute(
            "SELECT contract_hash,state FROM fm_v2_policy_projections WHERE owner_id=? AND project_id=?",
            (owner, project_id),
        ).fetchone()
        if not projection or projection[0] != expected_contract_hash or projection[1] != "active":
            raise HexResolutionError("project activation changed before publication")
        if general_hex_source:
            general_head = conn.execute(
                "SELECT revision,deleted FROM fm_general_hex_heads WHERE owner_id=? AND hex_id=?",
                (owner, str(general_hex_source.get("hex_id") or "")),
            ).fetchone()
            if not general_head or general_head[1] or general_head[0] != general_hex_source.get("revision"):
                raise HexResolutionError("General Hex changed before publication")
        if spell_id:
            spell = conn.execute(
                "SELECT revision,review_state,lifecycle FROM fm_v2_spells WHERE owner_id=? AND spell_id=? AND project_id=?",
                (owner, spell_id, project_id),
            ).fetchone()
            if not spell or int(spell[0]) != int(spell_revision or 0) or spell[1:] != ("accepted", "active"):
                raise HexResolutionError("Spell changed before publication")
        _append_transition_event(
            conn,
            owner=owner,
            operation_id=transition_id,
            project_id=project_id,
            phase="prepared",
            values={
                "contract_hash": new_hash,
                "actor_id": str(actor_id or owner),
                "payload_hash": payload_hash,
                "contract_path": resolution.contract_path,
                "old_contract_hash": expected_contract_hash,
                "new_contract_hash": new_hash,
                "old_bytes": old_bytes,
                "new_bytes": candidate,
                "spell_id": spell_id,
                "spell_revision": spell_revision,
                "payload_json": json.dumps(
                    {"project": project, "engine_version": ENGINE_VERSION, "general_hex_source": general_hex_source},
                    sort_keys=True,
                ),
            },
        )
    try:
        observed_hash = _atomic_replace_contract(
            Path(resolution.contract_path or ""),
            expected_hash=expected_contract_hash,
            payload=candidate,
        )
        _set_transition_phase(db_path, owner=owner, transition_id=transition_id, phase="file_published")
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.row_factory = sqlite3.Row
            changed = conn.execute(
                """
                UPDATE fm_v2_policy_projections
                   SET contract_hash=?,source_revision=source_revision+1,
                       activation_revision=activation_revision+1,state='active',
                       activated_by=?,activated_at=?,updated_at=?
                 WHERE owner_id=? AND project_id=? AND contract_hash=? AND state='active'
                """,
                (new_hash, str(actor_id or owner), now, now, owner, project_id, expected_contract_hash),
            ).rowcount
            if changed != 1:
                raise HexResolutionError("project activation changed during publication")
            previous = _latest_transition_row(
                conn, owner=owner, operation_id=transition_id
            )
            if previous is None:
                raise HexResolutionError("policy transition disappeared")
            _append_transition_event(
                conn,
                owner=owner,
                operation_id=transition_id,
                project_id=project_id,
                phase="activation_advanced",
                values=dict(previous),
            )
        current = resolve_hex(project["canonical_root"], workspace_root=project["canonical_root"])
        if current.contract_hash != observed_hash or current.contract_hash != new_hash:
            raise HexResolutionError("published policy did not validate")
        _set_transition_phase(db_path, owner=owner, transition_id=transition_id, phase="validated")
        _set_transition_phase(db_path, owner=owner, transition_id=transition_id, phase="projection_enqueued")
        if spell_id:
            with sqlite3.connect(db_path, timeout=30) as conn:
                changed = conn.execute(
                    """
                    UPDATE fm_v2_spells
                       SET status='promoted',lifecycle='superseded',revision=revision+1,
                           promoted_contract_hash=?,promoted_transition_id=?,updated_at=?
                     WHERE owner_id=? AND spell_id=? AND project_id=? AND revision=?
                       AND review_state='accepted' AND lifecycle='active'
                    """,
                    (new_hash, transition_id, now, owner, spell_id, project_id, spell_revision),
                ).rowcount
                if changed != 1:
                    raise HexResolutionError("Spell changed while linking promotion")
            _set_transition_phase(db_path, owner=owner, transition_id=transition_id, phase="spell_linked")
        _set_transition_phase(db_path, owner=owner, transition_id=transition_id, phase="committed")
        return {
            "owner": owner,
            "project_id": project_id,
            "transition_id": transition_id,
            "contract_hash": new_hash,
            "spell_id": spell_id,
        }
    except BaseException:
        try:
            recover_policy_transitions(owner=owner, project_id=project_id, db_path=db_path)
        except BaseException:
            pass
        raise


def transition_contract_filename(
    *,
    owner: str,
    project_id: str,
    expected_contract_hash: str,
    target_name: str,
    db_path: str,
    actor_id: Optional[str] = None,
) -> dict[str, Any]:
    """Journal and atomically rename one active legacy/.hex contract.

    The source and destination never coexist, so duplicate discovery remains a
    hard error instead of becoming a migration technique.
    """
    owner = str(owner or "").strip()
    project_id = str(project_id or "").strip()
    target_name = str(target_name or "").strip()
    if not owner or not project_id or not expected_contract_hash:
        raise HexResolutionError("owner, project, and expected contract hash are required")
    if Path(target_name).name != target_name or target_name not in CONTRACT_NAMES:
        raise HexResolutionError("unsupported project contract filename")
    ensure_policy_schema(db_path)
    recover_policy_transitions(owner=owner, project_id=project_id, db_path=db_path)
    project = get_project(project_id, owner=owner, db_path=db_path)
    if not project:
        raise HexResolutionError("project identity not found")
    resolution = require_hex_activation(
        project["canonical_root"],
        owner=owner,
        project_id=project_id,
        db_path=db_path,
        workspace_root=project["canonical_root"],
    )
    if resolution.contract_hash != expected_contract_hash:
        raise HexResolutionError("project contract hash conflict")
    source = Path(resolution.contract_path or "")
    target = source.with_name(target_name)
    if source == target:
        raise HexResolutionError("project contract rename has no changes")
    if os.path.lexists(target):
        raise HexResolutionError("project contract rename target already exists")
    old_bytes = _read_contract_bytes(source)
    if old_bytes is None or hashlib.sha256(old_bytes).hexdigest() != expected_contract_hash:
        raise HexResolutionError("project contract changed before rename")
    _safe_yaml(old_bytes)
    now = datetime.now(timezone.utc).isoformat()
    transition_id = "transition_" + uuid.uuid4().hex
    payload = {
        "action": "rename_contract",
        "owner": owner,
        "project_id": project_id,
        "old_path": str(source),
        "new_path": str(target),
        "contract_hash": expected_contract_hash,
        "engine_version": ENGINE_VERSION,
    }
    payload_json = json.dumps(payload, sort_keys=True)
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("BEGIN IMMEDIATE")
        projection = conn.execute(
            "SELECT contract_path,contract_hash,state FROM fm_v2_policy_projections "
            "WHERE owner_id=? AND project_id=?",
            (owner, project_id),
        ).fetchone()
        if (
            not projection
            or projection[0] != str(source)
            or projection[1] != expected_contract_hash
            or projection[2] != "active"
        ):
            raise HexResolutionError("project activation changed before rename")
        _append_transition_event(
            conn,
            owner=owner,
            operation_id=transition_id,
            project_id=project_id,
            phase="prepared",
            values={
                "contract_hash": expected_contract_hash,
                "actor_id": str(actor_id or owner),
                "payload_hash": payload_hash,
                "contract_path": str(source),
                "old_contract_hash": expected_contract_hash,
                "new_contract_hash": expected_contract_hash,
                "old_bytes": old_bytes,
                "new_bytes": old_bytes,
                "payload_json": payload_json,
            },
        )
    try:
        observed_hash = _atomic_rename_contract(
            source,
            target,
            expected_hash=expected_contract_hash,
        )
        _set_transition_phase(
            db_path,
            owner=owner,
            transition_id=transition_id,
            phase="file_published",
        )
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.row_factory = sqlite3.Row
            changed = conn.execute(
                "UPDATE fm_v2_policy_projections SET contract_path=?,"
                "source_revision=source_revision+1,activation_revision=activation_revision+1,"
                "activated_by=?,activated_at=?,updated_at=? "
                "WHERE owner_id=? AND project_id=? AND contract_path=? "
                "AND contract_hash=? AND state='active'",
                (
                    str(target),
                    str(actor_id or owner),
                    now,
                    now,
                    owner,
                    project_id,
                    str(source),
                    expected_contract_hash,
                ),
            ).rowcount
            if changed != 1:
                raise HexResolutionError("project activation changed during rename")
            previous = _latest_transition_row(
                conn, owner=owner, operation_id=transition_id
            )
            if previous is None:
                raise HexResolutionError("policy transition disappeared")
            _append_transition_event(
                conn,
                owner=owner,
                operation_id=transition_id,
                project_id=project_id,
                phase="activation_advanced",
                values=dict(previous),
            )
        current = resolve_hex(
            project["canonical_root"],
            workspace_root=project["canonical_root"],
        )
        if (
            current.contract_path != str(target)
            or current.contract_hash != observed_hash
            or current.contract_hash != expected_contract_hash
        ):
            raise HexResolutionError("renamed policy did not validate")
        _set_transition_phase(
            db_path, owner=owner, transition_id=transition_id, phase="validated"
        )
        _set_transition_phase(
            db_path,
            owner=owner,
            transition_id=transition_id,
            phase="projection_enqueued",
        )
        _set_transition_phase(
            db_path, owner=owner, transition_id=transition_id, phase="committed"
        )
        return {
            "owner": owner,
            "project_id": project_id,
            "transition_id": transition_id,
            "contract_hash": expected_contract_hash,
            "old_path": str(source),
            "new_path": str(target),
        }
    except BaseException:
        try:
            recover_policy_transitions(
                owner=owner,
                project_id=project_id,
                db_path=db_path,
            )
        except BaseException:
            pass
        raise


def promote_spell(
    spell_id: str,
    *,
    owner: str,
    project_id: str,
    contract_hash: str,
    db_path: str,
    expected_revision: Optional[int] = None,
    candidate_hash: Optional[str] = None,
    actor_id: Optional[str] = None,
) -> dict[str, str]:
    """Publish one reviewed Spell into the live contract through the journal."""
    spells = list_spells(owner=owner, project_id=project_id, db_path=db_path)
    spell = next((item for item in spells if item["spell_id"] == spell_id), None)
    if not spell:
        raise HexResolutionError("Spell not found in this owner/project scope")
    revision = int(expected_revision if expected_revision is not None else spell["revision"])
    preview = preview_spell_promotion(
        spell_id,
        owner=owner,
        project_id=project_id,
        expected_revision=revision,
        db_path=db_path,
    )
    if preview["contract_hash"] != contract_hash:
        raise HexResolutionError("Spell promotion contract hash conflict")
    if candidate_hash and preview["candidate_hash"] != str(candidate_hash):
        raise HexResolutionError("Spell promotion preview is stale")
    return publish_contract_update(
        owner=owner,
        project_id=project_id,
        expected_contract_hash=contract_hash,
        candidate_text=preview["candidate_text"],
        db_path=db_path,
        actor_id=actor_id,
        spell_id=spell_id,
        spell_revision=revision,
    )


def _activation_digest(owner: str, project_id: str, resolution: HexResolution) -> str:
    return hashlib.sha256(
        "\0".join((owner, project_id, resolution.contract_hash or "", resolution.engine_version)).encode("utf-8")
    ).hexdigest()


def activate_hex(
    resolution: HexResolution,
    *,
    owner: str,
    project_id: str,
    db_path: str,
    actor_id: Optional[str] = None,
) -> dict[str, str]:
    """Pin an exact parsed contract for one owner/project.

    Discovery remains side-effect free.  This explicit operation is the trust
    boundary used by policy middleware: the path, bytes hash, engine version,
    and authenticated owner are recorded together, and a later contract drift
    cannot silently inherit the old trust decision.
    """
    owner = str(owner or "").strip()
    project_id = str(project_id or "").strip()
    if not owner or not project_id:
        raise HexResolutionError("owner and project_id are required for activation")
    if (
        not resolution.contract_path
        or not resolution.contract_hash
        or resolution.contract is None
    ):
        raise HexResolutionError("a valid project contract is required for activation")
    ensure_policy_schema(db_path)
    project = get_project(project_id, owner=owner, db_path=db_path)
    if project is None:
        project = register_project(
            resolution.project_root,
            owner=owner,
            workspace_id="global",
            project_id=project_id,
            db_path=db_path,
        )
    if project["canonical_root"] != str(Path(resolution.project_root).resolve()):
        raise HexResolutionError("project contract does not belong to the registered project root")
    current = resolve_hex(
        project["canonical_root"],
        workspace_root=project["canonical_root"],
    )
    if (current.contract_path != resolution.contract_path
            or current.contract_hash != resolution.contract_hash
            or current.contract is None):
        raise HexResolutionError("project contract changed after review; inspect it again before activation")
    identities = _project_identities(Path(project["canonical_root"]))
    if identities != (
        project["root_identity"],
        project["git_identity"],
        project["worktree_identity"],
    ):
        raise HexResolutionError("project identity changed and requires relocation review")
    token = _activation_digest(owner, project_id, resolution)
    now = datetime.now(timezone.utc).isoformat()
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                """
                INSERT INTO fm_v2_policy_projections(
                    owner_id,project_id,contract_path,contract_hash,engine_version,
                    summary_json,source_revision,state,updated_at,workspace_id,
                    canonical_root,root_identity,git_identity,worktree_identity,
                    activation_revision,activated_by,activated_at
                ) VALUES (?,?,?,?,?,?,1,'active',?,?,?,?,?,?,1,?,?)
                ON CONFLICT(owner_id,project_id) DO UPDATE SET
                    contract_path=excluded.contract_path,
                    contract_hash=excluded.contract_hash,
                    engine_version=excluded.engine_version,
                    summary_json=excluded.summary_json,
                    source_revision=fm_v2_policy_projections.source_revision+1,
                    state='active',updated_at=excluded.updated_at,
                    workspace_id=excluded.workspace_id,
                    canonical_root=excluded.canonical_root,
                    root_identity=excluded.root_identity,
                    git_identity=excluded.git_identity,
                    worktree_identity=excluded.worktree_identity,
                    activation_revision=fm_v2_policy_projections.activation_revision+1,
                    activated_by=excluded.activated_by,
                    activated_at=excluded.activated_at
                """,
                (
                    owner,
                    project_id,
                    resolution.contract_path,
                    resolution.contract_hash,
                    resolution.engine_version,
                    json.dumps(
                        {
                            "project_root": resolution.project_root,
                            "diagnostics": list(resolution.diagnostics),
                        },
                        sort_keys=True,
                    ),
                    now,
                    project["workspace_id"],
                    project["canonical_root"],
                    project["root_identity"],
                    project["git_identity"],
                    project["worktree_identity"],
                    str(actor_id or owner),
                    now,
                ),
            )
            transition_id = "transition_" + uuid.uuid4().hex
            _append_transition_event(
                conn,
                owner=owner,
                operation_id=transition_id,
                project_id=project_id,
                phase="committed",
                values={
                    "contract_hash": resolution.contract_hash,
                    "actor_id": str(actor_id or owner),
                    "payload_hash": token,
                    "contract_path": resolution.contract_path,
                    "old_contract_hash": resolution.contract_hash,
                    "new_contract_hash": resolution.contract_hash,
                    "payload_json": json.dumps(
                        {"action": "activate", "project": project},
                        sort_keys=True,
                    ),
                },
            )
        return {"owner": owner, "project_id": project_id, "contract_hash": resolution.contract_hash, "activation_token": token}
    except sqlite3.Error as exc:
        raise HexResolutionError("project contract activation could not be recorded") from exc


def hex_activation_current(
    resolution: HexResolution,
    *,
    owner: str,
    project_id: str,
    db_path: str,
) -> bool:
    """Return true only when the exact current bytes are still trusted."""
    if not resolution.contract_hash:
        return False
    try:
        ensure_policy_schema(db_path)
        with sqlite3.connect(db_path, timeout=30) as conn:
            pending = _pending_transition_rows(
                conn,
                owner=str(owner or "").strip(),
                project_id=str(project_id or "").strip(),
            )
            if pending:
                return False
            row = conn.execute(
                "SELECT contract_path,contract_hash,engine_version,state,canonical_root,root_identity,git_identity,worktree_identity FROM fm_v2_policy_projections WHERE owner_id=? AND project_id=?",
                (str(owner or "").strip(), str(project_id or "").strip()),
            ).fetchone()
        if not row or row[3] != "active":
            return False
        root = Path(resolution.project_root).resolve()
        identities = _project_identities(root)
        current = bool(
            row[0] == resolution.contract_path
            and row[1] == resolution.contract_hash
            and row[2] == resolution.engine_version
            and row[4] == str(root)
            and tuple(row[5:8]) == identities
        )
        if not current:
            with sqlite3.connect(db_path, timeout=30) as conn:
                conn.execute(
                    "UPDATE fm_v2_policy_projections SET state='drifted',updated_at=? WHERE owner_id=? AND project_id=? AND state='active'",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        str(owner or "").strip(),
                        str(project_id or "").strip(),
                    ),
                )
        return current
    except sqlite3.Error:
        return False


def deactivate_hex(
    *,
    owner: str,
    project_id: str,
    expected_activation_revision: int,
    db_path: str,
    actor_id: Optional[str] = None,
) -> dict[str, Any]:
    """Explicitly return one owner's project policy to inspect-only mode."""
    owner = str(owner or "").strip()
    project_id = str(project_id or "").strip()
    if not owner or not project_id:
        raise HexResolutionError("owner and project_id are required")
    ensure_policy_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    transition_id = "transition_" + uuid.uuid4().hex
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT contract_path,contract_hash,activation_revision,state "
            "FROM fm_v2_policy_projections WHERE owner_id=? AND project_id=?",
            (owner, project_id),
        ).fetchone()
        if not row:
            raise HexResolutionError("project policy has never been activated")
        if int(row[2]) != int(expected_activation_revision):
            raise HexResolutionError("project activation revision conflict")
        if row[3] == "deactivated":
            return {
                "owner": owner,
                "project_id": project_id,
                "state": "deactivated",
                "activation_revision": int(row[2]),
            }
        changed = conn.execute(
            "UPDATE fm_v2_policy_projections SET state='deactivated',"
            "activation_revision=activation_revision+1,activated_by=?,"
            "activated_at=?,updated_at=? WHERE owner_id=? AND project_id=? "
            "AND activation_revision=?",
            (
                str(actor_id or owner),
                now,
                now,
                owner,
                project_id,
                expected_activation_revision,
            ),
        ).rowcount
        if changed != 1:
            raise HexResolutionError("project activation revision conflict")
        payload = {
            "action": "deactivate",
            "owner": owner,
            "project_id": project_id,
            "activation_revision": int(expected_activation_revision) + 1,
        }
        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        _append_transition_event(
            conn,
            owner=owner,
            operation_id=transition_id,
            project_id=project_id,
            phase="committed",
            values={
                "contract_hash": row[1],
                "actor_id": str(actor_id or owner),
                "payload_hash": payload_hash,
                "contract_path": row[0],
                "old_contract_hash": row[1],
                "new_contract_hash": row[1],
                "payload_json": json.dumps(payload, sort_keys=True),
            },
        )
    return {
        "owner": owner,
        "project_id": project_id,
        "state": "deactivated",
        "activation_revision": int(expected_activation_revision) + 1,
        "transition_id": transition_id,
    }


def inspect_project_policy(
    project_id: str,
    *,
    owner: str,
    db_path: str,
) -> dict[str, Any]:
    project = get_project(project_id, owner=owner, db_path=db_path)
    if not project:
        raise HexResolutionError("project identity not found")
    resolution: Optional[HexResolution] = None
    diagnostic: Optional[str] = None
    try:
        resolution = resolve_hex(
            project["canonical_root"],
            workspace_root=project["canonical_root"],
        )
        if resolution.contract_path:
            hex_activation_current(
                resolution,
                owner=owner,
                project_id=project_id,
                db_path=db_path,
            )
    except HexResolutionError as exc:
        diagnostic = str(exc)
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        projection = conn.execute(
            "SELECT * FROM fm_v2_policy_projections WHERE owner_id=? AND project_id=?",
            (owner, project_id),
        ).fetchone()
        pending = _pending_transition_rows(
            conn, owner=owner, project_id=project_id
        )
    projection_data = dict(projection) if projection else None
    if projection_data:
        for key in ("old_bytes", "new_bytes"):
            projection_data.pop(key, None)
    resolution_data = None
    if resolution:
        resolution_data = {
            "project_root": resolution.project_root,
            "contract_path": resolution.contract_path,
            "contract_hash": resolution.contract_hash,
            "contract": resolution.contract,
            "state": projection_data.get("state", resolution.state)
            if projection_data
            else resolution.state,
            "diagnostics": list(resolution.diagnostics),
            "engine_version": resolution.engine_version,
        }
    return {
        "project": project,
        "policy": resolution_data,
        "projection": projection_data,
        "spells": list_spells(owner=owner, project_id=project_id, db_path=db_path),
        "pending_transitions": [
            {
                "transition_id": row["operation_id"] or row["transition_id"],
                "phase": row["phase"],
                "sequence": row["sequence"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in pending
        ],
        "diagnostic": diagnostic,
    }


def require_project_mutation_admission(
    *,
    owner: str,
    workspace: str,
    db_path: str,
) -> Optional[dict[str, Any]]:
    """Apply the shared active/drifted gate at a mutation boundary.

    Never-activated and deactivated projects stay parse/display-only as the
    contract requires.  Once an owner activates a contract, every registered
    native mutation path must present the same stable project identity and
    exact live hash or fail closed.
    """
    project = project_for_root(workspace, owner=owner, db_path=db_path)
    if not project:
        return None
    with sqlite3.connect(db_path, timeout=30) as conn:
        row = conn.execute(
            "SELECT state FROM fm_v2_policy_projections WHERE owner_id=? AND project_id=?",
            (owner, project["project_id"]),
        ).fetchone()
    if not row or row[0] in {"never_activated", "deactivated"}:
        return project
    if row[0] == "drifted":
        raise HexResolutionError("active project policy has drifted and blocks mutations")
    require_hex_activation(
        workspace,
        owner=owner,
        project_id=project["project_id"],
        db_path=db_path,
        workspace_root=workspace,
    )
    return project


def inspect_registered_project_mutation_policy(
    *,
    owner: str,
    workspace: str,
    db_path: str,
) -> dict[str, Any]:
    """Return the one session-safe policy decision used by remote runtimes."""
    project = require_project_mutation_admission(
        owner=owner, workspace=workspace, db_path=db_path
    )
    if not project:
        return {"enforced": False, "project": None, "state": "unregistered"}
    with sqlite3.connect(db_path, timeout=30) as conn:
        row = conn.execute(
            "SELECT state FROM fm_v2_policy_projections "
            "WHERE owner_id=? AND project_id=?",
            (owner, project["project_id"]),
        ).fetchone()
    state = str(row[0]) if row else "never_activated"
    if state in {"never_activated", "deactivated"}:
        return {"enforced": False, "project": project, "state": state}
    resolution = require_hex_activation(
        project["canonical_root"],
        owner=owner,
        project_id=project["project_id"],
        db_path=db_path,
        workspace_root=project["canonical_root"],
    )
    require_executable_trust(
        resolution,
        owner=owner,
        project_id=project["project_id"],
        db_path=db_path,
    )
    return {"enforced": True, "project": project, "state": state}


def global_policy_context(
    *,
    owner: str,
    workspace: str,
    db_path: str,
) -> dict[str, Any]:
    """Return the stable, non-semantic policy context for an authenticated lane.

    This projection is deliberately separate from memory/RAG retrieval.  It is
    safe to place in agent or diagnostics context because it contains only the
    active contract identity, canonical path vocabulary, and remediation
    instructions; it never contains a policy rule body, spell, or memory hit.
    Project lookup is owner-scoped so a caller cannot use this diagnostic to
    discover another owner's registration or activation state.
    """
    owner = str(owner or "").strip()
    workspace = str(workspace or "").strip()
    if not owner or not workspace:
        raise HexResolutionError("authenticated owner and workspace are required")
    root = _canonical_project_root(workspace)
    project = project_for_root(root, owner=owner, db_path=db_path)
    resolution: Optional[HexResolution] = None
    diagnostic: Optional[str] = None
    state = "unregistered"
    enforced = False
    if project:
        state = "never_activated"
    try:
        resolution = resolve_hex(root, workspace_root=root)
        if project:
            with sqlite3.connect(db_path, timeout=30) as conn:
                row = conn.execute(
                    "SELECT state FROM fm_v2_policy_projections "
                    "WHERE owner_id=? AND project_id=?",
                    (owner, project["project_id"]),
                ).fetchone()
            state = str(row[0]) if row else "never_activated"
            if state == "active":
                enforced = bool(
                    resolution.contract_path
                    and hex_activation_current(
                        resolution,
                        owner=owner,
                        project_id=project["project_id"],
                        db_path=db_path,
                    )
                )
                if not enforced:
                    state = "drifted"
    except (HexResolutionError, OSError, sqlite3.Error) as exc:
        diagnostic = str(exc)[:512]
        if project and state == "active":
            state = "unavailable"
            enforced = False

    contract_relative = None
    if resolution and resolution.contract_path:
        try:
            contract_relative = Path(resolution.contract_path).resolve().relative_to(root).as_posix()
        except ValueError:
            diagnostic = diagnostic or "resolved contract is outside the project root"
    return {
        "schema": "open-clank-policy-context/v1",
        "authority": "global_hex_policy",
        "semantic_memory": False,
        "owner_scoped": True,
        "project_id": project["project_id"] if project else None,
        "project_root": str(root),
        "state": state,
        "enforced": enforced,
        "activation": {
            "contract_path": contract_relative,
            "contract_hash": resolution.contract_hash if resolution else None,
            "contract_version": (
                str((resolution.contract or {}).get("contract") or "")
                if resolution and resolution.contract
                else None
            ),
            "engine_version": resolution.engine_version if resolution else None,
        },
        "canonical_paths": {
            "archive": f"{CLANKER_ARCHIVE_DIR}/",
            "plans": f"{CLANKER_FUTURES_DIR}/",
            "hexes": f"{CLANKER_HEXES_DIR}/",
            "robonotes": f"{CLANKER_ROBONOTES_DIR}/",
            "tools": f"{CLANKER_TOOLS_DIR}/",
            "references": f"{CLANKER_REFERENCES_DIR}/",
        },
        "explain": (
            f"scripts/openclank hex explain {contract_relative or GLOBAL_HEX_CONTRACT} --json"
        ),
        "remediation": (
            "Use the canonical paths above; an active-policy drift requires owner reactivation."
        ),
        "diagnostic": diagnostic,
    }


def validate_registered_project_candidates(
    *,
    owner: str,
    workspace: str,
    db_path: str,
    candidates: Mapping[str, Optional[bytes]],
) -> dict[str, Any]:
    """Apply active project policy to one exact native mutation candidate."""
    project = require_project_mutation_admission(
        owner=owner, workspace=workspace, db_path=db_path
    )
    if not project:
        return {"enforced": False, "allowed": True, "findings": [], "warnings": []}
    with sqlite3.connect(db_path, timeout=30) as conn:
        row = conn.execute(
            "SELECT state FROM fm_v2_policy_projections "
            "WHERE owner_id=? AND project_id=?",
            (owner, project["project_id"]),
        ).fetchone()
    if not row or row[0] in {"never_activated", "deactivated"}:
        return {"enforced": False, "allowed": True, "findings": [], "warnings": []}
    resolution = require_hex_activation(
        workspace,
        owner=owner,
        project_id=project["project_id"],
        db_path=db_path,
        workspace_root=project["canonical_root"],
    )
    result = validate_project_file_candidates(
        resolution,
        owner=owner,
        project_id=project["project_id"],
        db_path=db_path,
        candidates=candidates,
    )
    return {"enforced": True, **result}


def _git_root(start: Path) -> Optional[Path]:
    current = start
    for candidate in (current, *current.parents):
        marker = candidate / ".git"
        if marker.is_dir() or marker.is_file():
            return candidate
    return None


def _read_contract_bytes(path: Path) -> Optional[bytes]:
    """Read one stable regular file without following a final symlink."""
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HexResolutionError(f"project contract could not be inspected: {path}") from exc
    if stat.S_ISLNK(before.st_mode):
        raise HexResolutionError(f"project contracts cannot be symlinks in {path.parent}")
    if not stat.S_ISREG(before.st_mode):
        raise HexResolutionError(f"project contract must be a regular file: {path}")
    if before.st_size > MAX_BYTES:
        raise HexResolutionError("project contract exceeds the 1 MiB limit")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise HexResolutionError(f"project contract changed while opening: {path}") from exc
    except OSError as exc:
        raise HexResolutionError(f"project contract could not be opened safely: {path}") from exc

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise HexResolutionError(f"project contract must be a regular file: {path}")
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size != before.st_size
            or opened.st_mtime_ns != before.st_mtime_ns
        ):
            raise HexResolutionError(f"project contract changed while opening: {path}")
        if opened.st_size > MAX_BYTES:
            raise HexResolutionError("project contract exceeds the 1 MiB limit")

        payload = bytearray()
        while len(payload) <= MAX_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, MAX_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > MAX_BYTES:
            raise HexResolutionError("project contract exceeds the 1 MiB limit")

        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            raise HexResolutionError(f"project contract changed while reading: {path}")
        return bytes(payload)
    except HexResolutionError:
        raise
    except OSError as exc:
        raise HexResolutionError(f"project contract could not be read safely: {path}") from exc
    finally:
        os.close(descriptor)


def _safe_yaml(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_BYTES:
        raise HexResolutionError("project contract exceeds the 1 MiB limit")
    try:
        value = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise HexResolutionError("project contract is not safe YAML") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HexResolutionError("project contract must be a YAML mapping")
    # SafeLoader rejects executable tags, but aliases and deeply nested
    # containers can still consume unbounded memory before policy evaluation.
    # Count the parsed graph without expanding aliases a second time.
    stack = [(value, 0)]
    seen: set[int] = set()
    nodes = 0
    while stack:
        item, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise HexResolutionError("project contract nesting exceeds the 128-level limit")
        identity = id(item)
        if isinstance(item, (dict, list, tuple, set)):
            if identity in seen:
                continue
            seen.add(identity)
            nodes += 1
            if nodes > MAX_NODES:
                raise HexResolutionError("project contract contains too many nodes")
            values = item.values() if isinstance(item, dict) else item
            stack.extend((child, depth + 1) for child in values)
        elif isinstance(item, (str, bytes)):
            nodes += 1
            if len(item) > MAX_BYTES:
                raise HexResolutionError("project contract scalar exceeds the 1 MiB limit")
    return value


def resolve_hex(target: str | os.PathLike[str], *, workspace_root: str | os.PathLike[str] | None = None) -> HexResolution:
    path = Path(target).expanduser().resolve(strict=False)
    if path.exists() and path.is_file():
        path = path.parent
    if not path.exists() or not path.is_dir():
        raise HexResolutionError("target is not an existing directory or file")
    git_root = _git_root(path)
    root = git_root or (Path(workspace_root).expanduser().resolve() if workspace_root else path)
    if workspace_root:
        configured = Path(workspace_root).expanduser().resolve()
        try:
            path.relative_to(configured)
        except ValueError as exc:
            raise HexResolutionError("target escapes the declared workspace root") from exc
        root = configured
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise HexResolutionError("target is outside project root") from exc
    current = path
    while True:
        canonical_payloads = {
            current / name: raw
            for name in GLOBAL_HEX_CONTRACT_PATHS
            if (raw := _read_contract_bytes(current / name)) is not None
        }
        primary = current / ".hex"
        primary_raw = _read_contract_bytes(primary)
        legacy_payloads = {
            current / name: raw
            for name in CONTRACT_NAMES[1:]
            if (raw := _read_contract_bytes(current / name)) is not None
        }
        if canonical_payloads:
            canonical, canonical_raw = next(iter(canonical_payloads.items()))
            value = _safe_yaml(canonical_raw)
            compatibility_payloads = {
                **{path: raw for path, raw in canonical_payloads.items() if path != canonical},
                primary: primary_raw,
                **legacy_payloads,
            }
            diagnostics = tuple(
                f"ignored legacy contract after canonical v2 discovery: {path.relative_to(current)}"
                for path, raw in sorted(compatibility_payloads.items(), key=lambda item: str(item[0]))
                if raw is not None
            )
            return HexResolution(
                str(root),
                str(canonical),
                hashlib.sha256(canonical_raw).hexdigest(),
                value,
                "never_activated",
                diagnostics,
                _engine_version_for_contract(value),
            )
        if primary_raw is not None:
            value = _safe_yaml(primary_raw)
            first_party = value.get("contract") in SUPPORTED_FIRST_PARTY_CONTRACTS and "hexes" in value
            if legacy_payloads and not first_party:
                raise HexResolutionError(f"duplicate project contracts in {current}")
            diagnostics = (
                tuple(
                    f"ignored legacy contract after first-party .hex migration: {path.name}"
                    for path in sorted(legacy_payloads, key=str)
                )
                if legacy_payloads
                else ()
            )
            return HexResolution(
                str(root),
                str(primary),
                hashlib.sha256(primary_raw).hexdigest(),
                value,
                "never_activated",
                diagnostics,
                _engine_version_for_contract(value),
            )
        if legacy_payloads:
            if len(legacy_payloads) > 1:
                raise HexResolutionError(f"duplicate legacy project contracts in {current}")
            contract_path = next(iter(legacy_payloads))
            raw = legacy_payloads[contract_path]
            value = _safe_yaml(raw)
            return HexResolution(
                str(root),
                str(contract_path),
                hashlib.sha256(raw).hexdigest(),
                value,
                "never_activated",
                (),
                _engine_version_for_contract(value),
            )
        if current == root or current.parent == current:
            break
        current = current.parent
    return HexResolution(str(root), None, None, None, "never_activated", ("no project contract found",))
