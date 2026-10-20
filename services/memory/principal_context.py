"""Idempotent, scoped Memory principals and late identity rendering.

The authenticated owner remains the authorization boundary.  This module only
projects two reserved ontology principals into the existing FrankenMemory v2
entity/role tables: the assistant's self and the human Handler.  Names are
presentation data and never participate in the stable IDs.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
from typing import Any, Optional

from src.constants import FM_DB_PATH

CONTRACT = "openclank.memory-principal-context/v1"
ENTITY_MATCH_CANDIDATE_CONTRACT = "openclank.memory-entity-match-candidate/v1"
MAX_ASSISTANT_LABEL_LENGTH = 120
_ASSISTANT_RESERVED_ALIASES = ("me", "I", "myself")
_ASSISTANT_ENTITY_ID_RE = re.compile(r"\Aprincipal_assistant_[0-9a-f]{32}\Z")
_UNSAFE_SELF_MATCH_ALIASES = frozenset({"i", "me", "myself"})

# In-process memo of scopes whose principals are already ensured. The key
# carries the resolved persona label, so a rename naturally re-runs the
# sync; `invalidate_principal_cache` forces it explicitly.
_ENSURED_SCOPES: set[tuple[str, ...]] = set()
_ENSURED_LOCK = threading.Lock()

_HANDLER_NAME_PATTERNS = (
    re.compile(r"\A%USER%(?:'s|’s)\s+name\s+is\s+(.+?)[.!?]?\Z", re.IGNORECASE),
    re.compile(
        r"\A%USER%(?:'s|’s)\s+preferred\s+name\s+is\s+(.+?)[.!?]?\Z",
        re.IGNORECASE,
    ),
    re.compile(r"\A%USER%\s+is\s+named\s+(.+?)[.!?]?\Z", re.IGNORECASE),
)
_NEUTRAL_HANDLER_LABELS = frozenset(
    {"handler", "user", "the user", "i", "me", "myself", "self"}
)


def _clean_assistant_label(value: Any) -> str:
    """Return a bounded label without inventing a fallback identity."""

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    clean = []
    for character in normalized:
        if character.isspace():
            clean.append(" ")
        elif not unicodedata.category(character).startswith("C"):
            clean.append(character)
    label = " ".join("".join(clean).split())
    return label[:MAX_ASSISTANT_LABEL_LENGTH].rstrip()


def _sanitize_assistant_label(value: Any) -> str:
    """Return a bounded, single-line presentation label for assistant-self."""

    return _clean_assistant_label(value) or "Open Clank"


def resolve_assistant_persona_label(
    owner: str,
    supplied: Optional[str] = None,
) -> str:
    """Prefer an explicit label, otherwise read the owner's default persona."""
    value: Any = supplied
    if supplied is None:
        try:
            from src.default_persona import get_default_persona

            value = get_default_persona(owner).get("name")
        except Exception:
            value = None
    return _sanitize_assistant_label(value)


def _normalize_alias_match_text(value: Any) -> str:
    """Normalize presentation text for an exact, Unicode-aware alias match."""

    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def build_assistant_entity_match_candidate(
    *,
    source_text: Any,
    matched_alias: Any,
    assistant_label: Any,
    assistant_entity_id: Any,
) -> Optional[dict[str, Any]]:
    """Build one review-only assistant-self candidate from trusted context.

    ``assistant_entity_id`` must be the opaque reserved principal produced by
    :func:`ensure_principal_context`.  Uploaded/model-authored IDs therefore do
    not become candidates.  The alias is accepted only when it is the Persona
    label itself and appears as a token-bounded exact match in the source after
    NFKC, case-fold, and whitespace normalization.
    """

    entity_id = str(assistant_entity_id or "").strip()
    if not _ASSISTANT_ENTITY_ID_RE.fullmatch(entity_id):
        return None

    raw_alias = str(matched_alias or "")
    raw_label = str(assistant_label or "")
    if not raw_alias.strip() or not raw_label.strip():
        return None
    # Candidate matching must not turn controls-only attacker/model text into
    # the presentation fallback ("Open Clank").  An empty cleaned alias is no
    # match; fallback behavior belongs only to Persona display resolution.
    alias = _clean_assistant_label(raw_alias)
    label = _clean_assistant_label(raw_label)
    alias_key = _normalize_alias_match_text(alias)
    label_key = _normalize_alias_match_text(label)
    if (
        not alias_key
        or alias_key in _UNSAFE_SELF_MATCH_ALIASES
        or alias_key != label_key
    ):
        return None

    source_key = _normalize_alias_match_text(source_text)
    if not source_key or re.search(
        rf"(?<!\w){re.escape(alias_key)}(?!\w)",
        source_key,
        flags=re.UNICODE,
    ) is None:
        return None

    return {
        "contract": ENTITY_MATCH_CANDIDATE_CONTRACT,
        "entity_id": entity_id,
        "role": "assistant_self",
        "matched_alias": alias,
        "match_method": "persona_setting_exact",
        "state": "proposed",
        "requires_review": True,
    }


def _merge_aliases(existing: Any, *required: str) -> list[str]:
    """Preserve valid aliases while adding required reviewed aliases once."""
    aliases: list[str] = []
    for candidate in [*(existing if isinstance(existing, list) else []), *required]:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        if candidate not in aliases:
            aliases.append(candidate)
    return aliases


def account_principal_id(owner: Optional[str]) -> Optional[str]:
    """Resolve the immutable auth account ID without exposing auth material."""
    owner = str(owner or "").strip().lower()
    if not owner:
        return None
    try:
        from src.constants import AUTH_FILE
        with open(AUTH_FILE, "r", encoding="utf-8") as handle:
            users = (json.load(handle) or {}).get("users") or {}
        value = str((users.get(owner) or {}).get("account_id") or "").strip()
        return value or None
    except (OSError, TypeError, ValueError):
        return None


def _stable_id(kind: str, owner: str, workspace: str, project: str, principal: str) -> str:
    raw = "\x1f".join((CONTRACT, kind, owner, workspace, project, principal))
    return f"principal_{kind}_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _reviewed_handler_label(
    *,
    owner: str,
    workspace: str,
    handler_entity_id: str,
    db_path: str,
) -> Optional[str]:
    """Read one exact, owner-reviewed Handler name from compatibility memory.

    This deliberately does not infer a name from arbitrary prose, the login
    username, or model metadata.  The import review must be a curated Handler
    profile identity claim with the server-derived reserved entity ID.  It is
    a migration bridge until the typed ``preferred_name`` claim is canonical.
    """
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "curated" not in tables:
            return None
        rows = conn.execute(
            "SELECT content,kind,metadata FROM curated "
            "WHERE owner=? AND archived=0 "
            "AND (workspace_id=? OR workspace_id='global') "
            "ORDER BY updated_at DESC,id DESC LIMIT 500",
            (owner, workspace),
        ).fetchall()
    except (OSError, sqlite3.Error):
        return None
    finally:
        if conn is not None:
            conn.close()

    for row in rows:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(metadata, dict):
            continue
        attribution = metadata.get("subject_attribution")
        review = metadata.get("import_review")
        if not isinstance(attribution, dict) or not isinstance(review, dict):
            continue
        if (
            attribution.get("contract")
            != "openclank.memory-subject-attribution/v1"
            or attribution.get("role") != "handler"
            or attribution.get("entity_id") != handler_entity_id
            or metadata.get("source_document_role") != "handler_profile"
            or str(review.get("reviewed_by") or "").strip() != owner
            or str(metadata.get("category") or "").strip().lower() != "identity"
            or str(row["kind"] or "").strip().lower() not in {"persona", "fact"}
        ):
            continue
        content = str(row["content"] or "").strip()
        match = next(
            (pattern.fullmatch(content) for pattern in _HANDLER_NAME_PATTERNS if pattern.fullmatch(content)),
            None,
        )
        if match is None:
            continue
        label = _clean_assistant_label(match.group(1).strip(" \t\"'“”‘’"))
        key = label.casefold()
        if (
            not label
            or key in _NEUTRAL_HANDLER_LABELS
            or "%" in label
            or len(label) > MAX_ASSISTANT_LABEL_LENGTH
        ):
            continue
        return label
    return None


def _ensure_binding_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fm_v2_principal_bindings (
            owner_id TEXT NOT NULL,
            workspace_key TEXT NOT NULL DEFAULT '',
            project_key TEXT NOT NULL DEFAULT '',
            binding_key TEXT NOT NULL,
            assistant_entity_id TEXT NOT NULL,
            handler_entity_id TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(owner_id, workspace_key, project_key, binding_key)
        )
        """
    )


def ensure_principal_context(
    *,
    owner: Optional[str],
    workspace_id: Optional[str] = None,
    project_id: Optional[str] = None,
    assistant_principal_id: str = "default",
    assistant_label: Optional[str] = None,
    handler_principal_id: Optional[str] = None,
    handler_label: Optional[str] = None,
    db_path: str = FM_DB_PATH,
) -> Optional[dict[str, Any]]:
    """Create/reuse the reserved principals for one exact scope.

    Legacy or uninitialised databases are treated as unavailable rather than
    making ordinary compatibility Memory calls fail.  Once v2 is installed,
    this is deterministic and safe to call on every read/write path.
    """
    owner = str(owner or "").strip()
    if not owner:
        return None
    workspace = str(workspace_id or "global").strip() or "global"
    project = str(project_id or "").strip()
    assistant_key = str(assistant_principal_id or "default").strip() or "default"
    assistant_display_label = resolve_assistant_persona_label(owner, assistant_label)
    handler_key = str(handler_principal_id or account_principal_id(owner) or owner).strip() or owner
    assistant_id = _stable_id("assistant", owner, workspace, project, assistant_key)
    handler_id = _stable_id("handler", owner, workspace, project, handler_key)
    supplied_handler_label = (
        _clean_assistant_label(handler_label) if handler_label is not None else ""
    )
    handler_display_label = supplied_handler_label or _reviewed_handler_label(
        owner=owner,
        workspace=workspace,
        handler_entity_id=handler_id,
        db_path=str(db_path),
    ) or "Handler"
    binding_key = f"assistant:{assistant_key}:handler:{handler_key}"
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required = {
            "fm_v2_change_sets",
            "fm_v2_entities",
            "fm_v2_entity_revisions",
            "fm_v2_entity_roles",
            "fm_v2_outbox",
        }
        if not required.issubset(tables):
            conn.close()
            return None
        _ensure_binding_table(conn)
        conn.commit()
        # The schema probe connection is done; release it before the binding
        # upsert opens its own write connection below.
        conn.close()
        from src.frankenmemory_v2 import V2OperationError, V2Repository

        repo = V2Repository(str(db_path))

        def ensure_entity(entity_id: str, *, kind: str, label: str, role: str, principal: str) -> None:
            current = None
            try:
                current = repo.get_entity(owner=owner, entity_id=entity_id, workspace_id=workspace, project_id=project or None)
            except Exception:
                pass
            if current is None:
                try:
                    repo.create_entity(
                        owner=owner,
                        entity_id=entity_id,
                        entity_type="assistant" if kind == "assistant" else "person",
                        canonical_label=(label or ("Open Clank" if kind == "assistant" else "Handler")).strip(),
                        workspace_id=workspace,
                        project_id=project or None,
                        aliases=(
                            _merge_aliases([], *_ASSISTANT_RESERVED_ALIASES, label)
                            if kind == "assistant"
                            else _merge_aliases([], "handler", label)
                        ),
                        roles=[role],
                        tags=["reserved", "principal", principal],
                        actor_type="system",
                        actor_id="memory-principal-bootstrap",
                        reason="bootstrap reserved Memory principals",
                    )
                except Exception as exc:
                    # Another concurrent bootstrap may have won the insert race.
                    try:
                        current = repo.get_entity(owner=owner, entity_id=entity_id, workspace_id=workspace, project_id=project or None)
                    except Exception:
                        raise exc
                else:
                    return

            for attempt in range(3):
                if kind == "assistant":
                    aliases = _merge_aliases(
                        current.get("aliases"),
                        *_ASSISTANT_RESERVED_ALIASES,
                        label,
                    )
                    sync_label = True
                    actor_id = "memory-persona-sync"
                    reason = "sync reviewed default persona name"
                else:
                    aliases = _merge_aliases(
                        current.get("aliases"),
                        "handler",
                        current.get("canonical_label"),
                        label,
                    )
                    # A missing name claim must not overwrite a reviewed/manual
                    # name with the neutral bootstrap label.
                    sync_label = label.casefold() not in _NEUTRAL_HANDLER_LABELS
                    actor_id = "memory-handler-name-sync"
                    reason = "sync owner-reviewed Handler preferred name"
                desired_label = label if sync_label else current.get("canonical_label")
                if current.get("canonical_label") == desired_label and aliases == current.get("aliases"):
                    return
                try:
                    repo.update_entity(
                        owner=owner,
                        entity_id=entity_id,
                        expected_revision=current["revision"],
                        payload={"canonical_label": desired_label, "aliases": aliases},
                        workspace_id=workspace,
                        project_id=project or None,
                        actor_type="system",
                        actor_id=actor_id,
                        reason=reason,
                    )
                    return
                except V2OperationError:
                    # A concurrent ensure may have won the compare-and-swap.
                    # Accept an equivalent winner; otherwise retry from its
                    # newest revision without discarding aliases it introduced.
                    current = repo.get_entity(
                        owner=owner,
                        entity_id=entity_id,
                        workspace_id=workspace,
                        project_id=project or None,
                    )
                    winner_aliases = (
                        _merge_aliases(
                            current.get("aliases"),
                            *_ASSISTANT_RESERVED_ALIASES,
                            label,
                        )
                        if kind == "assistant"
                        else _merge_aliases(
                            current.get("aliases"),
                            "handler",
                            current.get("canonical_label"),
                            label,
                        )
                    )
                    if (
                        current.get("canonical_label") == desired_label
                        and winner_aliases == current.get("aliases")
                    ):
                        return
                    if attempt == 2:
                        raise

        ensure_entity(assistant_id, kind="assistant", label=assistant_display_label, role=f"assistant_self:{assistant_key}", principal=assistant_key)
        ensure_entity(handler_id, kind="handler", label=handler_display_label, role="handler", principal=handler_key)
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO fm_v2_principal_bindings
               (owner_id,workspace_key,project_key,binding_key,assistant_entity_id,handler_entity_id,revision,created_at,updated_at)
               VALUES (?,?,?,?,?,?,1,?,?)
               ON CONFLICT(owner_id,workspace_key,project_key,binding_key) DO UPDATE SET
                 assistant_entity_id=excluded.assistant_entity_id,
                 handler_entity_id=excluded.handler_entity_id,
                 revision=CASE WHEN fm_v2_principal_bindings.assistant_entity_id=excluded.assistant_entity_id
                                    AND fm_v2_principal_bindings.handler_entity_id=excluded.handler_entity_id
                               THEN fm_v2_principal_bindings.revision
                               ELSE fm_v2_principal_bindings.revision+1 END,
                 updated_at=excluded.updated_at""",
            (owner, workspace, project, binding_key, assistant_id, handler_id, now, now),
        )
        row = conn.execute(
            "SELECT owner_id,workspace_key,project_key,binding_key,assistant_entity_id,handler_entity_id,revision FROM fm_v2_principal_bindings WHERE owner_id=? AND workspace_key=? AND project_key=? AND binding_key=?",
            (owner, workspace, project, binding_key),
        ).fetchone()
        conn.commit()
        conn.close()
        return dict(row) if row else None
    except (OSError, RuntimeError, sqlite3.Error, ValueError):
        # V2OperationError is a RuntimeError: a conflicting or unavailable v2
        # repository also fails open instead of 500-ing the caller's route.
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return None


def invalidate_principal_cache(owner: Optional[str]) -> None:
    """Drop memoized scopes for exactly one durable owner.

    An empty owner used to mean "clear every tenant". That is unsafe for
    owner-scoped reset and rename call sites, where a missing context must fail
    closed instead of widening the mutation.
    """
    owner_key = str(owner or "").strip()
    if not owner_key:
        raise ValueError("principal cache invalidation requires an owner")
    with _ENSURED_LOCK:
        _ENSURED_SCOPES.difference_update(
            {key for key in _ENSURED_SCOPES if key[1] == owner_key}
        )


def ensure_principal_context_cached(
    *,
    owner: Optional[str],
    workspace_id: Optional[str] = None,
    project_id: Optional[str] = None,
    assistant_principal_id: str = "default",
    assistant_label: Optional[str] = None,
    handler_principal_id: Optional[str] = None,
    handler_label: Optional[str] = None,
    db_path: str = FM_DB_PATH,
) -> Optional[dict[str, Any]]:
    """Ensure once per unchanged identity, then answer from the memo.

    The memo key carries the resolved persona label, so a rename re-runs
    the sync exactly once; callers on hot paths should still dispatch via
    ``asyncio.to_thread`` since the first (or post-rename) call performs
    the synchronous sqlite bootstrap. Failures are not memoized.
    """
    owner_key = str(owner or "").strip()
    if not owner_key:
        return None
    workspace = str(workspace_id or "global").strip() or "global"
    project = str(project_id or "").strip()
    label = resolve_assistant_persona_label(owner_key, assistant_label)
    assistant_key = str(assistant_principal_id or "default").strip() or "default"
    handler_key = (
        str(handler_principal_id or account_principal_id(owner_key) or owner_key).strip()
        or owner_key
    )
    resolved_handler_label = (
        _clean_assistant_label(handler_label)
        if handler_label is not None
        else resolve_handler_display_label(
            owner_key,
            workspace_id=workspace,
            project_id=project or None,
            db_path=str(db_path),
        )
    ) or "Handler"
    key = (
        str(db_path),
        owner_key,
        workspace,
        project,
        assistant_key,
        label,
        handler_key,
        resolved_handler_label,
    )
    with _ENSURED_LOCK:
        if key in _ENSURED_SCOPES:
            return _read_binding(
                db_path=str(db_path),
                owner=owner_key,
                workspace=workspace,
                project=project,
                assistant_principal_id=assistant_principal_id,
                handler_principal_id=handler_principal_id,
            )
    result = ensure_principal_context(
        owner=owner_key,
        workspace_id=workspace,
        project_id=project,
        assistant_principal_id=assistant_principal_id,
        assistant_label=assistant_label,
        handler_principal_id=handler_principal_id,
        handler_label=resolved_handler_label,
        db_path=db_path,
    )
    if result is not None:
        with _ENSURED_LOCK:
            _ENSURED_SCOPES.add(key)
    return result


def _read_binding(
    *,
    db_path: str,
    owner: str,
    workspace: str,
    project: str,
    assistant_principal_id: str,
    handler_principal_id: Optional[str],
) -> Optional[dict[str, Any]]:
    """Re-read one memoized scope's binding row (one indexed SELECT)."""
    assistant_key = str(assistant_principal_id or "default").strip() or "default"
    handler_key = (
        str(handler_principal_id or account_principal_id(owner) or owner).strip()
        or owner
    )
    binding_key = f"assistant:{assistant_key}:handler:{handler_key}"
    try:
        with sqlite3.connect(str(db_path), timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "fm_v2_principal_bindings" not in tables:
                return None
            row = conn.execute(
                "SELECT owner_id,workspace_key,project_key,binding_key,assistant_entity_id,handler_entity_id,revision FROM fm_v2_principal_bindings WHERE owner_id=? AND workspace_key=? AND project_key=? AND binding_key=?",
                (owner, workspace, project, binding_key),
            ).fetchone()
        return dict(row) if row else None
    except (OSError, sqlite3.Error):
        return None


def resolve_handler_display_label(
    owner: Optional[str],
    *,
    workspace_id: Optional[str] = None,
    project_id: Optional[str] = None,
    db_path: str = FM_DB_PATH,
) -> str:
    """Return the scope's current Handler display label for read-time rendering.

    Names are presentation data and never participate in stable IDs, so a
    Handler rename is picked up here without touching stored claims.  This is
    read-only and failure-safe: a missing binding/entity or an unavailable
    store yields the neutral ``"Handler"`` fallback instead of breaking the
    read path.
    """
    owner_key = str(owner or "").strip()
    if not owner_key:
        return "Handler"
    workspace = str(workspace_id or "global").strip() or "global"
    project = str(project_id or "").strip()
    # Mirror ensure_principal_context's handler identity exactly: same
    # principal key and exact scope, same stable ID inputs.
    handler_key = str(account_principal_id(owner_key) or owner_key).strip() or owner_key
    handler_id = _stable_id("handler", owner_key, workspace, project, handler_key)
    label = ""
    try:
        from src.frankenmemory_v2 import V2Repository

        entity = V2Repository(str(db_path)).get_entity(
            owner=owner_key,
            entity_id=handler_id,
            workspace_id=workspace,
            project_id=project or None,
        )
    except Exception:
        entity = None
    label = _clean_assistant_label((entity or {}).get("canonical_label"))
    if label and label.casefold() not in _NEUTRAL_HANDLER_LABELS:
        return label
    reviewed = _reviewed_handler_label(
        owner=owner_key,
        workspace=workspace,
        handler_entity_id=handler_id,
        db_path=str(db_path),
    )
    return reviewed or label or "Handler"


def render_identity_template(text: str, *, handler_label: str = "Handler", assistant_subject: str = "me") -> str:
    """Expand only explicit compatibility tokens; ordinary prose is untouched."""
    result = str(text or "")
    replacements = {
        "%USER%": handler_label or "Handler",
        "%HANDLER%": handler_label or "Handler",
        "%SELF%": assistant_subject or "me",
        "%SELF{subject}%": assistant_subject or "me",
        "%SELF{object}%": "me",
        "%SELF{poss_det}%": "my",
        "%SELF{poss_pron}%": "mine",
        "%SELF{reflexive}%": "myself",
        "%SELF{subject}": assistant_subject or "me",
        "%SELF{object}": "me",
        "%SELF{poss_det}": "my",
        "%SELF{poss_pron}": "mine",
        "%SELF{reflexive}": "myself",
    }
    for token, value in replacements.items():
        result = result.replace(token, value)
    return result
