"""Canonical first-party paths for plans, Hexes, and Clanker notes.

This module is deliberately small and dependency-free.  It is the single path
vocabulary used by plan persistence and policy discovery while the on-disk
corpora are migrated.  Legacy paths are accepted only as bounded read/migration
inputs; callers must use the returned canonical path for every new write.
"""

from __future__ import annotations

import re
from pathlib import Path


CLANKER_FUTURES_DIR = ".clanker/futures"
CLANKERS_ROOT = ".clankers"
CLANKERS_HEXES_DIR = ".clankers/hexes"
CLANKERS_ROBONOTES_DIR = ".clankers/robonotes"
GLOBAL_HEX_CONTRACT = ".clankers/hexes/contract.yaml"

LEGACY_FUTURES_DIR = ".futures"
LEGACY_DOT_ROBONOTES_DIR = ".robonotes"
LEGACY_ROBONOTES_DIR = "robonotes"

TYPO_NAMESPACES = frozenset(
    {
        ".clankers/futures",
        ".clanker/hexes",
        ".clanker/robonotes",
    }
)

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ClankerPathError(ValueError):
    """A path is not a safe project-relative Clanker artifact path."""


def _validate_relative_posix(value: str) -> str:
    if not isinstance(value, str):
        raise ClankerPathError("path must be a string")
    value = value.strip()
    if not value:
        raise ClankerPathError("path is required")
    if "\\" in value:
        raise ClankerPathError("alternate path separators are not allowed")
    if value.startswith("/") or value.startswith("//"):
        raise ClankerPathError("absolute paths are not allowed")
    if re.match(r"^[A-Za-z]:", value):
        raise ClankerPathError("drive-qualified paths are not allowed")
    if _CONTROL_RE.search(value):
        raise ClankerPathError("control characters are not allowed")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ClankerPathError("empty, current-directory, and traversal segments are not allowed")
    return value


def _suffix_for(value: str, prefixes: tuple[str, ...]) -> tuple[str, str] | None:
    for prefix in prefixes:
        marker = prefix + "/"
        if value.startswith(marker):
            suffix = value[len(marker):]
            if suffix:
                return prefix, suffix
    return None


def canonical_plan_path(relative_path: str, *, allow_legacy: bool = True) -> str:
    """Return a safe canonical plan path.

    ``.futures/<suffix>`` is a compatibility input when ``allow_legacy`` is
    true.  Canonical output is always ``.clanker/futures/<suffix>``.
    """
    value = _validate_relative_posix(relative_path)
    if value in TYPO_NAMESPACES:
        raise ClankerPathError("invalid singular/plural Clanker namespace")
    canonical = _suffix_for(value, (CLANKER_FUTURES_DIR,))
    if canonical:
        return value
    legacy = _suffix_for(value, (LEGACY_FUTURES_DIR,)) if allow_legacy else None
    if legacy:
        return f"{CLANKER_FUTURES_DIR}/{legacy[1]}"
    if value.startswith((".clanker/", ".clankers/", ".futures/")):
        raise ClankerPathError("path is not a valid canonical plan namespace")
    raise ClankerPathError("plan artifact must be under the canonical futures namespace")


def canonical_robonote_path(relative_path: str, *, allow_legacy: bool = True) -> str:
    """Return a safe canonical Clanker robonote/evidence path."""
    value = _validate_relative_posix(relative_path)
    if value in TYPO_NAMESPACES:
        raise ClankerPathError("invalid singular/plural Clanker namespace")
    canonical = _suffix_for(value, (CLANKERS_ROBONOTES_DIR,))
    if canonical:
        return value
    legacy = _suffix_for(
        value,
        (LEGACY_DOT_ROBONOTES_DIR, LEGACY_ROBONOTES_DIR),
    ) if allow_legacy else None
    if legacy:
        return f"{CLANKERS_ROBONOTES_DIR}/{legacy[1]}"
    if value.startswith((".clanker/", ".clankers/", ".robonotes/", "robonotes/")):
        raise ClankerPathError("path is not a valid canonical robonote namespace")
    raise ClankerPathError("robonote must be under the canonical Clanker namespace")


def is_legacy_plan_path(relative_path: str) -> bool:
    try:
        value = _validate_relative_posix(relative_path)
    except ClankerPathError:
        return False
    return _suffix_for(value, (LEGACY_FUTURES_DIR,)) is not None


def project_root_for_contract(contract_path: str | Path) -> Path:
    """Return the project root for either a canonical or legacy contract file."""
    path = Path(contract_path).resolve()
    parts = path.parts
    canonical_parent = Path(CLANKERS_HEXES_DIR).parts
    for index in range(len(parts) - len(canonical_parent) + 1):
        if tuple(parts[index:index + len(canonical_parent)]) == canonical_parent:
            return Path(*parts[:index])
    return path.parent


def canonical_relative_path(relative_path: str, *, allow_legacy: bool = True) -> str:
    """Canonicalize a plan or robonote path, rejecting mixed namespaces."""
    value = _validate_relative_posix(relative_path)
    if _suffix_for(value, (CLANKER_FUTURES_DIR,)) or _suffix_for(value, (LEGACY_FUTURES_DIR,)):
        return canonical_plan_path(value, allow_legacy=allow_legacy)
    if _suffix_for(value, (CLANKERS_ROBONOTES_DIR,)) or _suffix_for(
        value, (LEGACY_DOT_ROBONOTES_DIR, LEGACY_ROBONOTES_DIR)
    ):
        return canonical_robonote_path(value, allow_legacy=allow_legacy)
    raise ClankerPathError("unsupported Clanker artifact namespace")


__all__ = [
    "CLANKER_FUTURES_DIR",
    "CLANKERS_ROOT",
    "CLANKERS_HEXES_DIR",
    "CLANKERS_ROBONOTES_DIR",
    "GLOBAL_HEX_CONTRACT",
    "LEGACY_FUTURES_DIR",
    "LEGACY_DOT_ROBONOTES_DIR",
    "LEGACY_ROBONOTES_DIR",
    "ClankerPathError",
    "canonical_plan_path",
    "canonical_robonote_path",
    "canonical_relative_path",
    "is_legacy_plan_path",
    "project_root_for_contract",
]
