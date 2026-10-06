"""Evaluate one trusted Open Clank Hexes contract in the parser sandbox."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from src.hex_contract import BLOCK, HexFinding as Finding, evaluate_contract


@dataclass(frozen=True)
class CandidateDiff:
    root: Path
    source_root: Path
    added: frozenset[str]
    modified: frozenset[str]
    deleted: frozenset[str]
    git_index_paths: frozenset[str] | None = None
    git_index_state: str | None = None

    @property
    def changed(self) -> frozenset[str]:
        return self.added | self.modified | self.deleted

    def old_text(self, rel: str) -> str | None:
        path = self.source_root / rel
        try:
            return path.read_text(encoding="utf-8")
        except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
            return None

    def new_text(self, rel: str) -> str | None:
        path = self.root / rel
        try:
            return path.read_text(encoding="utf-8")
        except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
            return None


def _command_findings(contract, stage: str, root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for command in stage_commands(contract, stage):
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=False,
            )
            returncode = result.returncode
        except subprocess.TimeoutExpired:
            returncode = 124
        if returncode:
            findings.append(
                Finding(
                    level=BLOCK,
                    henxel=f"`{command}` must pass before {stage.replace('_', ' ')}",
                    path="",
                    message="",
                    details=[f"contained command failed (exit {returncode})"],
                    steer="fix the command or change the matching project contract rule",
                )
            )
    return findings


def _deletion_findings(contract, diff: CandidateDiff) -> list[Finding]:
    policy = settings.delete_protection(contract)
    if not policy:
        return []
    threshold = int(policy["over_lines"])
    deletions = Deletions(files=sorted(diff.deleted))
    for relative in sorted(diff.modified):
        old = diff.old_text(relative)
        new = diff.new_text(relative)
        if old is None or new is None:
            continue
        removed = max(0, len(old.splitlines()) - len(new.splitlines()))
        if removed > threshold:
            deletions.lines.append((relative, removed))
    return [] if deletions.empty else [deletion_finding(deletions)]


def _finding_payload(finding: Finding) -> dict[str, object]:
    return {
        "level": finding.level,
        "henxel": finding.henxel,
        "path": finding.path,
        "message": finding.message,
        "reason": finding.reason,
        "steer": finding.steer,
        "fix": finding.fix,
        "details": list(finding.details),
    }


def main(spec_path: str | None = None) -> int:
    spec = json.loads(Path(spec_path or "/input/source").read_text(encoding="utf-8"))
    root = Path(spec["candidate_root"])
    if spec.get("policy_root") is not None:
        sys.path.insert(0, str(Path(spec["policy_root"])))
    changes = spec.get("changes") or {}
    if ("git_index_paths" in spec) != ("git_index_state" in spec):
        raise ValueError("native Git projection fields must be paired")
    if "git_index_paths" in spec and (
        spec["git_index_state"] not in {"git", "non-git"}
        or not isinstance(spec["git_index_paths"], list)
        or any(not isinstance(path, str) for path in spec["git_index_paths"])
        or (spec["git_index_state"] == "non-git" and spec["git_index_paths"])
    ):
        raise ValueError("malformed native Git index authority")
    diff = CandidateDiff(
        root=root,
        source_root=Path(spec["source_root"]),
        added=frozenset(changes.get("added") or ()),
        modified=frozenset(changes.get("modified") or ()),
        deleted=frozenset(changes.get("deleted") or ()),
        git_index_paths=frozenset(spec["git_index_paths"]) if "git_index_paths" in spec else None,
        git_index_state=spec.get("git_index_state"),
    )
    stage = str(spec.get("stage") or "check")
    payload = evaluate_contract(
        spec["contract_path"],
        root=root,
        files=list(spec.get("files") or ()),
        diff=diff,
        stage=stage,
        command_root=diff.source_root,
        policy_root=spec.get("policy_root"),
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) == 2 else None))
