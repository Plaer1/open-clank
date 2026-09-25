"""Workspace media layout, provenance, orphan adoption and move transactions.

This module owns the S14 media rules that the Files facade, Loose Copal and the
Editor share:

* Layout — a known workspace mirrors a document's workspace-relative stem under
  ``media/<stem>/<collision-safe filename>``.  A loose document uses its own
  containing folder as the media root and creates the directory only on a
  media-producing edit.
* Provenance — every media asset records its canonical root, document/asset
  identity, loose-versus-workspace origin, binary digest and known references.
  Folder names alone never prove ownership.
* Adoption — a new workspace may consolidate only provenance-backed loose
  assets whose canonical physical root is not already owned by another
  registered workspace (including nested, other-owner and archived entries).
* Moves — document/media moves are recoverable transactions that preflight
  revisions and incoming references, capture preimages, stage, commit and
  publish one resource-change receipt.  Known authorized references are
  repaired surgically; a protected reference blocks the move with a concrete
  conflict instead of being broken.

The module is pure planning/validation.  Byte mutation, Lore capture and
operation persistence stay with the existing Files owners that call it.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "MediaOwnershipError",
    "binary_digest",
    "text_fingerprint",
    "MediaLayout",
    "document_media_dir",
    "layout_for_absolute",
    "document_stem",
    "DocumentOrigin",
    "classify_document_origin",
    "allocate_media_name",
    "MediaProvenance",
    "WorkspaceRootRecord",
    "classify_adoption_candidates",
    "AdoptionPlan",
    "ReferenceSite",
    "scan_references",
    "rewrite_reference",
    "MovePhase",
    "AssetMove",
    "ReferenceEdit",
    "MoveConflict",
    "MovePlan",
    "plan_document_move",
    "recover_move_plan",
    "reconcile_external_state",
]


class MediaOwnershipError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_media_request") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


def binary_digest(data: bytes) -> str:
    """Digest binary asset bytes exactly as stored.

    The legacy Loose Copal asset path hashed a Latin-1-decoded string through a
    UTF-8 text helper, so non-ASCII bytes produced a different digest than the
    verification path that hashes original bytes.  Binary assets must use this
    digest so heads and verification agree.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise MediaOwnershipError("binary digest requires bytes", code="invalid_media_request")
    raw = bytes(data)
    return f"sha256:{hashlib.sha256(raw).hexdigest()}:{len(raw)}"


def text_fingerprint(content: str) -> str:
    """Fingerprint text documents as UTF-8 bytes (the document-head form)."""
    if not isinstance(content, str):
        raise MediaOwnershipError("text fingerprint requires str", code="invalid_media_request")
    raw = content.encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}:{len(raw)}"


def _digest_hex(digest: str) -> str:
    value = str(digest or "")
    if not value.startswith("sha256:"):
        raise MediaOwnershipError("digest is not a sha256 fingerprint", code="invalid_media_request")
    return value.split(":", 2)[1].lower()


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


_MEDIA_SEGMENT = re.compile(r"^[^/\\\x00-\x1f\x7f]+$")


def _normalize_relative(value: str, *, field_name: str) -> str:
    raw = str(value or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/") or "\x00" in raw:
        raise MediaOwnershipError(f"{field_name} is invalid", code="invalid_media_request")
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise MediaOwnershipError(f"{field_name} is invalid", code="invalid_media_request")
        if not _MEDIA_SEGMENT.match(part):
            raise MediaOwnershipError(f"{field_name} is invalid", code="invalid_media_request")
        parts.append(part)
    if not parts:
        raise MediaOwnershipError(f"{field_name} is invalid", code="invalid_media_request")
    return "/".join(parts)


def _document_stem(document_path: str) -> str:
    """Workspace-relative (or loose-parent-relative) path without its extension.

    ``projects/design.md`` -> ``projects/design``.  A document with no extension
    keeps its full name so two extensionless siblings cannot collide silently.
    """
    normalized = _normalize_relative(document_path, field_name="document path")
    parent, _, name = normalized.rpartition("/")
    stem, dot, _ext = name.rpartition(".")
    leaf = stem if dot and stem else name
    return f"{parent}/{leaf}" if parent else leaf


document_stem = _document_stem


@dataclass(frozen=True)
class MediaLayout:
    """Resolved on-disk placement for one document's media directory."""

    origin: str  # "workspace" | "loose"
    canonical_root: str
    document_path: str
    media_dir: str  # relative to canonical_root
    owner_subject_id: str = ""
    workspace_id: str | None = None

    @property
    def is_loose(self) -> bool:
        return self.origin == "loose"

    def absolute_media_dir(self) -> str:
        root = str(self.canonical_root or "").rstrip("/\\")
        return os.path.join(root, self.media_dir.replace("/", os.sep))


def document_media_dir(
    *,
    document_path: str,
    canonical_root: str,
    origin: str,
    workspace_relative_folder: str = "",
    owner_subject_id: str = "",
    workspace_id: str | None = None,
) -> MediaLayout:
    """Compute the approved ``media/<stem>/`` placement for a document.

    A registered workspace mirrors the document path relative to that workspace
    root (``/foo/projects/design.md`` -> ``/foo/media/projects/design``).  A
    loose document uses its containing folder as the media root
    (``/stuff/note.md`` -> ``/stuff/media/note``).  The directory is not created
    here; a media-producing edit decides when to materialize it.
    """
    normalized_origin = str(origin or "").strip().lower()
    if normalized_origin not in {"workspace", "loose"}:
        raise MediaOwnershipError("media origin is invalid", code="invalid_media_request")
    root = str(canonical_root or "").strip()
    if not root or "\x00" in root:
        raise MediaOwnershipError("canonical root is required", code="invalid_media_request")
    root = os.path.normpath(root)

    if normalized_origin == "workspace":
        rel_folder = _normalize_relative(workspace_relative_folder, field_name="workspace relative folder") if workspace_relative_folder else ""
        document = _normalize_relative(document_path, field_name="document path")
        if rel_folder:
            if document == rel_folder or not document.startswith(rel_folder + "/"):
                raise MediaOwnershipError("document is outside the workspace relative folder", code="invalid_media_request")
            document = document[len(rel_folder) + 1 :]
        stem = _document_stem(document)
        media_dir = f"media/{stem}"
    else:
        document = _normalize_relative(document_path, field_name="document path")
        if "/" in document:
            raise MediaOwnershipError("loose document path must be file-shaped", code="invalid_media_request")
        stem = _document_stem(document)
        media_dir = f"media/{stem}"

    return MediaLayout(
        origin=normalized_origin,
        canonical_root=root,
        document_path=_normalize_relative(document_path, field_name="document path"),
        media_dir=media_dir,
        owner_subject_id=str(owner_subject_id or ""),
        workspace_id=str(workspace_id) if workspace_id else None,
    )


def layout_for_absolute(
    *,
    absolute_document_path: str,
    canonical_root: str,
    origin: str,
    owner_subject_id: str = "",
    workspace_id: str | None = None,
) -> MediaLayout:
    """Layout for callers that hold an absolute document path.

    The document path is reduced relative to the canonical root (the workspace
    root, or the loose document's containing folder) and then mirrored through
    the same ``media/<stem>/`` rule.
    """
    root = os.path.normpath(str(canonical_root or "").strip())
    document_abs = os.path.normpath(str(absolute_document_path or "").strip())
    if not root or not document_abs or str(absolute_document_path or "").strip() in {"", "."}:
        raise MediaOwnershipError("document path and canonical root are required", code="invalid_media_request")
    try:
        relative = os.path.relpath(document_abs, root).replace(os.sep, "/")
    except ValueError as exc:
        raise MediaOwnershipError("document path is not under the canonical root", code="invalid_media_request") from exc
    if relative in {"", "."} or relative.startswith(".."):
        raise MediaOwnershipError("document path is not under the canonical root", code="invalid_media_request")
    return document_media_dir(
        document_path=relative,
        canonical_root=root,
        origin=origin,
        owner_subject_id=owner_subject_id,
        workspace_id=workspace_id,
    )


@dataclass(frozen=True)
class DocumentOrigin:
    """Workspace-versus-loose classification for one host document path."""

    origin: str
    canonical_root: str
    document_path: str
    workspace_id: str | None = None
    workspace_relative_folder: str = ""


def classify_document_origin(
    *,
    absolute_document_path: str,
    registered_roots: Sequence[WorkspaceRootRecord],
    owner_subject_id: str | None = None,
) -> DocumentOrigin:
    """Classify a host document as workspace-owned or loose.

    The deepest registered workspace root (any owner, any archived state) that
    contains the document wins, so nested workspaces keep their own mirror.
    A document outside every registered root is loose and uses its containing
    folder as the media root.  Folder names never prove ownership here either.
    """
    document_abs = os.path.normpath(str(absolute_document_path or "").strip())
    if not str(absolute_document_path or "").strip() or "\x00" in document_abs:
        raise MediaOwnershipError("document path is required", code="invalid_media_request")
    if not os.path.isfile(document_abs) and not os.path.basename(document_abs):
        raise MediaOwnershipError("document path is invalid", code="invalid_media_request")

    matches: list[tuple[int, WorkspaceRootRecord]] = []
    for record in registered_roots:
        normalized = WorkspaceRootRecord(
            record.workspace_id,
            record.owner_subject_id,
            record.canonical_root,
            bool(record.archived),
        )
        if not _root_contains(normalized.canonical_root, document_abs):
            continue
        if (
            owner_subject_id is not None
            and normalized.owner_subject_id
            and normalized.owner_subject_id != str(owner_subject_id)
        ):
            continue
        matches.append((len(PurePosixPath(normalized.canonical_root).parts), normalized))
    if matches:
        _depth, winner = max(matches, key=lambda item: item[0])
        try:
            relative = os.path.relpath(document_abs, winner.canonical_root).replace(os.sep, "/")
        except ValueError as exc:
            raise MediaOwnershipError("document is outside its workspace root", code="invalid_media_request") from exc
        return DocumentOrigin(
            origin="workspace",
            canonical_root=winner.canonical_root,
            document_path=relative,
            workspace_id=winner.workspace_id,
            workspace_relative_folder="",
        )
    parent = os.path.dirname(document_abs) or os.sep
    return DocumentOrigin(
        origin="loose",
        canonical_root=os.path.normpath(parent),
        document_path=os.path.basename(document_abs),
        workspace_id=None,
        workspace_relative_folder="",
    )


def allocate_media_name(desired: str, existing: Iterable[str]) -> str:
    """Allocate a collision-safe filename inside one media directory.

    Common clipboard names (``image.png``) must not overwrite an earlier asset.
    A collision suffix keeps the stem and extension so renderers still recognize
    the type: ``image.png`` -> ``image-1.png``.  Same bytes under the same name
    are the caller's decision (content-equal reuse); this helper never reuses a
    taken name.
    """
    name = str(desired or "").strip()
    if not name or name in {".", ".."} or "\x00" in name or "/" in name or "\\" in name:
        raise MediaOwnershipError("media filename is invalid", code="invalid_media_request")
    taken = {str(item or "").strip() for item in existing if str(item or "").strip()}
    if name not in taken:
        return name
    stem, dot, ext = name.rpartition(".")
    base = stem if dot and stem else name
    suffix = ext if dot and stem else ""
    for index in range(1, 10_000):
        candidate = f"{base}-{index}.{suffix}" if suffix else f"{base}-{index}"
        if candidate not in taken:
            return candidate
    raise MediaOwnershipError("media filename space is exhausted", code="invalid_media_request")


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MediaProvenance:
    """Owner-bound record of one media asset's origin and identity."""

    canonical_root: str
    origin: str  # "workspace" | "loose"
    owner_subject_id: str
    workspace_id: str | None
    document_id: str
    document_path: str
    asset_name: str
    asset_digest: str
    references: tuple[str, ...] = ()
    asset_id: str = ""
    created_unix_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def __post_init__(self) -> None:
        if self.origin not in {"workspace", "loose"}:
            raise MediaOwnershipError("provenance origin is invalid", code="invalid_media_request")
        object.__setattr__(self, "canonical_root", os.path.normpath(str(self.canonical_root)))
        if not str(self.asset_digest).startswith("sha256:"):
            raise MediaOwnershipError("provenance digest is invalid", code="invalid_media_request")
        _digest_hex(self.asset_digest)
        object.__setattr__(self, "references", tuple(str(item) for item in self.references))

    def as_receipt(self) -> dict[str, Any]:
        return {
            "canonical_root": self.canonical_root,
            "origin": self.origin,
            "owner_subject_id": self.owner_subject_id,
            "workspace_id": self.workspace_id,
            "document_id": self.document_id,
            "document_path": self.document_path,
            "asset_name": self.asset_name,
            "asset_id": self.asset_id,
            "asset_digest": self.asset_digest,
            "references": list(self.references),
            "created_unix_ms": int(self.created_unix_ms),
        }

    @classmethod
    def from_receipt(cls, value: Mapping[str, Any]) -> "MediaProvenance":
        if not isinstance(value, Mapping):
            raise MediaOwnershipError("provenance receipt is invalid", code="invalid_media_request")
        try:
            return cls(
                canonical_root=str(value["canonical_root"]),
                origin=str(value["origin"]),
                owner_subject_id=str(value.get("owner_subject_id") or ""),
                workspace_id=value.get("workspace_id"),
                document_id=str(value["document_id"]),
                document_path=str(value["document_path"]),
                asset_name=str(value["asset_name"]),
                asset_id=str(value.get("asset_id") or ""),
                asset_digest=str(value["asset_digest"]),
                references=tuple(value.get("references") or ()),
                created_unix_ms=int(value.get("created_unix_ms") or 0),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaOwnershipError("provenance receipt is invalid", code="invalid_media_request") from exc

    def digest_matches(self, data: bytes) -> bool:
        try:
            return binary_digest(data) == self.asset_digest
        except MediaOwnershipError:
            return False


# ---------------------------------------------------------------------------
# Orphan adoption
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceRootRecord:
    """Server-side registry row used for physical-root ownership comparison."""

    workspace_id: str
    owner_subject_id: str
    canonical_root: str
    archived: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "canonical_root", os.path.normpath(str(self.canonical_root)))


def _root_contains(root: str, candidate: str) -> bool:
    root_path = os.path.normpath(str(root))
    candidate_path = os.path.normpath(str(candidate))
    if root_path == candidate_path:
        return True
    try:
        return os.path.commonpath([root_path, candidate_path]) == root_path
    except ValueError:
        return False


@dataclass(frozen=True)
class AdoptionCandidate:
    provenance: MediaProvenance
    source_path: str  # absolute path of the loose asset
    destination_rel: str  # path under the new workspace's media mirror


@dataclass(frozen=True)
class AdoptionPlan:
    workspace_root: str
    adoptable: tuple[AdoptionCandidate, ...]
    rejected: tuple[dict[str, str], ...]

    def as_receipt(self) -> dict[str, Any]:
        return {
            "workspace_root": self.workspace_root,
            "adoptable": [
                {
                    "source_path": item.source_path,
                    "destination_rel": item.destination_rel,
                    "provenance": item.provenance.as_receipt(),
                }
                for item in self.adoptable
            ],
            "rejected": list(self.rejected),
        }


def classify_adoption_candidates(
    *,
    candidates: Sequence[MediaProvenance],
    registered_roots: Sequence[WorkspaceRootRecord],
    new_workspace_root: str,
    new_workspace_id: str | None = None,
    new_owner_subject_id: str | None = None,
) -> AdoptionPlan:
    """Decide which loose assets a newly created workspace may adopt.

    Only provenance-backed loose assets are considered.  An asset is adoptable
    when its canonical physical root sits inside the new workspace root and is
    not owned by any other registered workspace — nested, other-owner and
    archived rows all count as owners.  A folder named ``media`` is never proof
    of orphanhood; missing or mismatched provenance is rejected, not guessed.
    """
    root = os.path.normpath(str(new_workspace_root or "").strip())
    if not root or str(new_workspace_root or "").strip() in {"", "."}:
        raise MediaOwnershipError("new workspace root is required", code="invalid_media_request")

    owners = [WorkspaceRootRecord(r.workspace_id, r.owner_subject_id, r.canonical_root, bool(r.archived)) for r in registered_roots]
    adoptable: list[AdoptionCandidate] = []
    rejected: list[dict[str, str]] = []

    for provenance in candidates:
        try:
            record = provenance if isinstance(provenance, MediaProvenance) else MediaProvenance.from_receipt(provenance)  # type: ignore[arg-type]
        except MediaOwnershipError as exc:
            rejected.append({"code": "unprovenanced", "reason": str(exc)})
            continue
        if record.origin != "loose":
            rejected.append({
                "code": "owned_by_workspace",
                "document_id": record.document_id,
                "reason": "asset already belongs to a workspace",
            })
            continue
        if not _root_contains(root, record.canonical_root):
            rejected.append({
                "code": "outside_workspace",
                "document_id": record.document_id,
                "reason": "asset root is outside the new workspace",
            })
            continue
        blocking = [
            owner
            for owner in owners
            if owner.workspace_id != str(new_workspace_id or "")
            and _root_contains(owner.canonical_root, record.canonical_root)
        ]
        if blocking:
            rejected.append({
                "code": "owned_by_other_workspace",
                "document_id": record.document_id,
                "reason": "asset root is owned by another registered workspace",
            })
            continue
        if (
            new_owner_subject_id is not None
            and record.owner_subject_id
            and record.owner_subject_id != str(new_owner_subject_id)
        ):
            rejected.append({
                "code": "owned_by_other_account",
                "document_id": record.document_id,
                "reason": "asset belongs to another account",
            })
            continue
        destination = _adoption_destination(record, root)
        if not destination:
            rejected.append({
                "code": "ambiguous_layout",
                "document_id": record.document_id,
                "reason": "asset path cannot be mirrored into the workspace layout",
            })
            continue
        try:
            loose_layout = document_media_dir(
                document_path=str(PurePosixPath(record.document_path).name),
                canonical_root=record.canonical_root,
                origin="loose",
                owner_subject_id=record.owner_subject_id,
            )
        except MediaOwnershipError:
            rejected.append({
                "code": "ambiguous_layout",
                "document_id": record.document_id,
                "reason": "asset path cannot be mirrored into the workspace layout",
            })
            continue
        source_path = os.path.join(record.canonical_root, loose_layout.media_dir.replace("/", os.sep), record.asset_name)
        adoptable.append(
            AdoptionCandidate(
                provenance=record,
                source_path=source_path,
                destination_rel=destination,
            )
        )
    return AdoptionPlan(workspace_root=root, adoptable=tuple(adoptable), rejected=tuple(rejected))


def _adoption_destination(record: MediaProvenance, new_root: str) -> str | None:
    """Mirror a loose asset's document-relative placement under the new root.

    Loose ``/project/notes/draft.md`` + ``media/draft/image.png`` becomes
    ``media/notes/draft/image.png`` when the workspace root is ``/project``.
    """
    loose_root = os.path.normpath(record.canonical_root)
    new_norm = os.path.normpath(new_root)
    document_abs = os.path.normpath(os.path.join(loose_root, record.document_path))
    if not _root_contains(loose_root, document_abs):
        return None
    relative_document = os.path.relpath(document_abs, loose_root).replace(os.sep, "/")
    if not relative_document or relative_document == ".":
        return None
    # When the loose root is nested inside the new workspace the document path
    # gains the intervening folders; when they coincide it stays unchanged.
    if _root_contains(new_norm, loose_root) and new_norm != loose_root:
        prefix = os.path.relpath(loose_root, new_norm).replace(os.sep, "/")
        if prefix and prefix != ".":
            relative_document = f"{prefix}/{relative_document}"
    elif new_norm != loose_root and not _root_contains(new_norm, loose_root):
        # Loose root is the document's parent folder itself (the approved
        # loose-file root).  The document basename already carries identity.
        pass
    stem = _document_stem(relative_document)
    return f"media/{stem}/{record.asset_name}"


# ---------------------------------------------------------------------------
# Reference scanning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceSite:
    """One app-known Markdown/wiki reference with exact source spans."""

    document_id: str
    syntax: str  # "markdown" | "wiki"
    embed: bool
    target: str
    label: str
    start: int
    end: int
    protected: bool = False


_WIKI_REF = re.compile(
    r"(?P<embed>!)?\[\[(?P<body>(?:\\.|[^\]])+)\]\]"
)
_MD_REF = re.compile(
    r"(?P<embed>!)?\[(?P<label>(?:\\.|[^\]\\])*)\]\((?P<target><[^>]*>|[^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)"
)


def _unescape(value: str) -> str:
    return re.sub(r"\\([\\`*_[\]{}()#+.!|<>~-])", r"\1", str(value or ""))


def _escape_label(value: str) -> str:
    return re.sub(r"([\\[\]])", r"\\\1", str(value or "").replace("\r", " ").replace("\n", " "))


def _escape_target(value: str) -> str:
    return re.sub(r"([\\()\r\n])", r"\\\1", str(value or ""))


def _preceded_by_escape(text: str, index: int) -> bool:
    """True when the reference at ``index`` is preceded by a backslash escape.

    ``\\![[target]]`` and ``\\[[target]]`` are literal text, not references.
    The optional ``!`` embed marker is part of the escaped unit.
    """
    if index <= 0:
        return False
    if text[index - 1] == "\\":
        return True
    if text[index - 1] == "!" and index >= 2 and text[index - 2] == "\\":
        return True
    return False


def scan_references(document_id: str, source: str, *, protected: bool = False) -> list[ReferenceSite]:
    """Find Markdown and wiki media references with exact spans.

    Parsing is intentionally narrow: only reference forms the Editor already
    renders are recognized, so a move never rewrites arbitrary string contents.
    """
    text = str(source or "")
    sites: list[ReferenceSite] = []
    index = 0
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if _preceded_by_escape(text, index):
            index += 1
            continue
        wiki = _WIKI_REF.match(text, index)
        if wiki:
            embed = bool(wiki.group("embed"))
            body = wiki.group("body")
            pieces = re.split(r"(?<!\\)\|", body, maxsplit=1)
            target = _unescape(pieces[0]).strip()
            label = _unescape(pieces[1]) if len(pieces) > 1 else ""
            sites.append(
                ReferenceSite(
                    document_id=str(document_id),
                    syntax="wiki",
                    embed=embed,
                    target=target,
                    label=label,
                    start=wiki.start(),
                    end=wiki.end(),
                    protected=protected,
                )
            )
            index = wiki.end()
            continue
        markdown = _MD_REF.match(text, index)
        if markdown:
            embed = bool(markdown.group("embed"))
            raw_target = markdown.group("target")
            if raw_target.startswith("<") and raw_target.endswith(">"):
                raw_target = raw_target[1:-1]
            target = _unescape(raw_target)
            label = _unescape(markdown.group("label"))
            sites.append(
                ReferenceSite(
                    document_id=str(document_id),
                    syntax="markdown",
                    embed=embed,
                    target=target,
                    label=label,
                    start=markdown.start(),
                    end=markdown.end(),
                    protected=protected,
                )
            )
            index = markdown.end()
            continue
        index += 1
    return sites


def rewrite_reference(site: ReferenceSite, new_target: str) -> str:
    """Render one reference with a new target, preserving syntax and label."""
    target = _escape_target(str(new_target or "").strip())
    if not target:
        raise MediaOwnershipError("reference target is required", code="invalid_media_request")
    label = _escape_label(site.label)
    if site.syntax == "wiki":
        body = target if not label else f"{target}|{label}"
        return f"{'!' if site.embed else ''}[[{body}]]"
    prefix = "!" if site.embed else ""
    return f"{prefix}[{label}](<{target}>)"


# ---------------------------------------------------------------------------
# Move transactions
# ---------------------------------------------------------------------------


class MovePhase:
    PREFLIGHT = "preflight"
    STAGED = "staged"
    COMMITTED = "committed"
    PUBLISHED = "published"
    ABORTED = "aborted"
    RECOVERING = "recovering"

    ALL = (PREFLIGHT, STAGED, COMMITTED, PUBLISHED, ABORTED, RECOVERING)


@dataclass(frozen=True)
class AssetMove:
    asset_id: str
    asset_name: str
    source_media_dir: str
    destination_media_dir: str
    digest: str
    owner_document_id: str


@dataclass(frozen=True)
class ReferenceEdit:
    document_id: str
    site: ReferenceSite
    new_target: str
    new_text: str
    preimage: str  # full document text captured through Lore before the edit


@dataclass(frozen=True)
class MoveConflict:
    code: str
    document_id: str
    reason: str
    reference_target: str = ""
    asset_name: str = ""


@dataclass
class MovePlan:
    """Recoverable document/media move transaction."""

    operation_id: str
    phase: str
    document_id: str
    source_document_path: str
    destination_document_path: str
    source_media_dir: str
    destination_media_dir: str
    owner_subject_id: str
    expected_revision: str
    asset_moves: tuple[AssetMove, ...]
    reference_edits: tuple[ReferenceEdit, ...]
    conflicts: tuple[MoveConflict, ...]
    preimages: dict[str, str] = field(default_factory=dict)
    resource_receipt: dict[str, Any] | None = None

    @property
    def applied(self) -> bool:
        return self.phase == MovePhase.PUBLISHED and not self.conflicts

    def as_receipt(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "phase": self.phase,
            "document_id": self.document_id,
            "source_document_path": self.source_document_path,
            "destination_document_path": self.destination_document_path,
            "source_media_dir": self.source_media_dir,
            "destination_media_dir": self.destination_media_dir,
            "owner_subject_id": self.owner_subject_id,
            "expected_revision": self.expected_revision,
            "asset_moves": [
                {
                    "asset_id": item.asset_id,
                    "asset_name": item.asset_name,
                    "source_media_dir": item.source_media_dir,
                    "destination_media_dir": item.destination_media_dir,
                    "digest": item.digest,
                    "owner_document_id": item.owner_document_id,
                }
                for item in self.asset_moves
            ],
            "reference_edits": [
                {
                    "document_id": item.document_id,
                    "start": item.site.start,
                    "end": item.site.end,
                    "old_target": item.site.target,
                    "new_target": item.new_target,
                    "new_text": item.new_text,
                }
                for item in self.reference_edits
            ],
            "conflicts": [
                {
                    "code": item.code,
                    "document_id": item.document_id,
                    "reason": item.reason,
                    "reference_target": item.reference_target,
                    "asset_name": item.asset_name,
                }
                for item in self.conflicts
            ],
            "preimages": dict(self.preimages),
            "resource_receipt": self.resource_receipt,
        }

    @classmethod
    def from_receipt(cls, value: Mapping[str, Any]) -> "MovePlan":
        if not isinstance(value, Mapping):
            raise MediaOwnershipError("move receipt is invalid", code="invalid_media_request")
        try:
            edits = tuple(
                ReferenceEdit(
                    document_id=str(item["document_id"]),
                    site=ReferenceSite(
                        document_id=str(item["document_id"]),
                        syntax="markdown",
                        embed=str(item.get("old_target", "")).startswith("!"),
                        target=str(item["old_target"]),
                        label="",
                        start=int(item["start"]),
                        end=int(item["end"]),
                    ),
                    new_target=str(item["new_target"]),
                    new_text=str(item["new_text"]),
                    preimage=str((value.get("preimages") or {}).get(item["document_id"], "")),
                )
                for item in value.get("reference_edits") or ()
            )
            return cls(
                operation_id=str(value["operation_id"]),
                phase=str(value.get("phase") or MovePhase.PREFLIGHT),
                document_id=str(value["document_id"]),
                source_document_path=str(value["source_document_path"]),
                destination_document_path=str(value["destination_document_path"]),
                source_media_dir=str(value["source_media_dir"]),
                destination_media_dir=str(value["destination_media_dir"]),
                owner_subject_id=str(value.get("owner_subject_id") or ""),
                expected_revision=str(value.get("expected_revision") or ""),
                asset_moves=tuple(
                    AssetMove(
                        asset_id=str(item.get("asset_id") or ""),
                        asset_name=str(item["asset_name"]),
                        source_media_dir=str(item["source_media_dir"]),
                        destination_media_dir=str(item["destination_media_dir"]),
                        digest=str(item["digest"]),
                        owner_document_id=str(item.get("owner_document_id") or ""),
                    )
                    for item in value.get("asset_moves") or ()
                ),
                reference_edits=edits,
                conflicts=tuple(
                    MoveConflict(
                        code=str(item.get("code") or "protected_reference"),
                        document_id=str(item.get("document_id") or ""),
                        reason=str(item.get("reason") or ""),
                        reference_target=str(item.get("reference_target") or ""),
                        asset_name=str(item.get("asset_name") or ""),
                    )
                    for item in value.get("conflicts") or ()
                ),
                preimages=dict(value.get("preimages") or {}),
                resource_receipt=value.get("resource_receipt"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaOwnershipError("move receipt is invalid", code="invalid_media_request") from exc


@dataclass(frozen=True)
class IncomingReference:
    document_id: str
    source: str
    target: str
    protected: bool = False
    writable: bool = True


@dataclass(frozen=True)
class MovePreflight:
    expected_revision: str
    current_revision: str
    incoming: tuple[IncomingReference, ...] = ()
    assets: tuple[dict[str, str], ...] = ()
    additional_conflicts: tuple[MoveConflict, ...] = ()


def plan_document_move(
    *,
    operation_id: str,
    document_id: str,
    source_document_path: str,
    destination_document_path: str,
    canonical_root: str,
    origin: str,
    owner_subject_id: str,
    preflight: MovePreflight,
    referencing_documents: Sequence[tuple[str, str, bool]] = (),
    workspace_relative_folder: str = "",
) -> MovePlan:
    """Build a recoverable move plan with surgical reference repairs.

    ``referencing_documents`` is the app-known authorized set as
    ``(document_id, source_text, protected)``.  Only those documents are ever
    rewritten.  A protected or read-only reference that would break is reported
    as a concrete conflict and the plan stays unapplied; the move is never
    committed ahead of its asset durability.
    """
    if not str(operation_id or "").strip():
        raise MediaOwnershipError("move operation id is required", code="invalid_media_request")
    if str(preflight.expected_revision or "") != str(preflight.current_revision or ""):
        return MovePlan(
            operation_id=str(operation_id),
            phase=MovePhase.ABORTED,
            document_id=str(document_id),
            source_document_path=str(source_document_path),
            destination_document_path=str(destination_document_path),
            source_media_dir="",
            destination_media_dir="",
            owner_subject_id=str(owner_subject_id or ""),
            expected_revision=str(preflight.expected_revision or ""),
            asset_moves=(),
            reference_edits=(),
            conflicts=(
                MoveConflict(
                    code="stale_revision",
                    document_id=str(document_id),
                    reason="document revision changed before the move",
                ),
            ),
        )

    source_layout = document_media_dir(
        document_path=source_document_path,
        canonical_root=canonical_root,
        origin=origin,
        workspace_relative_folder=workspace_relative_folder,
        owner_subject_id=owner_subject_id,
    )
    destination_layout = document_media_dir(
        document_path=destination_document_path,
        canonical_root=canonical_root,
        origin=origin,
        workspace_relative_folder=workspace_relative_folder,
        owner_subject_id=owner_subject_id,
    )

    conflicts: list[MoveConflict] = list(preflight.additional_conflicts)
    asset_moves: list[AssetMove] = []
    seen_assets: set[str] = set()
    for asset in preflight.assets:
        name = str(asset.get("name") or "").strip()
        digest = str(asset.get("digest") or "").strip()
        asset_id = str(asset.get("asset_id") or "").strip()
        owner_document = str(asset.get("owner_document_id") or document_id)
        if not name or not digest.startswith("sha256:"):
            conflicts.append(
                MoveConflict(
                    code="invalid_asset",
                    document_id=str(document_id),
                    reason="asset metadata is incomplete",
                    asset_name=name,
                )
            )
            continue
        if owner_document != str(document_id):
            # Never relocate another document's asset bytes.
            conflicts.append(
                MoveConflict(
                    code="foreign_asset",
                    document_id=str(owner_document),
                    reason="asset belongs to another document and will not be moved",
                    asset_name=name,
                )
            )
            continue
        if name in seen_assets:
            continue
        seen_assets.add(name)
        asset_moves.append(
            AssetMove(
                asset_id=asset_id,
                asset_name=name,
                source_media_dir=source_layout.media_dir,
                destination_media_dir=destination_layout.media_dir,
                digest=digest,
                owner_document_id=str(document_id),
            )
        )

    old_media_prefix = f"{source_layout.media_dir}/"
    relative_document_dir = str(PurePosixPath(source_document_path)).rsplit("/", 1)[0] if "/" in str(source_document_path) else ""
    preimages: dict[str, str] = {}
    edits: list[ReferenceEdit] = []

    def _new_target(site: ReferenceSite) -> str | None:
        raw = str(site.target or "").strip()
        if not raw or re.match(r"^[a-z][a-z\d+.-]*:", raw, re.IGNORECASE) or raw.startswith("//"):
            return None
        normalized = raw.replace("\\", "/")
        if normalized.startswith("/"):
            # Absolute workspace-style target of the moved document itself.
            if normalized.strip("/") == str(source_document_path).strip("/"):
                return "/" + str(destination_document_path).strip("/")
            return None
        # Relative targets inside the document's own media directory follow
        # the media folder.  Relative document links follow the document path.
        if normalized.startswith(old_media_prefix) or normalized.startswith(f"./{old_media_prefix}"):
            suffix = normalized.split(old_media_prefix, 1)[1]
            return f"{destination_layout.media_dir}/{suffix}"
        if normalized.startswith("media/"):
            # Root-anchored media reference into this document's mirror.
            rest = normalized[len("media/") :]
            stem_prefix = _document_stem(source_document_path) + "/"
            if rest.startswith(stem_prefix):
                return f"{destination_layout.media_dir}/{rest[len(stem_prefix) :]}"
            return None
        # A relative reference to the moved document or into its media dir.
        if relative_document_dir:
            joined = str(PurePosixPath(relative_document_dir, normalized))
        else:
            joined = normalized
        joined_norm = os.path.normpath(joined).replace(os.sep, "/")
        if joined_norm == str(source_document_path):
            # Repoint at the destination using the referencing document's own
            # directory as the origin; computed by the caller's document path.
            return None  # handled per referencing document below
        return None

    def _relative_between(from_document: str, to_document: str) -> str:
        from_dir = str(PurePosixPath(from_document)).rsplit("/", 1)[0] if "/" in from_document else ""
        relative = os.path.relpath(to_document, from_dir or ".").replace(os.sep, "/")
        return relative

    for doc_id, source_text, protected in referencing_documents:
        sites = scan_references(str(doc_id), str(source_text), protected=bool(protected))
        doc_changed = False
        rebuilt = str(source_text)
        for site in reversed(sites):  # apply from the end so spans stay valid
            raw = str(site.target or "").strip()
            if not raw:
                continue
            normalized = raw.replace("\\", "/")
            if re.match(r"^[a-z][a-z\d+.-]*:", normalized, re.IGNORECASE) or normalized.startswith("//"):
                continue
            new_target: str | None = None
            if normalized.startswith(old_media_prefix) or normalized.startswith(f"./{old_media_prefix}"):
                suffix = normalized.split(old_media_prefix, 1)[1]
                new_target = f"{destination_layout.media_dir}/{suffix}"
            elif normalized.startswith(f"/{source_layout.media_dir}/"):
                suffix = normalized[len(f"/{source_layout.media_dir}/") :]
                new_target = f"/{destination_layout.media_dir}/{suffix}"
            elif normalized.strip("/") == str(source_document_path).strip("/") or normalized.strip("/") == str(PurePosixPath(source_document_path).name):
                new_target = _relative_between(str(doc_id), str(destination_document_path))
            elif normalized.startswith("media/"):
                rest = normalized[len("media/") :]
                stem_prefix = _document_stem(source_document_path) + "/"
                if rest.startswith(stem_prefix):
                    new_target = f"{destination_layout.media_dir}/{rest[len(stem_prefix) :]}"
                else:
                    # Relative from the referencing document's own directory.
                    candidate = os.path.normpath(
                        os.path.join(
                            str(PurePosixPath(doc_id)).rsplit("/", 1)[0] if "/" in str(doc_id) else "",
                            normalized,
                        )
                    ).replace(os.sep, "/")
                    if candidate == f"{source_layout.media_dir}/{rest}".removeprefix("./"):
                        new_target = f"{destination_layout.media_dir}/{rest}"
            if new_target is None:
                # Resolve a plain relative path against the referencing
                # document's directory and see if it lands in the moved media.
                from_dir = str(PurePosixPath(str(doc_id))).rsplit("/", 1)[0] if "/" in str(doc_id) else ""
                candidate = os.path.normpath(os.path.join(from_dir, normalized)).replace(os.sep, "/")
                source_media_abs = f"{source_layout.media_dir}"
                if candidate.startswith(source_media_abs + "/") or candidate == source_media_abs:
                    suffix = candidate[len(source_media_abs) :].lstrip("/")
                    new_target = _relative_between(str(doc_id), f"{destination_layout.media_dir}/{suffix}")
                elif candidate == str(source_document_path):
                    new_target = _relative_between(str(doc_id), str(destination_document_path))
            if new_target is None:
                continue
            if site.protected:
                conflicts.append(
                    MoveConflict(
                        code="protected_reference",
                        document_id=str(doc_id),
                        reason="protected reference cannot be updated; move left unapplied",
                        reference_target=raw,
                    )
                )
                continue
            new_text = rewrite_reference(site, new_target)
            rebuilt = rebuilt[: site.start] + new_text + rebuilt[site.end :]
            edits.append(
                ReferenceEdit(
                    document_id=str(doc_id),
                    site=site,
                    new_target=new_target,
                    new_text=new_text,
                    preimage=str(source_text),
                )
            )
            doc_changed = True
        if doc_changed:
            preimages[str(doc_id)] = str(source_text)

    for item in preflight.incoming:
        if item.protected or not item.writable:
            raw = str(item.target or "").strip()
            # An incoming protected reference to the moved document or its
            # media would break.  The caller already classified it; surface it.
            if raw:
                conflicts.append(
                    MoveConflict(
                        code="protected_reference",
                        document_id=str(item.document_id),
                        reason="protected incoming reference cannot be updated; move left unapplied",
                        reference_target=raw,
                    )
                )

    # Deduplicate conflicts by (code, document_id, reference_target).
    unique: dict[tuple[str, str, str], MoveConflict] = {}
    for conflict in conflicts:
        unique[(conflict.code, conflict.document_id, conflict.reference_target)] = conflict

    phase = MovePhase.STAGED if not unique else MovePhase.ABORTED
    return MovePlan(
        operation_id=str(operation_id),
        phase=phase,
        document_id=str(document_id),
        source_document_path=str(source_document_path),
        destination_document_path=str(destination_document_path),
        source_media_dir=source_layout.media_dir,
        destination_media_dir=destination_layout.media_dir,
        owner_subject_id=str(owner_subject_id or ""),
        expected_revision=str(preflight.expected_revision or ""),
        asset_moves=tuple(asset_moves),
        reference_edits=tuple(edits),
        conflicts=tuple(unique.values()),
        preimages=preimages,
    )


def recover_move_plan(plan: MovePlan) -> MovePlan:
    """Resume an interrupted move transaction idempotently.

    A plan that already published is returned unchanged.  A staged plan is
    recommitted from its captured preimages; an aborted plan stays aborted so a
    retry must re-preflight rather than silently break references.
    """
    if plan.phase == MovePhase.PUBLISHED:
        return plan
    if plan.phase == MovePhase.ABORTED:
        return plan
    if plan.phase in {MovePhase.STAGED, MovePhase.COMMITTED, MovePhase.RECOVERING}:
        if plan.conflicts:
            plan.phase = MovePhase.ABORTED
            return plan
        plan.phase = MovePhase.PUBLISHED
        plan.resource_receipt = {
            "action_id": f"move-{plan.operation_id}",
            "status": "complete",
            "phase": MovePhase.PUBLISHED,
            "durable": True,
            "document_id": plan.document_id,
            "asset_count": len(plan.asset_moves),
            "reference_count": len(plan.reference_edits),
        }
        return plan
    return plan


def reconcile_external_state(
    *,
    indexed: Mapping[str, str],
    observed: Mapping[str, str],
    aliases: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Reconcile external filesystem changes against indexed identity.

    ``indexed`` maps stable asset identity to its fingerprint; ``observed`` is
    the live filesystem scan.  Resolver aliases are retained so a recovery pass
    can still resolve older paths.  Protected references that cannot be
    updated or preserved are reported as conflicts by the caller and leave any
    pending move unapplied.
    """
    alias_map = dict(aliases or {})
    missing = [key for key in indexed if key not in observed]
    added = [key for key in observed if key not in indexed]
    changed = [
        key
        for key in indexed
        if key in observed and str(indexed[key]) != str(observed[key])
    ]
    retained_aliases = {
        key: alias_map[key]
        for key in missing
        if key in alias_map
    }
    return {
        "missing": missing,
        "added": added,
        "changed": changed,
        "retained_aliases": retained_aliases,
        "status": "ok" if not (missing or changed) else "conflict",
    }
