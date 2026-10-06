# services/memory/skills.py
"""Skills storage layer.

Skills live on disk as `data/skills/<category>/<name>/SKILL.md` files with
YAML frontmatter and a structured markdown body (When to Use / Procedure /
Pitfalls / Verification). See `skill_format.py` for the format.

Runtime retrieval/use/outcome events live in the append-only shared
`data/skills/_usage_events.jsonl` stream keyed by immutable skill ID and
owner. Audit and necessity metadata remains in `_usage.json`, so SKILL.md
doesn't churn on every retrieval.

Ownership: skills declare `owner: <username>` in frontmatter. Single-user
deployments can leave that blank.

This module also retains a JSON fallback for any legacy `data/skills.json`
entries — they're surfaced as read-only `Skill` objects so old data still
loads while a user migrates them to disk.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import shutil
import sys
import tempfile
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from core.atomic_io import (
    AtomicFileChange,
    atomic_write_batch,
    file_fingerprint,
)
from .skill_format import Skill, slugify
from .skill_lifecycle import (
    PROMOTIONS_FILE,
    append_usage_event,
    current_bundle_sha256,
    ensure_revision,
    hydrate_identity,
    load_state,
    load_usage_events,
    locked,
    read_bundle_file,
    read_bundle_snapshot,
    read_snapshot,
    save_state,
    snapshot_bundle,
    _safe_bundle_path,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token / similarity helpers (kept for the relevance fallback)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> set:
    return {w.strip('.,!?";:()[]') for w in (text or "").lower().split() if len(w) > 1}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _to_float(x, default: float = 0.0) -> float:
    """Coerce a possibly hand-edited frontmatter value to float without
    raising — a blank or non-numeric `confidence:` in a SKILL.md must not
    blow up retrieval or eviction."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# SkillsManager
# ---------------------------------------------------------------------------


class SkillsManager:
    """Read/write SKILL.md files under <data_dir>/skills/."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.skills_root = os.path.join(data_dir, "skills")
        self.usage_file = os.path.join(self.skills_root, "_usage.json")
        self.promotions_file = os.path.join(self.skills_root, PROMOTIONS_FILE)
        self.legacy_file = os.path.join(data_dir, "skills.json")  # back-compat
        os.makedirs(self.skills_root, exist_ok=True)

    def _assert_memory_sources_available(
        self,
        owner: Optional[str],
        memory_ids: Iterable[object],
        source_uri: object = None,
    ) -> None:
        """Reject new lineage while those memories are being forgotten.

        Every caller holds the global PROMOTIONS lock, so the recovery
        manifests and the skill write form one serializable boundary.
        """
        selected = {
            str(value)
            for value in memory_ids
            if str(value or "")
        }
        source_uri = str(source_uri or "").strip()
        promotion_id = (
            source_uri.removeprefix("memory-promotion:")
            if source_uri.startswith("memory-promotion:")
            else ""
        )
        if not selected and not promotion_id:
            return
        recovery_root = os.path.join(
            os.path.abspath(self.data_dir),
            ".memory-forget-skills",
        )
        if os.path.islink(recovery_root):
            raise ValueError("memory lifecycle state is unsafe")
        if not os.path.isdir(recovery_root):
            return
        try:
            entries = list(os.scandir(recovery_root))
        except OSError as exc:
            raise ValueError("memory lifecycle state is unavailable") from exc
        for entry in entries:
            if entry.is_symlink():
                raise ValueError("memory lifecycle state is unsafe")
            if not entry.is_dir(follow_symlinks=False):
                continue
            try:
                with open(
                    os.path.join(entry.path, "manifest.json"),
                    encoding="utf-8",
                ) as handle:
                    manifest = json.load(handle)
            except (OSError, ValueError) as exc:
                raise ValueError("memory lifecycle state is unreadable") from exc
            if (
                not isinstance(manifest, dict)
                or manifest.get("owner") != (owner or "")
                or manifest.get("state")
                not in {
                    "preparing",
                    "prepared",
                    "committed",
                    "provider_restored",
                    "restoring",
                }
            ):
                continue
            blocked = {
                str(value)
                for value in manifest.get("memory_ids") or ()
                if str(value or "")
            }
            blocked_promotions = {
                str(row.get("id"))
                for row in manifest.get("promotions") or ()
                if isinstance(row, dict) and str(row.get("id") or "")
            }
            if selected & blocked or promotion_id in blocked_promotions:
                raise ValueError(
                    "memory-derived skill source is inside an unfinished "
                    "memory lifecycle operation"
                )

    # ----------------------------------------------------------------------
    # Path helpers
    # ----------------------------------------------------------------------

    def _skill_dir(self, category: str, name: str) -> str:
        cat = slugify(category or "general", fallback="general")
        nm = slugify(name, fallback="skill")
        return os.path.join(self.skills_root, cat, nm)

    def _skill_file(self, category: str, name: str) -> str:
        return os.path.join(self._skill_dir(category, name), "SKILL.md")

    def _reserved_skill_names(self) -> set[str]:
        names: set[str] = set()
        if not os.path.isdir(self.skills_root):
            return names
        with os.scandir(self.skills_root) as categories:
            for category in categories:
                if not category.is_dir(follow_symlinks=False):
                    continue
                with os.scandir(category.path) as skills:
                    names.update(
                        entry.name
                        for entry in skills
                        if entry.is_dir(follow_symlinks=False)
                    )
        return names

    # ----------------------------------------------------------------------
    # Usage sidecar
    # ----------------------------------------------------------------------

    def _load_usage(self) -> Dict[str, Dict]:
        if not os.path.exists(self.usage_file):
            return {}
        try:
            with open(self.usage_file, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save_usage(self, usage: Dict[str, Dict]) -> None:
        try:
            from core.atomic_io import atomic_write_json
            atomic_write_json(self.usage_file, usage, indent=2)
        except Exception:
            tmp = self.usage_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(usage, f, indent=2)
            os.replace(tmp, self.usage_file)

    @staticmethod
    def _usage_key(skill_id: str, owner: Optional[str] = None) -> str:
        return f"{owner}::{skill_id}" if owner else skill_id

    def _usage_entry(
        self,
        usage: Dict[str, Dict],
        skill_id: str,
        owner: Optional[str] = None,
        *,
        legacy_name: str = "",
    ) -> Dict:
        key = self._usage_key(skill_id, owner)
        entry = usage.get(key)
        if isinstance(entry, dict):
            return entry
        legacy = usage.get(self._usage_key(legacy_name, owner)) if legacy_name else None
        if isinstance(legacy, dict):
            return legacy
        return {}

    def _find_skill(self, skill_id: str, owner: Optional[str]) -> tuple[str, Skill] | None:
        for path in self._iter_skill_files():
            sk = self._read_skill(path)
            if not sk:
                continue
            if sk.name != skill_id and sk.skill_id != skill_id:
                continue
            if (sk.owner or "") != (owner or ""):
                continue
            return path, sk
        return None

    @staticmethod
    def _matches_locked_head(
        current: Optional[Skill],
        expected: Skill,
        requested: str,
        owner: Optional[str],
    ) -> bool:
        """Fail closed when a lookup races a rename, edit, or owner transfer."""
        return bool(
            current
            and (current.owner or "") == (owner or "")
            and current.skill_id == expected.skill_id
            and current.name == expected.name
            and current.revision == expected.revision
            and current.content_hash == expected.content_hash
            and requested in (current.name, current.skill_id)
        )

    def _event_counts(self) -> Dict[tuple[str, str], Dict[str, int | float | None]]:
        counts: Dict[tuple[str, str], Dict[str, int | float | None]] = {}
        for event in load_usage_events(self.skills_root):
            owner = str(event.get("owner") or "")
            skill_id = str(event.get("skill_id") or "")
            if not skill_id:
                continue
            row = counts.setdefault(
                (owner, skill_id),
                {
                    "uses": 0, "retrievals": 0, "successes": 0, "failures": 0,
                    "corrections": 0, "contradictions": 0, "mismatches": 0,
                    "last_used": None, "last_retrieved": None,
                },
            )
            kind = event.get("event")
            timestamp = event.get("timestamp")
            if kind == "use":
                row["uses"] = int(row["uses"] or 0) + 1
                row["last_used"] = timestamp
            elif kind == "retrieval":
                row["retrievals"] = int(row["retrievals"] or 0) + 1
                row["last_retrieved"] = timestamp
            elif kind == "failure":
                row["failures"] = int(row["failures"] or 0) + 1
            elif kind == "success":
                row["successes"] = int(row["successes"] or 0) + 1
            elif kind == "correction":
                row["corrections"] = int(row["corrections"] or 0) + 1
            elif kind == "contradiction":
                row["contradictions"] = int(row["contradictions"] or 0) + 1
            elif kind == "mismatch":
                row["mismatches"] = int(row["mismatches"] or 0) + 1
        return counts

    def set_audit(self, name: str, verdict: str, by_teacher: bool = False,
                  worker_model: str = "", teacher_model: str = "",
                  owner: Optional[str] = None,
                  results: Optional[Dict] = None) -> None:
        """Record the last test/audit result for a skill in the usage sidecar
        (so it surfaces in load() without touching SKILL.md). Drives the
        'verified' check + teacher mark on the card."""
        import time as _t
        found = self._find_skill(name, owner)
        if not found:
            return
        path, _ = found
        bundle: Dict[str, object] = {}
        bundle_error = ""
        with locked(path):
            sk = self._read_skill(path)
            if sk is None or (sk.owner or "") != (owner or ""):
                return
            state = ensure_revision(sk, path, load_state(path))
            row = next(
                (
                    item
                    for item in state.get("history", [])
                    if item.get("revision") == sk.revision
                    and item.get("content_hash") == sk.content_hash
                ),
                None,
            )
            if row is None:
                bundle_error = "the lifecycle revision is unavailable"
            elif row.get("bundle_sha256"):
                if read_bundle_snapshot(path, row) is None:
                    bundle_error = "the immutable bundle snapshot is invalid"
                else:
                    try:
                        current_digest = current_bundle_sha256(
                            path,
                            sk.revision,
                            sk.content_hash,
                        )
                    except (OSError, ValueError) as exc:
                        bundle_error = str(exc)
                    if (
                        not bundle_error
                        and current_digest != row.get("bundle_sha256")
                    ):
                        bundle_error = "the bundle changed without a new revision"
                    if not bundle_error:
                        bundle = {
                            key: row.get(key)
                            for key in (
                                "bundle_root",
                                "bundle_manifest",
                                "bundle_sha256",
                                "bundle_version",
                            )
                        }
            else:
                try:
                    bundle = snapshot_bundle(path, sk.revision, sk.content_hash)
                except (OSError, UnicodeError, ValueError) as exc:
                    bundle_error = str(exc)
                else:
                    row.update(bundle)

            effective_verdict = verdict if not bundle_error else "fail"
            result_summary = {
                "verdict": str(
                    (results or {}).get("verdict") or effective_verdict
                ),
                "passed": effective_verdict == "pass",
                "confidence": _to_float((results or {}).get("confidence"), 0.0),
                "issue_count": (
                    len((results or {}).get("issues") or [])
                    + (1 if bundle_error else 0)
                ),
            }
            if bundle_error:
                logger.warning(
                    "Refusing exact skill attestation for %s: %s",
                    path,
                    bundle_error,
                )
                result_summary["verdict"] = "fail"
                result_summary["bundle_error"] = bundle_error
            state.setdefault("attestations", {})[sk.content_hash] = {
                "skill_id": sk.skill_id,
                "revision": sk.revision,
                "content_hash": sk.content_hash,
                "verdict": effective_verdict,
                "compatible": effective_verdict == "pass",
                "evaluator": teacher_model or worker_model or "manual-audit",
                "audited_at": _t.time(),
                "test_results": result_summary,
                **bundle,
            }
            save_state(path, state)

        usage = self._load_usage()
        key = self._usage_key(sk.skill_id, owner)
        e = usage.setdefault(key, {"uses": 0, "last_used": None})
        e["audit_verdict"] = effective_verdict
        e["audit_skill_id"] = sk.skill_id
        e["audit_revision"] = sk.revision
        e["audit_content_hash"] = sk.content_hash
        e["audit_bundle_sha256"] = bundle.get("bundle_sha256")
        e["audit_compatible"] = effective_verdict == "pass"
        e["audit_evaluator"] = teacher_model or worker_model or "manual-audit"
        e["audit_by_teacher"] = bool(by_teacher)
        if worker_model:
            e["audit_worker_model"] = worker_model
        if teacher_model:
            e["audit_teacher_model"] = teacher_model
        e["audited_at"] = state["attestations"][sk.content_hash]["audited_at"]
        e["audit_results"] = result_summary
        self._save_usage(usage)

    def set_necessity(self, name: str, necessary: bool,
                      redundant_with=None, reason: str = "",
                      owner: Optional[str] = None) -> None:
        """Record the advisory 'is this skill necessary?' judgment in the usage
        sidecar. Surfaced on the card as a flag; never acts on the skill."""
        found = self._find_skill(name, owner)
        if not found:
            return
        _path, sk = found
        usage = self._load_usage()
        key = self._usage_key(sk.skill_id, owner)
        e = usage.setdefault(key, {"uses": 0, "last_used": None})
        e["necessity"] = {
            "necessary": bool(necessary),
            "redundant_with": list(redundant_with or []),
            "reason": str(reason or ""),
            "revision": sk.revision,
            "content_hash": sk.content_hash,
        }
        e["retrieval_precision"] = {
            "ok": bool(necessary),
            "reason": "covered by the necessity review",
            "revision": sk.revision,
            "content_hash": sk.content_hash,
        }
        self._save_usage(usage)

    def set_retrieval_precision(
        self,
        name: str,
        ok: bool,
        reason: str = "",
        owner: Optional[str] = None,
    ) -> None:
        found = self._find_skill(name, owner)
        if not found:
            return
        _path, sk = found
        usage = self._load_usage()
        key = self._usage_key(sk.skill_id, owner)
        entry = usage.setdefault(key, {"uses": 0, "last_used": None})
        entry["retrieval_precision"] = {
            "ok": bool(ok),
            "reason": str(reason or ""),
            "revision": sk.revision,
            "content_hash": sk.content_hash,
        }
        self._save_usage(usage)

    # ----------------------------------------------------------------------
    # Disk scan
    # ----------------------------------------------------------------------

    def _iter_skill_files(self) -> Iterable[str]:
        if not os.path.isdir(self.skills_root):
            return
        # Canonical skills are exactly <category>/<name>/SKILL.md. A recursive
        # walk also finds immutable _revisions bundle copies and turns one
        # skill into many mutable discovery rows.
        with os.scandir(self.skills_root) as categories:
            category_dirs = sorted(
                (
                    entry for entry in categories
                    if entry.is_dir(follow_symlinks=False)
                ),
                key=lambda entry: entry.name,
            )
        for category in category_dirs:
            with os.scandir(category.path) as skills:
                skill_dirs = sorted(
                    (
                        entry for entry in skills
                        if entry.is_dir(follow_symlinks=False)
                    ),
                    key=lambda entry: entry.name,
                )
            for skill_dir in skill_dirs:
                path = os.path.join(skill_dir.path, "SKILL.md")
                if os.path.isfile(path) and not os.path.islink(path):
                    yield path

    def _read_skill(self, path: str) -> Optional[Skill]:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            skill = Skill.from_markdown(text, path=path)
            hydrate_identity(skill, path)
            return skill
        except Exception as e:
            logger.warning(f"Failed to parse {path}: {e}")
            return None


    def _write_skill(self, sk: Skill) -> str:
        from src.openclank.attachment_admission import admit_references, settle_references
        admitted = admit_references(sk.owner, "skills", sk.to_markdown())
        path = self._skill_file(sk.category or "general", sk.name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        hydrate_identity(sk, path)
        from core.atomic_io import atomic_write_text
        atomic_write_text(path, sk.to_markdown())
        settle_references(admitted)
        sk.path = path
        return path

    def _active_skill(
        self,
        path: str,
        head: Skill,
        state: Optional[Dict] = None,
    ) -> tuple[Optional[Skill], Optional[Dict]]:
        """Resolve one exact activation pointer, failing closed on corruption.

        A lifecycle file with an explicit null pointer is an intentional
        demotion and must never fall back to legacy frontmatter.
        """
        state = load_state(path) if state is None else state
        pointer = state.get("published")
        if isinstance(pointer, dict):
            text = read_snapshot(path, pointer)
            if not text:
                logger.warning("Published skill snapshot is unavailable: %s", path)
                return None, pointer
            try:
                active = Skill.from_markdown(text, path=path)
                declared_hash = active.content_hash
                hydrate_identity(active, path)
            except Exception as exc:
                logger.warning("Published skill snapshot is invalid: %s: %s", path, exc)
                return None, pointer
            snapshot_sha256 = str(pointer.get("snapshot_sha256") or "")
            if not snapshot_sha256:
                logger.warning("Published skill pointer lacks a snapshot hash: %s", path)
                return None, pointer
            import hashlib

            if hashlib.sha256(text.encode("utf-8")).hexdigest() != snapshot_sha256:
                logger.warning("Published skill snapshot bytes changed: %s", path)
                return None, pointer
            bundle_text = read_bundle_file(path, pointer, "SKILL.md")
            if bundle_text is None:
                logger.warning("Published skill bundle snapshot is invalid: %s", path)
                return None, pointer
            if bundle_text != text:
                logger.warning(
                    "Published skill bundle does not match its revision snapshot: %s",
                    path,
                )
                return None, pointer
            state_id = str(state.get("skill_id") or "")
            owner = str(head.owner or "")
            valid = (
                state_id == head.skill_id
                and state.get("head_revision") == head.revision
                and state.get("head_hash") == head.content_hash
                and "owner" in state
                and str(state.get("owner") or "") == owner
                and pointer.get("skill_id") == head.skill_id
                and "owner" in pointer
                and str(pointer.get("owner") or "") == owner
                and active.skill_id == head.skill_id
                and str(active.owner or "") == owner
                and active.revision == pointer.get("revision")
                and declared_hash == pointer.get("content_hash")
                and active.content_hash == pointer.get("content_hash")
            )
            if not valid:
                logger.warning("Published skill pointer does not match snapshot: %s", path)
                return None, pointer
            active.status = "published"
            active.hidden = False
            return active, pointer
        # No lifecycle pointer means no local publication authority. This
        # includes legacy frontmatter, explicit demotion, and malformed state.
        return None, None


    def _resume_owner_transfer(self, path: str) -> bool:
        """Finish a state-first owner transfer interrupted after the head write."""
        with locked(path):
            current = self._read_skill(path)
            if current is None:
                return False
            state = load_state(path)
            transfer = state.get("owner_transfer")
            if not isinstance(transfer, dict):
                return False
            if (
                state.get("head_revision") == current.revision
                and state.get("head_hash") == current.content_hash
            ):
                return False
            target_owner = str(transfer.get("to") or "")
            if (
                not target_owner
                or str(current.owner or "") != target_owner
                or str(state.get("owner") or "") != target_owner
                or state.get("skill_id") != current.skill_id
            ):
                return False
            target_tuple = (
                transfer.get("skill_id"),
                transfer.get("name"),
                transfer.get("revision"),
                transfer.get("content_hash"),
            )
            if all(value not in (None, "") for value in target_tuple):
                if target_tuple != (
                    current.skill_id,
                    current.name,
                    current.revision,
                    current.content_hash,
                ):
                    return False
            else:
                head_revision = state.get("head_revision")
                if not (
                    isinstance(head_revision, int)
                    and current.parent_revision == head_revision
                    and current.revision == head_revision + 1
                ):
                    return False
            resumed = dict(transfer)
            resumed.update({
                "skill_id": current.skill_id,
                "name": current.name,
                "revision": current.revision,
                "content_hash": current.content_hash,
                "completed_at": time.time(),
            })
            state["owner_transfer"] = resumed
            ensure_revision(current, path, state)
            return True


    # ----------------------------------------------------------------------
    # Public API — keeps the old method names so callers don't break
    # ----------------------------------------------------------------------

    def load_all(self) -> List[Dict]:
        """Return every skill as a plain dict, plus any legacy JSON entries."""
        usage = self._load_usage()
        event_counts = self._event_counts()
        out: List[Dict] = []
        seen_names: set[str] = set()
        for path in self._iter_skill_files():
            sk = self._read_skill(path)
            if not sk:
                continue

            d = sk.to_dict()
            u = self._usage_entry(
                usage, sk.skill_id, sk.owner, legacy_name=sk.name
            )
            counters = event_counts.get((str(sk.owner or ""), sk.skill_id), {})
            d["uses"] = int(counters.get("uses", u.get("uses", 0)) or 0)
            d["retrievals"] = int(counters.get("retrievals", 0) or 0)
            d["successes"] = int(counters.get("successes", 0) or 0)
            d["failures"] = int(counters.get("failures", 0) or 0)
            d["corrections"] = int(counters.get("corrections", 0) or 0)
            d["contradictions"] = int(counters.get("contradictions", 0) or 0)
            d["mismatches"] = int(counters.get("mismatches", 0) or 0)
            d["last_used"] = counters.get("last_used") or u.get("last_used")
            d["last_retrieved"] = counters.get("last_retrieved")
            d["audit_verdict"] = u.get("audit_verdict")
            d["audit_by_teacher"] = bool(u.get("audit_by_teacher"))
            d["audit_worker_model"] = u.get("audit_worker_model")
            d["audit_teacher_model"] = u.get("audit_teacher_model")
            d["audited_at"] = u.get("audited_at")
            d["audit_revision"] = u.get("audit_revision")
            d["audit_content_hash"] = u.get("audit_content_hash")
            d["audit_evaluator"] = u.get("audit_evaluator")
            d["audit_compatible"] = bool(u.get("audit_compatible"))
            d["audit_results"] = u.get("audit_results")
            d["last_audit"] = (
                {
                    "at": u.get("audited_at"),
                    "verdict": u.get("audit_verdict"),
                    "evaluator": u.get("audit_evaluator"),
                    "revision": u.get("audit_revision"),
                    "content_hash": u.get("audit_content_hash"),
                }
                if u.get("audited_at") is not None
                else None
            )
            d["necessity"] = u.get("necessity")
            d["retrieval_precision"] = u.get("retrieval_precision")
            state = load_state(path)
            published = state.get("published") if isinstance(state.get("published"), dict) else {}
            active, _pointer = self._active_skill(path, sk, state)
            d["head_revision"] = sk.revision
            d["published_revision"] = published.get("revision")
            d["published_hash"] = published.get("content_hash")
            d["publisher"] = published.get("publisher")
            d["waiver"] = published.get("waiver")
            d["active"] = active is not None
            if active is None and d.get("status") == "published":
                # Runtime truth comes from the immutable pointer, never stale
                # or malformed frontmatter.
                d["status"] = "draft"
            exact_head_attestation = (
                u.get("audit_verdict") == "pass"
                and u.get("audit_revision") == sk.revision
                and u.get("audit_content_hash") == sk.content_hash
                and bool(u.get("audit_compatible"))
            )
            active_attestation = (
                state.get("attestations", {}).get(active.content_hash, {})
                if active is not None and isinstance(state.get("attestations"), dict)
                else {}
            )
            d["head_trust"] = (
                "evaluated"
                if exact_head_attestation
                else "untrusted"
                if sk.source in ("imported", "remote")
                else "staged"
            )
            if active is not None and published.get("waiver"):
                d["active_trust"] = "waived"
            elif (
                active is not None
                and active_attestation.get("verdict") == "pass"
                and active_attestation.get("compatible")
                and active_attestation.get("skill_id") == active.skill_id
                and active_attestation.get("revision") == active.revision
                and active_attestation.get("content_hash") == active.content_hash
            ):
                d["active_trust"] = "verified"
            elif active is not None:
                d["active_trust"] = "legacy"
            elif sk.source in ("imported", "remote"):
                d["active_trust"] = None
            else:
                d["active_trust"] = None
            d["trust"] = d["active_trust"] or d["head_trust"]
            d["provenance"] = {
                "source": sk.source,
                "source_status": sk.source_status,
                "uri": sk.source_uri,
                "source_revision": sk.source_revision,
                "memory_ids": list(sk.source_memory_ids),
            }
            out.append(d)
            seen_names.add(sk.name)
        # Legacy JSON entries — surfaced as draft, not editable from new flow
        if os.path.exists(self.legacy_file):
            try:
                with open(self.legacy_file, encoding="utf-8") as f:
                    legacy = json.load(f)
                if isinstance(legacy, list):
                    for row in legacy:
                        if not isinstance(row, dict):
                            continue
                        name = slugify(row.get("title") or row.get("id") or "skill")
                        if name in seen_names:
                            continue
                        out.append({
                            "id": row.get("id") or name,
                            "name": name,
                            "description": row.get("title", ""),
                            "version": "0.0.1",
                            "category": "legacy",
                            "tags": row.get("tags") or [],
                            "status": row.get("status") or "draft",
                            "confidence": row.get("confidence", 0.5),
                            "source": row.get("source", "imported"),
                            "owner": row.get("owner"),
                            "when_to_use": row.get("problem", ""),
                            "procedure": row.get("steps") or [],
                            "pitfalls": [],
                            "verification": [],
                            "body_extra": row.get("solution", ""),
                            "title": row.get("title", ""),
                            "problem": row.get("problem", ""),
                            "solution": row.get("solution", ""),
                            "steps": row.get("steps") or [],
                            "uses": row.get("uses", 0),
                            "last_used": row.get("last_used"),
                            "_legacy": True,
                        })
            except Exception:
                pass
        return out

    def load(self, owner: Optional[str] = None) -> List[Dict]:
        entries = self.load_all()
        if owner is None:
            return entries
        # SECURITY: strict ownership filter. The previous predicate also
        # included skills with NO owner field (`not s.get("owner")`), which
        # leaked legacy / un-stamped skills to every authenticated user.
        # Hide them now; the owner needs to be backfilled on disk if those
        # skills should be visible to a specific user.
        return [s for s in entries if s.get("owner") == owner]

    # ----------------------------------------------------------------------
    # CRUD — disk-backed
    # ----------------------------------------------------------------------

    def add_skill(self, *args, **kwargs) -> Dict:
        """Create a skill while holding the global memory/skill lifecycle lock."""
        lock_target = os.path.join(self.skills_root, "PROMOTIONS")
        with locked(lock_target):
            return self._add_skill_unlocked(*args, **kwargs)

    def _add_skill_unlocked(
        self,
        title: str = "",
        problem: str = "",
        solution: str = "",
        steps: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        source: str = "learned",
        source_uri: Optional[str] = None,
        source_revision: Optional[str] = None,
        source_memory_ids: Optional[List[str]] = None,
        teacher_model: Optional[str] = None,
        confidence: float = 0.8,
        session_id: Optional[str] = None,
        owner: Optional[str] = None,
        # New-schema fields (optional; fall back to old shape if absent)
        name: Optional[str] = None,
        description: Optional[str] = None,
        category: str = "general",
        when_to_use: Optional[str] = None,
        procedure: Optional[List[str]] = None,
        pitfalls: Optional[List[str]] = None,
        verification: Optional[List[str]] = None,
        platforms: Optional[List[str]] = None,
        requires_toolsets: Optional[List[str]] = None,
        fallback_for_toolsets: Optional[List[str]] = None,
        status: str = "draft",
        version: str = "1.0.0",
    ) -> Dict:
        self._assert_memory_sources_available(
            owner,
            source_memory_ids or (),
            source_uri,
        )

        # Normalize name
        nm = slugify(name or title or description or "skill")

        # Free dedup-at-creation (always, no API): for LLM-authored skills,
        # skip if a near-identical skill already exists (Jaccard over
        # name+description+when_to_use+procedure). User-authored skills are
        # never auto-skipped — a human asked for it. The every-X AI audit
        # handles the fuzzier near-duplicates this cheap check won't catch.
        _all = self.load_all()
        _dedup_pool = _all if owner is None else [s for s in _all if s.get("owner") == owner]
        if source != "user":
            cand = _tokenize(" ".join([
                nm, (description or title or ""),
                (when_to_use if when_to_use is not None else (problem or "")),
                " ".join(procedure if procedure is not None else (steps or [])),
            ]))
            if cand:
                for s in _dedup_pool:
                    ex = _tokenize(" ".join([
                        s.get("name", ""), s.get("description", ""),
                        s.get("when_to_use", ""),
                        " ".join(s.get("procedure", []) or []),
                    ]))
                    if _jaccard(cand, ex) >= 0.82:
                        # Discovery is not execution: do not inflate "uses"
                        # merely because the authoring deduper found a match.
                        return {**s, "_deduped": True, "_duplicate_of": s.get("name")}

        # Avoid clobbering an existing skill with the same name
        existing = {s["name"] for s in _all} | self._reserved_skill_names()
        base = nm
        i = 2
        while nm in existing:
            nm = f"{base}-{i}"
            i += 1

        sk = Skill(
            name=nm,
            skill_id=str(uuid.uuid4()),
            revision=1,
            description=(description or title or "").strip(),
            version=version,
            category=category or "general",
            tags=list(tags or []),
            platforms=list(platforms or []),
            requires_toolsets=list(requires_toolsets or []),
            fallback_for_toolsets=list(fallback_for_toolsets or []),
            status="draft",
            confidence=float(confidence),
            source=source,
            source_uri=source_uri,
            source_revision=source_revision,
            source_memory_ids=list(source_memory_ids or []),
            teacher_model=teacher_model,
            owner=owner,
            when_to_use=(when_to_use if when_to_use is not None else (problem or "")),
            procedure=list(procedure if procedure is not None else (steps or [])),
            pitfalls=list(pitfalls or []),
            verification=list(verification or []),
            body_extra=(solution if solution and not procedure else ""),
        )
        path = self._write_skill(sk)
        ensure_revision(sk, path)

        return sk.to_dict()

    def import_bundle_from_files(self, *args, **kwargs) -> Dict:
        """Install a bundle without racing memory-derived quarantine."""
        lock_target = os.path.join(self.skills_root, "PROMOTIONS")
        with locked(lock_target):
            return self._import_bundle_from_files_unlocked(*args, **kwargs)

    def _import_bundle_from_files_unlocked(
        self,
        files: Dict[str, str],
        *,
        owner: Optional[str] = None,
        source_url: str = "",
        source_revision: str = "",
        category: str = "imported",
    ) -> Dict:
        """Install a fetched skill bundle (relative path → text) under skills/."""
        from .skill_importer import SkillImportError, pick_skill_md, _safe_relpath
        from core.atomic_io import _fsync_directory, atomic_write_text

        if not files:
            raise SkillImportError("empty bundle")
        from src.openclank.attachment_admission import admit_references, settle_references
        admitted = admit_references(owner, "skills", files)
        _rel, skill_md = pick_skill_md(files)
        sk = Skill.from_markdown(skill_md)
        incoming_source_uri = str(sk.source_uri or "").strip()
        self._assert_memory_sources_available(
            owner,
            sk.source_memory_ids,
            sk.source_uri,
        )
        nm = slugify(sk.name or _rel.split("/")[-2] or "skill")
        cat = slugify(category or sk.category or "imported", fallback="imported")

        lock_target = os.path.join(self.skills_root, ".import")
        with locked(lock_target):
            existing = {
                s["name"] for s in self.load_all()
            } | self._reserved_skill_names()
            base = nm
            i = 2
            while nm in existing:
                nm = f"{base}-{i}"
                i += 1

            category_dir = os.path.join(self.skills_root, cat)
            skill_dir = self._skill_dir(cat, nm)
            os.makedirs(category_dir, exist_ok=True)
            stage_dir = tempfile.mkdtemp(
                prefix=".skill-import-",
                dir=self.data_dir,
            )
            try:
                # The staging tree is outside skills_root, so discovery cannot
                # observe source frontmatter or a partly written bundle.
                staged_paths: set[str] = set()
                for rel, content in sorted(files.items()):
                    if not isinstance(rel, str) or not isinstance(content, str):
                        raise SkillImportError("skill bundle files must be UTF-8 text")
                    safe = _safe_relpath(rel)
                    if _safe_bundle_path(safe) != safe:
                        raise SkillImportError("skill bundle contains a reserved path")
                    if safe.lower().endswith("skill.md") and safe != "SKILL.md":
                        raise SkillImportError("skill bundle contains a nested SKILL.md")
                    path_key = safe.casefold()
                    if path_key in staged_paths:
                        raise SkillImportError("skill bundle contains duplicate paths")
                    staged_paths.add(path_key)
                    dest = os.path.join(stage_dir, safe)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    atomic_write_text(dest, content)

                sk.name = nm
                sk.category = cat
                sk.owner = owner
                sk.source_status = sk.status
                sk.source = "imported"
                sk.source_uri = (
                    incoming_source_uri
                    if incoming_source_uri.startswith("memory-promotion:")
                    else (source_url or incoming_source_uri or None)
                )
                sk.source_revision = source_revision or None
                sk.status = "draft"
                sk.skill_id = str(uuid.uuid4())
                sk.revision = 1
                if source_url:
                    extra = (sk.body_extra or "").strip()
                    note = f"Imported from {source_url}"
                    sk.body_extra = f"{extra}\n\n{note}".strip() if extra else note

                staged_skill = os.path.join(stage_dir, "SKILL.md")
                atomic_write_text(staged_skill, sk.to_markdown())
                with open(staged_skill, encoding="utf-8") as handle:
                    validated = Skill.from_markdown(handle.read(), path=staged_skill)
                if (
                    validated.status != "draft"
                    or validated.name != nm
                    or (validated.owner or "") != (owner or "")
                ):
                    raise SkillImportError("staged skill did not preserve draft ownership")
                ensure_revision(validated, staged_skill, state={"published": None})

                if os.path.lexists(skill_dir):
                    raise SkillImportError("skill destination changed during import")
                validated.path = self._skill_file(cat, nm)
                result = validated.to_dict()
                os.replace(stage_dir, skill_dir)
                try:
                    _fsync_directory(category_dir)
                except OSError:
                    os.replace(skill_dir, stage_dir)
                    try:
                        _fsync_directory(category_dir)
                    except OSError:
                        pass
                    raise
                stage_dir = ""
                settle_references(admitted)
                return result
            finally:
                if stage_dir:
                    shutil.rmtree(stage_dir, ignore_errors=True)

    def update_skill(self, *args, **kwargs) -> bool:
        """Update a skill under the global memory/skill lifecycle lock."""
        lock_target = os.path.join(self.skills_root, "PROMOTIONS")
        with locked(lock_target):
            return self._update_skill_unlocked(*args, **kwargs)

    def _update_skill_unlocked(
        self,
        skill_id: str,
        updates: Dict,
        owner: Optional[str] = None,
        *,
        _allow_publish: bool = False,
    ) -> bool:
        """`skill_id` is the slug name. Allows updating any field plus
        renames if `name` changes (file is moved on disk).

        The call is owner-scoped: it matches a skill on disk only if
        `skill.owner == owner` (string compare; both empty-string and
        None mean "ownerless"). When `owner is None` (the default), the
        call only matches skills whose own `owner` field is empty —
        callers that want to edit an owned skill must pass the matching
        owner explicitly. This prevents a caller with one owner from
        mutating a file owned by another user that happens to share
        the same slug across category directories. The `owner` key in
        `updates` is also ignored — ownership is not an editable field
        via this path; rename or admin tooling is required for that.
        Generic edits cannot activate a draft. They also stage any changed
        published procedure so revised instructions are not live until a human
        or successful audit explicitly publishes them again.
        """
        updates = dict(updates or {})
        if "source_memory_ids" in updates or "source_uri" in updates:
            self._assert_memory_sources_available(
                owner,
                updates.get("source_memory_ids") or (),
                updates.get("source_uri"),
            )
        if "status" in updates and updates.get("status") not in ("draft", "published"):
            return False
        if updates.get("status") == "published" and not _allow_publish:
            return False

        found = self._find_skill(skill_id, owner)
        if not found:
            return False
        path, _ = found
        with locked(path):
            # Re-read under the lock so an edit cannot race a publish/rename.
            sk = self._read_skill(path)
            if not sk or (sk.owner or "") != (owner or ""):
                return False
            if sk.name != skill_id and sk.skill_id != skill_id:
                return False

            state = ensure_revision(sk, path, load_state(path))
            old_hash = sk.content_hash
            old_revision = sk.revision
            old_dir = os.path.dirname(path)

            scalar_keys = (
                "description", "version", "category", "status", "confidence",
                "source", "source_uri", "source_revision", "teacher_model",
                "when_to_use", "body_extra",
            )
            for key in scalar_keys:
                if key in updates:
                    setattr(sk, key, updates[key])
            list_keys = (
                "tags", "procedure", "pitfalls", "verification", "platforms",
                "requires_toolsets", "fallback_for_toolsets",
                "source_memory_ids",
            )
            for key in list_keys:
                if key in updates:
                    setattr(sk, key, list(updates[key] or []))

            if "title" in updates and "description" not in updates:
                sk.description = updates["title"]
            if "problem" in updates and "when_to_use" not in updates:
                sk.when_to_use = updates["problem"]
            if "solution" in updates and "body_extra" not in updates and not sk.procedure:
                sk.body_extra = updates["solution"]
            if "steps" in updates and "procedure" not in updates:
                sk.procedure = list(updates["steps"] or [])

            sk.name = slugify(updates.get("name") or sk.name)
            new_hash = sk.compute_content_hash()
            changed_content = new_hash != old_hash
            if changed_content:
                sk.parent_revision = old_revision
                sk.revision = old_revision + 1
                sk.status = "draft"
                sk.content_hash = new_hash

            new_path = self._skill_file(sk.category, sk.name)
            if new_path != path:
                new_dir = os.path.dirname(new_path)
                if os.path.isdir(new_dir):
                    logger.warning("Skill rename target exists: %s", new_dir)
                    return False
                os.makedirs(os.path.dirname(new_dir), exist_ok=True)
                os.rename(old_dir, new_dir)
                path = new_path

            if updates.get("status") == "draft" and not changed_content:
                state["published"] = None
                state.setdefault("metrics", {})["demotions"] = (
                    int(state.get("metrics", {}).get("demotions", 0)) + 1
                )
                save_state(path, state)
            self._write_skill(sk)
            state = ensure_revision(sk, path, state)
            return True

    def publish_readiness(
        self, skill_id: str, owner: Optional[str] = None
    ) -> Dict[str, object]:
        found = self._find_skill(skill_id, owner)
        if not found:
            return {"ready": False, "blockers": ["skill not found"]}
        path, sk = found
        usage = self._load_usage()
        audit = self._usage_entry(
            usage, sk.skill_id, owner, legacy_name=sk.name
        )
        blockers: List[str] = []
        if audit.get("audit_verdict") != "pass":
            blockers.append("a passing audit is required")
        if audit.get("audit_revision") != sk.revision:
            blockers.append("the audit is for a different revision")
        if audit.get("audit_content_hash") != sk.content_hash:
            blockers.append("the audit is for different content")
        if not audit.get("audit_bundle_sha256"):
            blockers.append("the audit is not pinned to an immutable bundle")
        if not audit.get("audit_compatible"):
            blockers.append("platform/tool compatibility was not verified")
        platform = (
            "windows" if sys.platform.startswith("win")
            else "macos" if sys.platform == "darwin"
            else "linux"
        )
        declared_platforms = {str(item).lower() for item in sk.platforms}
        if declared_platforms and platform not in declared_platforms:
            blockers.append(f"this revision does not support {platform}")
        if sk.requires_toolsets:
            try:
                from src.tool_policy import known_tool_names

                available = set(known_tool_names())
            except Exception:
                available = set()
            missing = sorted(set(sk.requires_toolsets) - available)
            if missing:
                blockers.append(
                    "required tools are unavailable: " + ", ".join(missing)
                )
        necessity = audit.get("necessity")
        if not isinstance(necessity, dict) or necessity.get("necessary") is not True:
            blockers.append("necessity and retrieval precision must pass")
        elif (
            necessity.get("revision") != sk.revision
            or necessity.get("content_hash") != sk.content_hash
        ):
            blockers.append("necessity and retrieval precision are for different content")
        precision = audit.get("retrieval_precision")
        if not isinstance(precision, dict) or precision.get("ok") is not True:
            blockers.append("retrieval precision must pass")
        elif (
            precision.get("revision") != sk.revision
            or precision.get("content_hash") != sk.content_hash
        ):
            blockers.append("retrieval precision is for different content")
        state = load_state(path)
        attestation = (
            state.get("attestations", {}).get(sk.content_hash, {})
            if isinstance(state.get("attestations"), dict)
            else {}
        )
        if not (
            attestation.get("skill_id") == sk.skill_id
            and attestation.get("revision") == sk.revision
            and attestation.get("content_hash") == sk.content_hash
            and attestation.get("verdict") == "pass"
            and attestation.get("compatible") is True
        ):
            blockers.append("the lifecycle has no exact compatible quality attestation")
        if read_bundle_snapshot(path, attestation) is None:
            blockers.append("the audit has no valid immutable reference bundle")
        try:
            bundle_sha256 = current_bundle_sha256(path)
        except (OSError, UnicodeError, ValueError):
            blockers.append("the reference bundle cannot be snapshotted safely")
        else:
            if attestation.get("bundle_sha256") != bundle_sha256:
                blockers.append("the reference bundle changed after its audit")
        return {
            "ready": not blockers,
            "blockers": blockers,
            "skill_id": sk.skill_id,
            "revision": sk.revision,
            "content_hash": sk.content_hash,
            "evaluator": audit.get("audit_evaluator"),
            "test_results": attestation.get("test_results"),
        }

    def publish_skill(self, *args, **kwargs) -> bool:
        """Publish a skill under the global memory/skill lifecycle lock."""
        lock_target = os.path.join(self.skills_root, "PROMOTIONS")
        with locked(lock_target):
            return self._publish_skill_unlocked(*args, **kwargs)

    def _publish_skill_unlocked(
        self,
        skill_id: str,
        owner: Optional[str] = None,
        *,
        expected_revision: Optional[int] = None,
        expected_hash: Optional[str] = None,
        publisher: str = "",
        waiver_reason: str = "",
        allow_waiver: bool = False,
    ) -> bool:
        """CAS-publish only the exact audited revision (or a visible waiver)."""
        found = self._find_skill(skill_id, owner)
        if not found:
            return False
        path, _ = found
        with locked(path):
            sk = self._read_skill(path)
            if not sk or (sk.owner or "") != (owner or ""):
                return False
            if not publisher.startswith(("user:", "admin:")):
                return False
            if expected_revision is None or expected_hash is None:
                return False
            if sk.revision != int(expected_revision) or sk.content_hash != expected_hash:
                return False
            readiness = self.publish_readiness(sk.skill_id, owner)
            if waiver_reason.strip() and (
                not allow_waiver or not publisher.startswith("admin:")
            ):
                return False
            if not readiness["ready"] and not waiver_reason.strip():
                return False
            state = ensure_revision(sk, path, load_state(path))
            row = next(
                (
                    item for item in state.get("history", [])
                    if item.get("revision") == sk.revision
                    and item.get("content_hash") == sk.content_hash
                ),
                None,
            )
            if not row:
                return False
            pointer = {
                "skill_id": sk.skill_id,
                "owner": str(sk.owner or ""),
                "revision": sk.revision,
                "content_hash": sk.content_hash,
                "snapshot": row["snapshot"],
                "snapshot_sha256": row.get("snapshot_sha256"),
                "published_at": time.time(),
                "publisher": publisher or f"user:{owner or 'local'}",
            }
            attestation = state.get("attestations", {}).get(sk.content_hash, {})
            if read_bundle_snapshot(path, attestation) is None:
                return False
            pointer.update({
                "bundle_root": attestation.get("bundle_root"),
                "bundle_manifest": attestation.get("bundle_manifest"),
                "bundle_sha256": attestation.get("bundle_sha256"),
                "bundle_version": attestation.get("bundle_version"),
            })
            if waiver_reason.strip():
                pointer["waiver"] = {
                    "reason": waiver_reason.strip(),
                    "by": publisher or f"admin:{owner or 'local'}",
                    "at": time.time(),
                    "blockers": readiness["blockers"],
                }
            state["published"] = pointer
            state.setdefault("metrics", {})["publishes"] = (
                int(state.get("metrics", {}).get("publishes", 0)) + 1
            )
            sk.status = "published"
            save_state(path, state)
            self._write_skill(sk)
            return True

    def rollback_skill(
        self,
        skill_id: str,
        target_revision: int,
        owner: Optional[str] = None,
        *,
        publisher: str = "",
    ) -> bool:
        """Move the published pointer to a prior exact revision without rewriting it."""
        found = self._find_skill(skill_id, owner)
        if not found:
            return False
        path, sk = found
        with locked(path):
            if not publisher.startswith(("user:", "admin:")):
                return False
            current = self._read_skill(path)
            if not self._matches_locked_head(current, sk, skill_id, owner):
                return False
            assert current is not None
            sk = current
            state = ensure_revision(sk, path, load_state(path))
            row = next(
                (
                    item for item in state.get("history", [])
                    if item.get("revision") == int(target_revision)
                ),
                None,
            )
            if not row:
                return False
            attestations = state.get("attestations", {})
            exact = attestations.get(row.get("content_hash"), {})
            if not (
                exact.get("skill_id") == sk.skill_id
                and exact.get("revision") == row.get("revision")
                and exact.get("content_hash") == row.get("content_hash")
                and exact.get("verdict") == "pass"
                and exact.get("compatible") is True
            ):
                return False
            text = read_snapshot(path, row)
            if not text:
                return False
            try:
                target = Skill.from_markdown(text, path=path)
            except Exception:
                return False
            declared_hash = target.content_hash
            if (
                target.skill_id != sk.skill_id
                or str(target.owner or "") != str(sk.owner or "")
                or str(state.get("owner") or "") != str(sk.owner or "")
                or target.revision != row.get("revision")
                or declared_hash != row.get("content_hash")
                or target.compute_content_hash() != row.get("content_hash")
            ):
                return False
            platform = (
                "windows" if sys.platform.startswith("win")
                else "macos" if sys.platform == "darwin"
                else "linux"
            )
            declared_platforms = {str(item).lower() for item in target.platforms}
            if declared_platforms and platform not in declared_platforms:
                return False
            if target.requires_toolsets:
                try:
                    from src.tool_policy import known_tool_names

                    available = set(known_tool_names())
                except Exception:
                    available = set()
                if set(target.requires_toolsets) - available:
                    return False
            if exact.get("test_results", {}).get("passed") is not True:
                return False
            if read_bundle_snapshot(path, exact) is None:
                return False
            state["published"] = {
                "skill_id": sk.skill_id,
                "owner": str(sk.owner or ""),
                "revision": row["revision"],
                "content_hash": row["content_hash"],
                "snapshot": row["snapshot"],
                "snapshot_sha256": row.get("snapshot_sha256"),
                "published_at": time.time(),
                "publisher": publisher or f"user:{owner or 'local'}",
                "rollback": True,
                "bundle_root": exact["bundle_root"],
                "bundle_manifest": exact["bundle_manifest"],
                "bundle_sha256": exact["bundle_sha256"],
                "bundle_version": exact.get("bundle_version"),
            }
            state.setdefault("metrics", {})["rollbacks"] = (
                int(state.get("metrics", {}).get("rollbacks", 0)) + 1
            )
            save_state(path, state)
            return True

    def _load_promotions(self) -> List[Dict]:
        try:
            with open(self.promotions_file, encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, list) else []
        except (OSError, ValueError):
            return []

    def _save_promotions(self, rows: List[Dict]) -> None:
        from core.atomic_io import atomic_write_json

        atomic_write_json(self.promotions_file, rows, indent=2)

    def list_promotions(self, owner: Optional[str]) -> List[Dict]:
        return [
            row for row in self._load_promotions()
            if row.get("owner") == (owner or "")
        ]

    def nominate_promotion(
        self,
        *,
        owner: Optional[str],
        recommender: str,
        citations: List[Dict],
        name: str,
        scope: str,
        counterexamples: Optional[List[str]] = None,
    ) -> Dict:
        """Create a provenance-only candidate; memory bodies are never copied."""
        allowed = (
            "memory_id", "content_hash", "source", "source_type", "kind",
            "updated_at", "source_uri", "source_revision",
        )
        unique = {
            str(row.get("memory_id") or ""): {
                key: row.get(key)
                for key in allowed
                if row.get(key) not in (None, "")
            }
            for row in citations
            if isinstance(row, dict) and row.get("memory_id")
        }
        if len(unique) < 2:
            raise ValueError("promotion requires at least two curated memory citations")
        distinct_claims = {
            str(row.get("content_hash") or "")
            for row in unique.values()
            if row.get("content_hash")
        }
        if len(distinct_claims) < 2:
            raise ValueError("promotion requires two independently hashed curated memories")
        if counterexamples:
            raise ValueError("contradictory evidence must be resolved before drafting")
        lock_target = os.path.join(self.skills_root, "PROMOTIONS")
        with locked(lock_target):
            self._assert_memory_sources_available(owner, unique)
            rows = self._load_promotions()
            row = {
                "id": str(uuid.uuid4()),
                "owner": owner or "",
                "state": "candidate",
                "name": slugify(name),
                "scope": str(scope or "owner"),
                "citations": list(unique.values()),
                "counterexamples": [],
                "roles": {"recommender": recommender},
                "metrics": {
                    "nominations": 1,
                    "evaluations": 0,
                    "rejections": 0,
                    "publishes": 0,
                    "rollbacks": 0,
                },
                "created_at": time.time(),
                "feedback": [],
            }
            rows.append(row)
            self._save_promotions(rows)
            return dict(row)

    def draft_promotion(
        self,
        promotion_id: str,
        *,
        owner: Optional[str],
        drafter: str,
        fields: Dict,
    ) -> Dict:
        lock_target = os.path.join(self.skills_root, "PROMOTIONS")
        with locked(lock_target):
            rows = self._load_promotions()
            row = next(
                (
                    item for item in rows
                    if item.get("id") == promotion_id
                    and item.get("owner") == (owner or "")
                ),
                None,
            )
            if not row or row.get("state") != "candidate":
                raise ValueError("promotion is not an undrafted candidate")
            citation_ids = [
                str(item.get("memory_id"))
                for item in row.get("citations", [])
                if item.get("memory_id")
            ]
            self._assert_memory_sources_available(owner, citation_ids)
            skill = self._add_skill_unlocked(
                name=fields.get("name") or row.get("name"),
                description=fields.get("description") or "",
                category=fields.get("category") or "memory",
                tags=fields.get("tags") or [],
                platforms=fields.get("platforms") or [],
                requires_toolsets=fields.get("requires_toolsets") or [],
                when_to_use=fields.get("when_to_use") or "",
                procedure=fields.get("procedure") or [],
                pitfalls=fields.get("pitfalls") or [],
                verification=fields.get("verification") or [],
                source="memory-promotion",
                source_uri=f"memory-promotion:{promotion_id}",
                source_revision=promotion_id,
                source_memory_ids=citation_ids,
                owner=owner,
                status="draft",
            )
            row["state"] = "drafted"
            row["skill_id"] = skill["skill_id"]
            row["skill_name"] = skill["name"]
            row["roles"]["drafter"] = drafter
            row["drafted_at"] = time.time()
            self._save_promotions(rows)
            return {"promotion": dict(row), "skill": skill}

    def evaluate_promotion(
        self,
        promotion_id: str,
        *,
        owner: Optional[str],
        evaluator: str,
        feedback: str = "",
    ) -> Dict:
        lock_target = os.path.join(self.skills_root, "PROMOTIONS")
        with locked(lock_target):
            rows = self._load_promotions()
            row = next(
                (
                    item for item in rows
                    if item.get("id") == promotion_id
                    and item.get("owner") == (owner or "")
                ),
                None,
            )
            if not row or row.get("state") not in ("drafted", "rejected"):
                raise ValueError("promotion has no staged draft to evaluate")
            readiness = self.publish_readiness(row.get("skill_id") or "", owner)
            row["metrics"]["evaluations"] = int(row["metrics"].get("evaluations", 0)) + 1
            row["roles"]["evaluator"] = evaluator
            row["evaluated_at"] = time.time()
            if readiness["ready"]:
                row["state"] = "evaluated"
                row["attestation"] = {
                    "revision": readiness["revision"],
                    "content_hash": readiness["content_hash"],
                    "evaluator": readiness.get("evaluator") or evaluator,
                    "test_results": readiness.get("test_results"),
                }
            else:
                row["state"] = "rejected"
                row["metrics"]["rejections"] = int(row["metrics"].get("rejections", 0)) + 1
                row.setdefault("feedback", []).append(
                    {
                        "at": time.time(),
                        "by": evaluator,
                        "reason": feedback or "; ".join(readiness["blockers"]),
                    }
                )
            self._save_promotions(rows)
            return dict(row)

    def publish_promotion(
        self,
        promotion_id: str,
        *,
        owner: Optional[str],
        publisher: str,
    ) -> Dict:
        lock_target = os.path.join(self.skills_root, "PROMOTIONS")
        with locked(lock_target):
            rows = self._load_promotions()
            row = next(
                (
                    item for item in rows
                    if item.get("id") == promotion_id
                    and item.get("owner") == (owner or "")
                ),
                None,
            )
            if not row or row.get("state") != "evaluated":
                raise ValueError("promotion must be evaluated before publication")
            attestation = row.get("attestation") or {}
            evaluator = str(row.get("roles", {}).get("evaluator") or "")
            if evaluator and evaluator == publisher:
                raise ValueError("publisher must be distinct from the evaluator")
            ok = self._publish_skill_unlocked(
                row.get("skill_id") or "",
                owner,
                expected_revision=attestation.get("revision"),
                expected_hash=attestation.get("content_hash"),
                publisher=publisher,
            )
            if not ok:
                raise ValueError("staged revision changed or no longer passes quality gates")
            row["state"] = "published"
            row["roles"]["publisher"] = publisher
            row["published_at"] = time.time()
            row["metrics"]["publishes"] = int(row["metrics"].get("publishes", 0)) + 1
            self._save_promotions(rows)
            return dict(row)

    def reject_promotion(
        self,
        promotion_id: str,
        *,
        owner: Optional[str],
        actor: str,
        reason: str,
    ) -> Dict:
        lock_target = os.path.join(self.skills_root, "PROMOTIONS")
        with locked(lock_target):
            rows = self._load_promotions()
            row = next(
                (
                    item for item in rows
                    if item.get("id") == promotion_id
                    and item.get("owner") == (owner or "")
                ),
                None,
            )
            if not row:
                raise ValueError("promotion not found")
            row["state"] = "rejected"
            row["metrics"]["rejections"] = int(row["metrics"].get("rejections", 0)) + 1
            row.setdefault("feedback", []).append(
                {"at": time.time(), "by": actor, "reason": str(reason or "rejected")}
            )
            self._save_promotions(rows)
            return dict(row)

    def rollback_promotion(
        self,
        promotion_id: str,
        target_revision: int,
        *,
        owner: Optional[str],
        actor: str,
        reason: str = "",
    ) -> Dict:
        lock_target = os.path.join(self.skills_root, "PROMOTIONS")
        with locked(lock_target):
            rows = self._load_promotions()
            row = next(
                (
                    item for item in rows
                    if item.get("id") == promotion_id
                    and item.get("owner") == (owner or "")
                ),
                None,
            )
            if not row or row.get("state") != "published":
                raise ValueError("only a published promotion can be rolled back")
            if not self.rollback_skill(
                row.get("skill_id") or "",
                target_revision,
                owner,
                publisher=actor,
            ):
                raise ValueError("target revision is missing or was never audited")
            row["state"] = "rolled_back"
            row["metrics"]["rollbacks"] = int(row["metrics"].get("rollbacks", 0)) + 1
            row.setdefault("feedback", []).append(
                {
                    "at": time.time(),
                    "by": actor,
                    "reason": reason or f"rolled back to revision {target_revision}",
                }
            )
            self._save_promotions(rows)
            return dict(row)

    # ------------------------------------------------------------------
    # Exact-owner destructive reset
    # ------------------------------------------------------------------

    @staticmethod
    def _account_owner(value: object) -> str:
        return str(value or "").strip().lower()

    @staticmethod
    def _account_identity_fingerprint(inventory: Dict[str, object]) -> str:
        identities: list[str] = []
        for row in inventory.get("skills", []):
            if isinstance(row, dict):
                identities.append("skill:" + str(row.get("skill_id") or ""))
        for row in inventory.get("legacy", []):
            if isinstance(row, dict):
                identities.append(
                    "legacy:" + str(row.get("id") or row.get("name") or "")
                )
        identities.extend(
            "usage:" + str(value).partition("::")[2]
            for value in inventory.get("usage_keys", [])
        )
        for row in inventory.get("promotions", []):
            if isinstance(row, dict):
                identities.append(
                    "promotion:"
                    + str(row.get("id") or row.get("promotion_id") or "")
                )
        for row in inventory.get("events", []):
            if isinstance(row, dict):
                identities.append(
                    "event:"
                    + ":".join(
                        str(row.get(key) or "")
                        for key in ("skill_id", "event", "timestamp")
                    )
                )
        for row in inventory.get("audit_jobs", []):
            if isinstance(row, dict):
                job = row.get("row") or {}
                key = job.get("key") if isinstance(job, dict) else []
                identities.append(
                    "audit:"
                    + str(row.get("kind") or "")
                    + ":"
                    + ":".join(str(value) for value in list(key or [])[1:])
                )
        for row in inventory.get("recoveries", []):
            if isinstance(row, dict):
                identities.append("recovery:" + str(row.get("id") or ""))
        return "sha256:" + hashlib.sha256(
            json.dumps(sorted(identities), separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _rewrite_account_owner_value(
        cls,
        value: object,
        source: str,
        target: str,
        hash_map: Dict[str, str],
    ) -> object:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                new_key = hash_map.get(str(key), str(key))
                if key == "owner" and cls._account_owner(item) == source:
                    result[new_key] = target
                elif key in {
                    "content_hash", "head_hash", "audit_content_hash",
                    "published_hash", "bundle_sha256", "audit_bundle_sha256",
                } and str(item) in hash_map:
                    result[new_key] = hash_map[str(item)]
                else:
                    result[new_key] = cls._rewrite_account_owner_value(
                        item, source, target, hash_map
                    )
            return result
        if isinstance(value, list):
            return [
                cls._rewrite_account_owner_value(item, source, target, hash_map)
                for item in value
            ]
        return value

    def preview_owner_rename(
        self,
        source_owner: Optional[str],
        target_owner: Optional[str],
    ) -> Dict[str, object]:
        """Freeze the full skill authority before an account-key rename."""

        source = self._account_owner(source_owner)
        target = self._account_owner(target_owner)
        if not source or not target or source == target:
            raise ValueError("skill lifecycle owners must be distinct")
        lock_target = os.path.join(self.skills_root, "PROMOTIONS")
        with locked(lock_target):
            source_preview, source_plan = self._owner_purge_snapshot(source)
            target_preview, target_plan = self._owner_purge_snapshot(target)
            if any(int(value or 0) for value in target_preview["counts"].values()):
                raise ValueError("skill lifecycle target already contains state")
            return {
                "schema_version": 1,
                "source": source_preview,
                "target": target_preview,
                "identity_fingerprint": self._account_identity_fingerprint(
                    source_plan["inventory"]
                ),
            }

    def _account_owner_changes(
        self,
        source: str,
        target: str,
    ) -> list[AtomicFileChange]:
        changes: dict[str, AtomicFileChange] = {}
        hash_map: Dict[str, str] = {}

        def add(path: str, data: bytes, *, expected=None, missing=False):
            changes[os.path.abspath(path)] = AtomicFileChange(
                path=path,
                data=data,
                expected_fingerprint=expected,
                require_missing=missing,
            )

        for skill_path in self._iter_skill_files():
            head = self._read_skill(skill_path)
            if head is None or self._account_owner(head.owner) != source:
                continue
            skill_dir = Path(skill_path).parent
            state_file = skill_dir / "_lifecycle.json"
            try:
                state_raw = state_file.read_bytes()
                state = json.loads(state_raw)
            except FileNotFoundError:
                state_raw, state = None, {}
            except (OSError, UnicodeError, ValueError) as exc:
                raise ValueError("skill lifecycle state is unreadable") from exc
            if not isinstance(state, dict):
                raise ValueError("skill lifecycle state is malformed")

            proposed: dict[str, bytes] = {}
            for markdown in sorted(skill_dir.rglob("*.md")):
                if markdown.is_symlink() or not markdown.is_file():
                    raise ValueError("skill lifecycle tree is unsafe")
                raw = markdown.read_bytes()
                try:
                    skill = Skill.from_markdown(raw.decode("utf-8"), path=str(markdown))
                except (UnicodeError, ValueError) as exc:
                    raise ValueError("skill lifecycle markdown is unreadable") from exc
                if self._account_owner(skill.owner) != source:
                    continue
                declared = str(skill.content_hash or "")
                skill.owner = target
                rendered = skill.to_markdown().encode("utf-8")
                if declared:
                    hash_map[declared] = skill.content_hash
                proposed[str(markdown)] = rendered

            rewritten = self._rewrite_account_owner_value(
                state, source, target, hash_map
            )
            if not isinstance(rewritten, dict):
                raise ValueError("skill lifecycle rewrite failed")
            history = rewritten.get("history") or []
            for row in history:
                if not isinstance(row, dict):
                    continue
                old_relative = str(row.get("snapshot") or "")
                old_snapshot = skill_dir / old_relative
                old_bytes = proposed.get(str(old_snapshot))
                if old_bytes is None:
                    continue
                parsed = Skill.from_markdown(old_bytes.decode("utf-8"))
                new_relative = os.path.join(
                    "_revisions",
                    f"{int(parsed.revision):08d}-{parsed.content_hash}.md",
                )
                new_snapshot = skill_dir / new_relative
                row["snapshot"] = new_relative.replace(os.sep, "/")
                row["content_hash"] = parsed.content_hash
                row["owner"] = target
                row["snapshot_sha256"] = hashlib.sha256(old_bytes).hexdigest()
                if new_snapshot != old_snapshot:
                    add(
                        str(new_snapshot),
                        old_bytes,
                        missing=not new_snapshot.exists(),
                        expected=(
                            file_fingerprint(str(new_snapshot))
                            if new_snapshot.exists()
                            else None
                        ),
                    )
                    changes[str(old_snapshot.resolve())] = AtomicFileChange(
                        str(old_snapshot),
                        None,
                        expected_fingerprint=file_fingerprint(str(old_snapshot)),
                    )
                    proposed.pop(str(old_snapshot), None)

                bundle_root = str(row.get("bundle_root") or "")
                if bundle_root:
                    bundle = skill_dir / bundle_root
                    bundle_skill = bundle / "SKILL.md"
                    bundle_bytes = proposed.get(str(bundle_skill))
                    manifest_path = bundle / "_manifest.json"
                    if bundle_bytes is not None and manifest_path.is_file():
                        manifest_raw = manifest_path.read_bytes()
                        manifest = json.loads(manifest_raw)
                        manifest["files"]["SKILL.md"] = {
                            **manifest["files"]["SKILL.md"],
                            "sha256": hashlib.sha256(bundle_bytes).hexdigest(),
                            "size": len(bundle_bytes),
                        }
                        manifest_bytes = json.dumps(
                            manifest,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ).encode("utf-8")
                        digest = hashlib.sha256(manifest_bytes).hexdigest()
                        previous_digest = str(row.get("bundle_sha256") or "")
                        if previous_digest:
                            hash_map[previous_digest] = digest
                        row["bundle_sha256"] = digest
                        add(
                            str(manifest_path),
                            manifest_bytes,
                            expected=file_fingerprint(str(manifest_path)),
                        )

            published = rewritten.get("published")
            if isinstance(published, dict):
                published_hash = hash_map.get(
                    str(published.get("content_hash") or ""),
                    str(published.get("content_hash") or ""),
                )
                matching = next(
                    (
                        row
                        for row in history
                        if isinstance(row, dict)
                        and row.get("revision") == published.get("revision")
                        and row.get("content_hash") == published_hash
                    ),
                    None,
                )
                if matching is not None:
                    for key in (
                        "content_hash", "snapshot", "snapshot_sha256",
                        "bundle_root", "bundle_manifest", "bundle_sha256",
                        "bundle_version",
                    ):
                        if key in matching:
                            published[key] = matching[key]

            # Apply the final hash/owner mapping after history and bundle
            # descriptors have been derived from rewritten markdown.
            rewritten = self._rewrite_account_owner_value(
                rewritten, source, target, hash_map
            )
            for path, data in proposed.items():
                add(path, data, expected=file_fingerprint(path))
            if state_raw is not None:
                add(
                    str(state_file),
                    self._purge_json_bytes(rewritten),
                    expected=file_fingerprint(str(state_file)),
                )

        def shared_json(path: str, expected_type: type, transform):
            value, raw = self._purge_read_json(path, expected_type, expected_type())
            if raw is None:
                return
            updated = transform(value)
            if updated != value:
                add(path, self._purge_json_bytes(updated), expected=file_fingerprint(path))

        shared_json(
            self.usage_file,
            dict,
            lambda rows: {
                (
                    target + "::" + str(key).partition("::")[2]
                    if str(key).partition("::")[1]
                    and self._account_owner(str(key).partition("::")[0]) == source
                    else str(key)
                ): self._rewrite_account_owner_value(value, source, target, hash_map)
                for key, value in rows.items()
            },
        )
        shared_json(
            self.promotions_file,
            list,
            lambda rows: self._rewrite_account_owner_value(
                rows, source, target, hash_map
            ),
        )
        shared_json(
            self.legacy_file,
            list,
            lambda rows: self._rewrite_account_owner_value(
                rows, source, target, hash_map
            ),
        )
        audit_path = os.path.join(self.data_dir, "skill-audit-jobs.json")
        def audit_transform(rows):
            updated = self._rewrite_account_owner_value(rows, source, target, hash_map)
            for values in updated.values():
                for row in values:
                    key = row.get("key") if isinstance(row, dict) else None
                    if isinstance(key, list) and key and self._account_owner(key[0]) == source:
                        key[0] = target
            return updated
        shared_json(audit_path, dict, audit_transform)

        events_path = os.path.join(self.skills_root, "_usage_events.jsonl")
        if os.path.isfile(events_path):
            raw = Path(events_path).read_bytes()
            rows = []
            for line in raw.splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                rows.append(self._rewrite_account_owner_value(row, source, target, hash_map))
            output = b"".join(
                json.dumps(row, ensure_ascii=False).encode("utf-8") + b"\n"
                for row in rows
            )
            if output != raw:
                add(events_path, output, expected=file_fingerprint(events_path))

        recovery_root = Path(self.data_dir) / ".memory-forget-skills"
        if recovery_root.is_dir():
            for manifest_path in recovery_root.glob("*/manifest.json"):
                value, raw = self._purge_read_json(str(manifest_path), dict, {})
                if raw is None or self._account_owner(value.get("owner")) != source:
                    continue
                updated = self._rewrite_account_owner_value(
                    value, source, target, hash_map
                )
                add(
                    str(manifest_path),
                    self._purge_json_bytes(updated),
                    expected=file_fingerprint(str(manifest_path)),
                )
        return sorted(changes.values(), key=lambda change: change.path)

    def reconcile_owner_rename(
        self,
        source_owner: Optional[str],
        target_owner: Optional[str],
        manifest: Dict[str, object],
    ) -> Dict[str, object]:
        source = self._account_owner(source_owner)
        target = self._account_owner(target_owner)
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise ValueError("skill lifecycle manifest is invalid")
        lock_target = os.path.join(self.skills_root, "PROMOTIONS")
        with locked(lock_target):
            current_source, source_plan = self._owner_purge_snapshot(source)
            current_target, target_plan = self._owner_purge_snapshot(target)
            expected = manifest.get("source") or {}
            if any(int(value or 0) for value in current_source["counts"].values()):
                if (
                    current_source.get("fingerprint") != expected.get("fingerprint")
                    or current_source.get("counts") != expected.get("counts")
                ):
                    raise ValueError("skill lifecycle source changed after preflight")
                if any(int(value or 0) for value in current_target["counts"].values()):
                    raise ValueError("skill lifecycle source and target both contain state")
                changes = self._account_owner_changes(source, target)
                if changes:
                    atomic_write_batch(changes)
                current_source, source_plan = self._owner_purge_snapshot(source)
                current_target, target_plan = self._owner_purge_snapshot(target)
            if any(int(value or 0) for value in current_source["counts"].values()):
                raise ValueError("skill lifecycle source remains after rename")
            if self._account_identity_fingerprint(target_plan["inventory"]) != manifest.get(
                "identity_fingerprint"
            ):
                raise ValueError("skill lifecycle target conflicts with preflight")
            return {
                "state": "staged",
                "count": int(expected.get("count") or 0),
                "source": current_source,
                "target": current_target,
            }

    def compensate_owner_rename(
        self,
        source_owner: Optional[str],
        target_owner: Optional[str],
        manifest: Dict[str, object],
    ) -> Dict[str, object]:
        target_preview, target_plan = self._owner_purge_snapshot(
            self._account_owner(target_owner)
        )
        reverse = {
            "schema_version": 1,
            "source": target_preview,
            "target": self._owner_purge_snapshot(
                self._account_owner(source_owner)
            )[0],
            "identity_fingerprint": self._account_identity_fingerprint(
                target_plan["inventory"]
            ),
        }
        receipt = self.reconcile_owner_rename(target_owner, source_owner, reverse)
        return {**receipt, "state": "restored"}

    @staticmethod
    def _purge_builtin(value: object) -> bool:
        if isinstance(value, Skill):
            source = value.source
            marked = False
        elif isinstance(value, dict):
            source = value.get("source")
            marked = bool(value.get("builtin") or value.get("built_in"))
        else:
            return False
        return marked or str(source or "").strip().lower() in {
            "builtin", "built-in",
        }

    @staticmethod
    def _purge_json_bytes(value: object) -> bytes:
        return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")

    @staticmethod
    def _purge_read_json(path: str, expected_type: type, empty):
        if os.path.islink(path):
            raise ValueError("skill reset state cannot be a symlink")
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except FileNotFoundError:
            return empty, None
        except OSError as exc:
            raise ValueError("skill reset state is unavailable") from exc
        try:
            value = json.loads(raw)
        except (UnicodeError, ValueError) as exc:
            raise ValueError("skill reset state is unreadable") from exc
        if not isinstance(value, expected_type):
            raise ValueError("skill reset state has an invalid shape")
        return value, raw

    @staticmethod
    def _purge_tree_digest(path: str) -> str:
        base = os.path.abspath(path)
        if os.path.islink(base) or not os.path.isdir(base):
            raise ValueError("skill reset tree is unsafe")
        digest = hashlib.sha256()
        for current, dirs, files in os.walk(base, topdown=True, followlinks=False):
            dirs.sort()
            files.sort()
            for name in dirs:
                if os.path.islink(os.path.join(current, name)):
                    raise ValueError("skill reset tree contains a symlink")
            for name in files:
                item = os.path.join(current, name)
                if os.path.islink(item) or not os.path.isfile(item):
                    raise ValueError("skill reset tree contains a non-regular file")
                relative = os.path.relpath(item, base).replace(os.sep, "/")
                try:
                    with open(item, "rb") as handle:
                        content = handle.read()
                except OSError as exc:
                    raise ValueError("skill reset tree is unreadable") from exc
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                digest.update(hashlib.sha256(content).digest())
                digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _usage_belongs_to_owner(key: object, row: object, owner: str) -> bool:
        key = str(key)
        explicit = (
            str(row.get("owner") or "")
            if isinstance(row, dict) and "owner" in row
            else None
        )
        if owner:
            owner_key = str(owner).strip().lower()
            key_owner, separator, _suffix = key.partition("::")
            return (
                separator and key_owner.strip().lower() == owner_key
            ) or (
                explicit is not None and explicit.strip().lower() == owner_key
            )
        return "::" not in key and explicit in (None, "")

    def _owner_purge_snapshot(self, owner: str) -> tuple[dict, dict]:
        owner = str(owner or "")
        root = os.path.abspath(self.skills_root)
        if os.path.islink(root):
            raise ValueError("skills root cannot be a symlink")

        skill_rows: list[dict] = []
        skill_dirs: list[str] = []
        for path in self._iter_skill_files():
            skill = self._read_skill(path)
            if skill is None:
                raise ValueError("a skill identity is unreadable")
            if (
                self._account_owner(skill.owner) != self._account_owner(owner)
                or self._purge_builtin(skill)
            ):
                continue
            skill_dir = os.path.abspath(os.path.dirname(path))
            if os.path.commonpath((root, skill_dir)) != root:
                raise ValueError("skill reset path escaped its root")
            skill_dirs.append(skill_dir)
            skill_rows.append({
                "skill_id": skill.skill_id,
                "name": skill.name,
                "category": skill.category,
                "revision": skill.revision,
                "content_hash": skill.content_hash,
                "tree_sha256": self._purge_tree_digest(skill_dir),
            })

        usage, usage_raw = self._purge_read_json(self.usage_file, dict, {})
        usage_keys = sorted(
            str(key) for key, row in usage.items()
            if self._usage_belongs_to_owner(key, row, owner)
        )
        kept_usage = {
            key: row for key, row in usage.items()
            if not self._usage_belongs_to_owner(key, row, owner)
        }

        promotions, promotions_raw = self._purge_read_json(
            self.promotions_file, list, []
        )
        owned_promotions = [
            row for row in promotions
            if isinstance(row, dict)
            and self._account_owner(row.get("owner")) == self._account_owner(owner)
        ]
        kept_promotions = [
            row for row in promotions
            if not (
                isinstance(row, dict)
                and self._account_owner(row.get("owner"))
                == self._account_owner(owner)
            )
        ]

        events_path = os.path.join(self.skills_root, "_usage_events.jsonl")
        if os.path.islink(events_path):
            raise ValueError("skill usage events cannot be a symlink")
        try:
            with open(events_path, "rb") as handle:
                events_raw = handle.read()
        except FileNotFoundError:
            events_raw = None
        except OSError as exc:
            raise ValueError("skill usage events are unavailable") from exc
        owned_events: list[dict] = []
        kept_event_lines: list[bytes] = []
        if events_raw is not None:
            for raw_line in events_raw.splitlines(keepends=True):
                if not raw_line.strip():
                    kept_event_lines.append(raw_line)
                    continue
                try:
                    event = json.loads(raw_line)
                except (UnicodeError, ValueError) as exc:
                    raise ValueError("skill usage events are unreadable") from exc
                if not isinstance(event, dict):
                    raise ValueError("skill usage events have an invalid shape")
                if self._account_owner(event.get("owner")) == self._account_owner(owner):
                    owned_events.append(event)
                else:
                    kept_event_lines.append(raw_line)

        legacy, legacy_raw = self._purge_read_json(self.legacy_file, list, [])
        owned_legacy = [
            row for row in legacy
            if isinstance(row, dict)
            and self._account_owner(row.get("owner")) == self._account_owner(owner)
            and not self._purge_builtin(row)
        ]
        kept_legacy = [row for row in legacy if row not in owned_legacy]

        audit_jobs_path = os.path.join(self.data_dir, "skill-audit-jobs.json")
        audit_jobs, audit_jobs_raw = self._purge_read_json(
            audit_jobs_path, dict, {}
        )
        owned_jobs: list[dict] = []
        kept_jobs: dict[str, object] = {}
        for kind, rows in audit_jobs.items():
            if not isinstance(kind, str) or not isinstance(rows, list):
                raise ValueError("skill audit job state has an invalid shape")
            kept_rows = []
            for row in rows:
                key = row.get("key") if isinstance(row, dict) else None
                if (
                    isinstance(key, list)
                    and key
                    and self._account_owner(key[0]) == self._account_owner(owner)
                ):
                    owned_jobs.append({"kind": kind, "row": row})
                else:
                    kept_rows.append(row)
            kept_jobs[kind] = kept_rows

        recovery_dirs: list[str] = []
        recoveries: list[dict] = []
        recovery_root = os.path.join(os.path.abspath(self.data_dir), ".memory-forget-skills")
        if os.path.islink(recovery_root):
            raise ValueError("skill recovery root cannot be a symlink")
        if os.path.isdir(recovery_root):
            for entry in sorted(os.scandir(recovery_root), key=lambda item: item.name):
                if entry.is_symlink():
                    raise ValueError("skill recovery state contains a symlink")
                if not entry.is_dir(follow_symlinks=False):
                    continue
                manifest, manifest_raw = self._purge_read_json(
                    os.path.join(entry.path, "manifest.json"), dict, {}
                )
                if manifest_raw is None:
                    raise ValueError("skill recovery manifest is missing")
                if self._account_owner(manifest.get("owner")) != self._account_owner(owner):
                    continue
                recovery_dirs.append(entry.path)
                recoveries.append({
                    "id": entry.name,
                    "state": manifest.get("state"),
                    "tree_sha256": self._purge_tree_digest(entry.path),
                })

        inventory = {
            "owner": owner,
            "skills": sorted(skill_rows, key=lambda row: row["skill_id"]),
            "usage_keys": usage_keys,
            "promotions": owned_promotions,
            "events": owned_events,
            "legacy": owned_legacy,
            "audit_jobs": owned_jobs,
            "recoveries": recoveries,
        }
        counts = {
            "skills": len(skill_rows),
            "legacy": len(owned_legacy),
            "usage": len(usage_keys),
            "promotions": len(owned_promotions),
            "events": len(owned_events),
            "audit_jobs": len(owned_jobs),
            "recovery_manifests": len(recoveries),
        }
        preview = {
            "count": counts["skills"] + counts["legacy"],
            "fingerprint": "sha256:" + hashlib.sha256(
                json.dumps(
                    inventory,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
            "counts": counts,
        }

        changes: list[AtomicFileChange] = []
        candidates = (
            (self.usage_file, usage_raw, self._purge_json_bytes(kept_usage)),
            (self.promotions_file, promotions_raw, self._purge_json_bytes(kept_promotions)),
            (events_path, events_raw, b"".join(kept_event_lines)),
            (self.legacy_file, legacy_raw, self._purge_json_bytes(kept_legacy)),
            (audit_jobs_path, audit_jobs_raw, self._purge_json_bytes(kept_jobs)),
        )
        for path, raw, replacement in candidates:
            if raw is not None and raw != replacement:
                changes.append(AtomicFileChange(
                    path=path,
                    data=replacement,
                    expected_fingerprint=file_fingerprint(path),
                ))
        return preview, {
            "inventory": inventory,
            "skill_dirs": skill_dirs,
            "recovery_dirs": recovery_dirs,
            "changes": changes,
        }

    def preview_owner_purge(self, owner: Optional[str]) -> Dict[str, object]:
        """Preview every live skill artifact belonging to exactly one owner."""
        lock_target = os.path.join(self.skills_root, "PROMOTIONS")
        with locked(lock_target):
            preview, _plan = self._owner_purge_snapshot(str(owner or ""))
            return preview

    def purge_owner(
        self,
        owner: Optional[str],
        *,
        expected: Dict[str, object],
    ) -> Dict[str, object]:
        """CAS-purge one owner's skill trees and owner-indexed sidecars."""
        lock_target = os.path.join(self.skills_root, "PROMOTIONS")
        with locked(lock_target):
            preview, plan = self._owner_purge_snapshot(str(owner or ""))
            if (
                not isinstance(expected, dict)
                or expected.get("count") != preview["count"]
                or expected.get("fingerprint") != preview["fingerprint"]
            ):
                raise ValueError("skill owner purge preview is stale")

            moved: list[tuple[str, str]] = []
            try:
                for source in plan["skill_dirs"] + plan["recovery_dirs"]:
                    tombstone = os.path.join(
                        os.path.dirname(source),
                        f".owner-purge-{uuid.uuid4().hex}",
                    )
                    os.replace(source, tombstone)
                    moved.append((source, tombstone))
                if plan["changes"]:
                    atomic_write_batch(plan["changes"])
            except Exception:
                rollback_failures = []
                for source, tombstone in reversed(moved):
                    try:
                        os.replace(tombstone, source)
                    except OSError as restore_error:
                        rollback_failures.append(str(restore_error))
                if rollback_failures:
                    return {
                        "complete": False,
                        "count": 0,
                        "counts": preview["counts"],
                        "failures": ["skill reset rollback failed"],
                    }
                raise

            failures: list[str] = []
            for _source, tombstone in moved:
                try:
                    shutil.rmtree(tombstone)
                except OSError:
                    failures.append("a quarantined skill tree could not be erased")
            result = {
                "complete": not failures,
                "count": preview["count"],
                "counts": preview["counts"],
                "failures": failures,
            }
        if result["complete"]:
            from src.openclank.attachment_admission import refresh_domain_references
            from src.openclank.attachment_inventory import skills_inventory
            refresh_domain_references("skills", lambda: skills_inventory(self))
        return result

    def delete_skill(self, *args, **kwargs) -> bool:
        """Delete a skill under the global memory/skill lifecycle lock."""
        lock_target = os.path.join(self.skills_root, "PROMOTIONS")
        with locked(lock_target):
            result = self._delete_skill_unlocked(*args, **kwargs)
        if result:
            from src.openclank.attachment_admission import refresh_domain_references
            from src.openclank.attachment_inventory import skills_inventory
            refresh_domain_references("skills", lambda: skills_inventory(self))
        return result

    def _delete_skill_unlocked(
        self, skill_id: str, owner: Optional[str] = None
    ) -> bool:
        for path in self._iter_skill_files():
            sk = self._read_skill(path)
            if not sk or skill_id not in (sk.name, sk.skill_id):
                continue
            if (sk.owner or "") != (owner or ""):
                continue
            skill_dir = os.path.dirname(path)
            tombstone = os.path.join(
                self.skills_root,
                f".deleted-{sk.skill_id}-{uuid.uuid4().hex}",
            )
            try:
                with locked(path):
                    current = self._read_skill(path)
                    if not self._matches_locked_head(
                        current, sk, skill_id, owner
                    ):
                        return False
                    os.replace(skill_dir, tombstone)
            except Exception as e:
                logger.warning(f"Failed to remove skill dir {skill_dir}: {e}")
                return False
            # The lock inode moved with the directory. Remove it only after
            # releasing the lock so waiters can never split across two inodes.
            try:
                shutil.rmtree(tombstone)
            except Exception as e:
                logger.warning("Failed to clean deleted skill %s: %s", tombstone, e)
            usage = self._load_usage()
            usage_key = self._usage_key(sk.skill_id, sk.owner)
            if usage_key in usage:
                del usage[usage_key]
                self._save_usage(usage)
            return True
        return False

    def record_use(
        self,
        skill_id: str,
        owner: Optional[str] = None,
        *,
        revision: Optional[int] = None,
    ) -> None:
        self._record_event(skill_id, "use", owner, exact_revision=revision)

    def record_active_use(
        self,
        skill_id: str,
        owner: Optional[str],
        *,
        revision: int,
        content_hash: str,
    ) -> Optional[Dict]:
        """Record only an exact revision that is still actively published."""
        return self._record_event(
            skill_id,
            "use",
            owner,
            exact_revision=revision,
            exact_content_hash=content_hash,
            require_published=True,
        )

    def record_retrieval(
        self,
        skill_id: str,
        owner: Optional[str] = None,
        *,
        revision: Optional[int] = None,
    ) -> None:
        self._record_event(skill_id, "retrieval", owner, exact_revision=revision)

    def record_failure(self, skill_id: str, owner: Optional[str] = None) -> None:
        self._record_event(skill_id, "failure", owner)

    def record_success(self, skill_id: str, owner: Optional[str] = None) -> None:
        self._record_event(skill_id, "success", owner)

    def record_correction(
        self,
        skill_id: str,
        owner: Optional[str] = None,
        *,
        replacement_skill_id: str = "",
        demote: bool = True,
    ) -> None:
        self._record_event(
            skill_id,
            "correction",
            owner,
            replacement_skill_id=replacement_skill_id,
        )
        if demote:
            self.update_skill(skill_id, {"status": "draft"}, owner=owner)

    def record_contradiction(self, skill_id: str, owner: Optional[str] = None) -> None:
        self._record_event(skill_id, "contradiction", owner)

    def record_mismatch(self, skill_id: str, owner: Optional[str] = None) -> None:
        self._record_event(skill_id, "mismatch", owner)

    def _record_event(
        self,
        skill_id: str,
        event: str,
        owner: Optional[str],
        exact_revision: Optional[int] = None,
        exact_content_hash: Optional[str] = None,
        require_published: bool = False,
        **metadata,
    ) -> Optional[Dict]:
        found = self._find_skill(skill_id, owner)
        if not found:
            return None
        path, _ = found
        with locked(path):
            sk = self._read_skill(path)
            if (
                sk is None
                or (sk.owner or "") != (owner or "")
                or (sk.name != skill_id and sk.skill_id != skill_id)
            ):
                return None
            state = load_state(path)
            pointer = state.get("published")
            if isinstance(pointer, dict):
                active, _ = self._active_skill(path, sk, state)
                if active is None:
                    return None
                active_revision = active.revision
                active_hash = active.content_hash
            else:
                if require_published:
                    return None
                active_revision = sk.revision
                active_hash = sk.content_hash
            revision = int(
                exact_revision
                if exact_revision is not None
                else active_revision
            )
            if isinstance(pointer, dict) and revision != active_revision:
                logger.warning(
                    "Refusing usage event for inactive skill revision %s r%s",
                    sk.skill_id,
                    revision,
                )
                return None
            if (
                exact_content_hash is not None
                and exact_content_hash != active_hash
            ):
                logger.warning(
                    "Refusing usage event for inactive skill content %s r%s",
                    sk.skill_id,
                    revision,
                )
                return None
            known_revisions = {
                int(item.get("revision"))
                for item in state.get("history", [])
                if isinstance(item, dict) and item.get("revision") is not None
            }
            if known_revisions and revision not in known_revisions:
                logger.warning(
                    "Refusing usage event for unknown skill revision %s r%s",
                    sk.skill_id,
                    revision,
                )
                return None
            row = {
                "event": event,
                "owner": owner or "",
                "skill_id": sk.skill_id,
                "revision": revision,
                "timestamp": int(time.time()),
                "runtime": "python",
            }
            row.update({
                key: value
                for key, value in metadata.items()
                if value not in ("", None)
            })
            append_usage_event(self.skills_root, row)
            return {
                "ok": True,
                "name": active.name if isinstance(pointer, dict) else sk.name,
                "skill_id": sk.skill_id,
                "owner": owner or "",
                "revision": revision,
                "content_hash": active_hash,
            }

    # ----------------------------------------------------------------------
    # Reading a single skill (used by the skill_view tool)
    # ----------------------------------------------------------------------

    def read_skill_md(self, name: str, owner: Optional[str] = None) -> Optional[str]:
        for path in self._iter_skill_files():
            sk = self._read_skill(path)
            if not sk or sk.name != name:
                continue
            if (sk.owner or "") != (owner or ""):
                continue

            with locked(path):
                current = self._read_skill(path)
                if not self._matches_locked_head(current, sk, name, owner):
                    return None
                try:
                    with open(path, encoding="utf-8") as f:
                        return f.read()
                except Exception:
                    return None
        return None

    def read_published_skill_md(
        self, name: str, owner: Optional[str] = None
    ) -> Optional[str]:
        found = self._find_skill(name, owner)
        if not found:
            return None
        path, head = found
        with locked(path):
            current = self._read_skill(path)
            if not self._matches_locked_head(current, head, name, owner):
                return None
            assert current is not None
            active, pointer = self._active_skill(path, current)
            if active is None or not isinstance(pointer, dict):
                return None
            return read_bundle_file(path, pointer, "SKILL.md")

    def load_published(self, owner: Optional[str] = None) -> List[Dict]:
        """Return the immutable revisions selected by each published pointer."""
        out: List[Dict] = []
        metadata = {row.get("skill_id"): row for row in self.load(owner=owner)}
        for path in self._iter_skill_files():
            head = self._read_skill(path)
            if not head or (head.owner or "") != (owner or ""):
                continue
            with locked(path):
                current = self._read_skill(path)
                if not self._matches_locked_head(
                    current, head, head.name, owner
                ):
                    continue
                assert current is not None
                state = load_state(path)
                active, pointer = self._active_skill(path, current, state)
                if active is None:
                    continue
                row = active.to_dict()
                row["head_revision"] = current.revision
                attestation = (
                    state.get("attestations", {}).get(active.content_hash, {})
                    if isinstance(state.get("attestations"), dict)
                    else {}
                )
            row.update(
                {
                    key: value
                    for key, value in metadata.get(current.skill_id, {}).items()
                    if key in (
                        "uses", "retrievals", "successes", "failures",
                        "corrections", "contradictions", "mismatches",
                        "last_used", "last_retrieved", "audit_verdict",
                        "audited_at", "audit_evaluator", "audit_results",
                        "last_audit", "trust", "provenance",
                    )
                }
            )
            row["published_revision"] = active.revision
            row["published_hash"] = active.content_hash
            row["active"] = True
            row["trust"] = (
                "waived"
                if isinstance(pointer, dict) and pointer.get("waiver")
                else "verified"
                if attestation.get("verdict") == "pass"
                and attestation.get("compatible")
                else "published"
            )
            out.append(row)
        return out

    def read_skill_reference(self, name: str, ref_path: str, owner: Optional[str] = None) -> Optional[str]:
        """Read a sub-file under the skill's directory (references/, etc).
        Refuses path traversal."""
        for path in self._iter_skill_files():
            sk = self._read_skill(path)
            if not sk or sk.name != name:
                continue
            if (sk.owner or "") != (owner or ""):
                continue
            with locked(path):
                current = self._read_skill(path)
                if not self._matches_locked_head(current, sk, name, owner):
                    return None
                return self._read_skill_reference_path(path, ref_path)
        return None

    def read_published_skill_reference(
        self,
        name: str,
        ref_path: str,
        owner: Optional[str] = None,
    ) -> Optional[str]:
        found = self._find_skill(name, owner)
        if not found:
            return None
        path, expected = found
        with locked(path):
            head = self._read_skill(path)
            if not self._matches_locked_head(head, expected, name, owner):
                return None
            assert head is not None
            active, pointer = self._active_skill(path, head)
            if active is None or not isinstance(pointer, dict):
                return None
            return read_bundle_file(path, pointer, ref_path)

    @staticmethod
    def _read_skill_reference_path(path: str, ref_path: str) -> Optional[str]:
        base = os.path.realpath(os.path.dirname(path))
        target = os.path.realpath(os.path.join(base, ref_path))
        if os.path.commonpath([base, target]) != base or target == base:
            return None
        if not os.path.isfile(target):
            return None
        try:
            with open(target, encoding="utf-8") as handle:
                return handle.read()
        except Exception:
            return None

    # ----------------------------------------------------------------------
    # Index — the lightweight summary injected into the system prompt
    # ----------------------------------------------------------------------

    def index_for(
        self,
        owner: Optional[str] = None,
        *,
        active_toolsets: Optional[List[str]] = None,
        platform: Optional[str] = None,
    ) -> List[Dict]:
        """Return the `[{name, description, category, status}]` list the
        agent sees in its system prompt.

        Only explicitly published skills are active. Generated, teacher,
        imported, and migrated drafts remain staged until an authorized local
        user/admin publishes the exact audited revision.
        """
        out = []
        for s in self.load_published(owner=owner):
            status = s.get("status")
            if status != "published":
                continue
            # Platform gating
            if platform and s.get("platforms") and platform not in s["platforms"]:
                continue
            # requires_toolsets: hide unless every required toolset is active.
            # active_toolsets=None means the caller doesn't know the active
            # set (API listings, chat preface) — don't gate in that case;
            # only an explicit list filters.
            req = s.get("requires_toolsets") or []
            if req and active_toolsets is not None and not all(t in active_toolsets for t in req):
                continue
            # fallback_for_toolsets: hide when any of those toolsets is active
            fb = s.get("fallback_for_toolsets") or []
            if fb and active_toolsets and any(t in active_toolsets for t in fb):
                continue
            out.append({
                "id": s.get("skill_id") or s.get("id"),
                "skill_id": s.get("skill_id") or s.get("id"),
                "revision": s.get("revision"),
                "content_hash": s.get("content_hash"),
                "name": s["name"],
                "description": s.get("description") or s.get("title", ""),
                "category": s.get("category", "general"),
                "status": status,
                "owner": s.get("owner"),
                "source": s.get("source"),
                "source_status": s.get("source_status"),
                "source_revision": s.get("source_revision"),
                "trust": s.get("trust"),
                "audited_at": s.get("audited_at"),
                "audit_verdict": s.get("audit_verdict"),
                "audit_evaluator": s.get("audit_evaluator"),
                "platforms": s.get("platforms") or [],
                "requires_toolsets": s.get("requires_toolsets") or [],
            })
        out.sort(key=lambda x: (x["category"], x["name"]))
        return out

    # ----------------------------------------------------------------------
    # Relevance search (kept for the existing /api/skills/search endpoint
    # and the `manage_skills` action="search"). Now operates on the new
    # field set.
    # ----------------------------------------------------------------------

    def get_relevant_skills(
        self,
        query: str,
        skills: Optional[List[Dict]] = None,
        threshold: float = 0.3,
        max_items: int = 5,
        min_confidence: float = 0.0,
    ) -> List[Dict]:
        if skills is None:
            skills = self.load_published()
        if not skills or not query.strip():
            return []
        skills = [s for s in skills if s.get("status") == "published"]
        if not skills:
            return []

        query_tokens = _tokenize(query)
        scored = []
        for sk in skills:
            text = " ".join([
                sk.get("name", ""),
                sk.get("description", ""),
                sk.get("when_to_use", ""),
                " ".join(sk.get("tags", []) or []),
                " ".join(sk.get("procedure", []) or []),
            ])
            score = _jaccard(query_tokens, _tokenize(text))
            for tag in sk.get("tags", []) or []:
                # Match tags as whole tokens, not substrings: `tag in query`
                # boosted e.g. a "ai" tag for any query containing "email".
                tag_tokens = _tokenize(tag)
                if tag_tokens and tag_tokens <= query_tokens:
                    score = max(score, 0.3) * 1.3
            if query.lower() in (sk.get("description") or "").lower():
                score = max(score, 0.6)
            score *= 1.0 + _to_float(sk.get("confidence"), 0.5) * 0.1
            if sk.get("uses", 0) > 0:
                score *= 1.05
            if score >= threshold:
                scored.append((score, sk))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [sk for _, sk in scored[:max_items]]
