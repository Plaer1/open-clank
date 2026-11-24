"""Conservative defaults for a workspace without an existing Hex contract."""

from __future__ import annotations

from pathlib import Path

import yaml

from core.atomic_io import atomic_write_text
from src.clanker_paths import CLANKER_REFERENCES_DIR, GLOBAL_HEX_CONTRACT
from src.hex_contract import HexContractError, find_contract


def default_contract() -> dict:
    """Fresh editable rules; project-specific checks do not belong here."""
    return {
        "contract": "open-clank-hexes/v2",
        "hexes": [
            {
                "hex": "Retired plans, Clanker artifacts and explicitly archived material live in .clanker/archive/; preserve their workspace-relative structure and link to replacements when known.",
                "in": "./.clanker/archive/*",
                "why": "Archive deliberately; preserve provenance and do not delete old material automatically.",
            },
            {
                "hex": "Current Markdown metaplans live in .clanker/futures/<metaplan>.md with their slices and subfolders below the same root.",
                "in": "./.clanker/futures/*",
                "allowed_filetypes": ".md",
            },
            {
                "hex": "Markdown sidecar notes live in .clanker/robonotes/ and mirror the relevant workspace path; cross-cutting notes use a focused topic folder.",
                "in": "./.clanker/robonotes/*",
                "allowed_filetypes": ".md",
                "why": "Create notes on demand, not an empty copy of the workspace tree. Keep evidence beside its subject instead of a flat pile.",
            },
            {
                "hex": "Plans and robonotes keep concise navigation indexes linking coherent slices, current decisions, next steps and related plans; read only the relevant slices.",
                "in": ["./.clanker/futures/*", "./.clanker/robonotes/*"],
                "why": "Split at meaningful conceptual boundaries without arbitrary size limits; summarize evidence instead of copying raw logs.",
            },
            {
                "hex": "Workspace Hexes live in .clanker/hexes/contract.yaml with optional checks/; edit the contract and regenerate its AGENTS.md digest with openclank hex sync.",
                "in": "./.clanker/hexes/*",
                "why": "Hex contracts remain trackable. Existing plural-path data is a compatibility input; new writes use .clanker/.",
            },
            {
                "hex": "Explicit operator-run workspace tools and migrations live in .clanker/tools/, with a concise index explaining targets and invocation.",
                "in": "./.clanker/tools/*",
                "why": "Discovery, startup and Hex sync never execute these tools. Existing-data conversion is an explicit operator action; the app owns only current-schema initialization and ordinary runtime work.",
            },
            {
                "hex": "Reference and inspiration material belongs in .clanker/references/ and must never be tracked by Git, including force-added files or reference submodules.",
                "untracked_only": [CLANKER_REFERENCES_DIR, ".references"],
            },
        ],
    }


def initialize_defaults(workspace: str | Path) -> tuple[Path, bool]:
    """Seed an unconfigured workspace; never replace a discovered contract.

    This creates policy text, not an activation or executable-trust record.
    A surrounding workspace's contract also counts as existing policy.
    """
    root = Path(workspace).resolve(strict=True)
    if not root.is_dir():
        raise HexContractError("workspace must be a directory")
    existing = find_contract(root)
    if existing is not None:
        return existing, False
    ignore = root / ".gitignore"
    text = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    # Append after any negations; Git ignores alone cannot prevent git add -f.
    rule = f"/{CLANKER_REFERENCES_DIR}/"
    if not text.splitlines() or text.splitlines()[-1] != rule:
        atomic_write_text(str(ignore), text + ("\n" if text and not text.endswith("\n") else "") + rule + "\n")
    target = root / GLOBAL_HEX_CONTRACT
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as handle:
        handle.write(yaml.safe_dump(default_contract(), sort_keys=False, allow_unicode=True))
    return target, True
