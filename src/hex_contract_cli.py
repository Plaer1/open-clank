"""CLI for the first-party Open Clank Hexes contract engine."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from core.atomic_io import atomic_write_text
from src.hex_contract import (
    CandidateDiff,
    HexContractError,
    discover,
    evaluate_contract,
    explain_contract,
    find_contract,
    load_contract,
    render_agent_digest,
    replace_agent_digest,
)
from src.clanker_paths import project_root_for_contract


_BLESSING_FILE = "openclank-hex-blessings.json"
_BLESSING_TTL_SECONDS = 15 * 60


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openclank hex", description="Open Clank project contract")
    commands = parser.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser("init", help="add default Hexes to an unconfigured workspace")
    initialize.add_argument("path", nargs="?", default=".")

    check = commands.add_parser("check", help="validate a project against its Hexes contract")
    check.add_argument("path", nargs="?", default=".")
    check.add_argument("--config")
    check.add_argument("--stage", choices=("check", "pre-commit", "pre-push"), default="check")
    check.add_argument("--json", action="store_true")

    explain = commands.add_parser("explain", help="show rules governing a path")
    explain.add_argument("path")
    explain.add_argument("--config")
    explain.add_argument("--json", action="store_true")

    sync = commands.add_parser("sync", help="sync the selected Hex contract into AGENTS.md")
    sync.add_argument("--config")
    sync.add_argument("--agents", default="AGENTS.md")

    commands.add_parser("install-hooks", help="install first-party Hexes git hooks")

    bless = commands.add_parser("bless", help="issue a consume-once local contract blessing")
    bless.add_argument("action", choices=("delete", "push"))
    bless.add_argument("--reason", required=True)

    hook = commands.add_parser("hook", help=argparse.SUPPRESS)
    hook.add_argument("stage", choices=("pre-commit", "pre-push"))
    hook.add_argument("hook_args", nargs=argparse.REMAINDER)
    return parser


def _repo_root(start: str | os.PathLike[str]) -> Path:
    result = subprocess.run(
        ["git", "-C", str(Path(start).resolve()), "rev-parse", "--show-toplevel"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode:
        return Path(start).resolve()
    return Path(result.stdout.strip()).resolve()


def _config(value: Optional[str], start: Path) -> Path:
    if value:
        path = Path(value)
        return (path if path.is_absolute() else Path.cwd() / path).resolve(strict=True)
    found = find_contract(start)
    if found is None:
        raise HexContractError("no Hexes contract found")
    return found.resolve(strict=True)


def _render(result: dict[str, Any], *, json_output: bool) -> int:
    if json_output:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        findings = list(result.get("findings") or [])
        if not findings:
            print(f"Hexes: contract satisfied ({result.get('engine_version')})")
        for finding in findings:
            marker = "BLOCK" if finding.get("level") == "block" else "WARN"
            print(f"{marker}: {finding.get('hex') or finding.get('henxel')}")
            if finding.get("reason"):
                print(f"  why: {finding['reason']}")
            for detail in finding.get("details") or ():
                print(f"  - {detail}")
            if finding.get("steer"):
                print(f"  next: {finding['steer']}")
    return 0 if result.get("allowed") else 1


def _check(args: argparse.Namespace) -> int:
    target = Path(args.path).resolve()
    search_root = target if target.is_dir() else target.parent
    contract_path = _config(args.config, search_root)
    root = project_root_for_contract(contract_path)
    result = evaluate_contract(
        contract_path,
        root=root,
        files=discover(root),
        stage=args.stage,
        command_root=root,
    )
    return _render(result, json_output=bool(args.json))


def _explain(args: argparse.Namespace) -> int:
    target = Path(args.path).resolve(strict=False)
    search_root = target if target.is_dir() else target.parent
    contract_path = _config(args.config, search_root)
    root = project_root_for_contract(contract_path)
    contract = load_contract(contract_path)
    try:
        relative = target.relative_to(root).as_posix()
    except ValueError as exc:
        raise HexContractError("explain path must be inside the project") from exc
    rules = explain_contract(contract, relative)
    if args.json:
        print(json.dumps({"path": relative, "hexes": rules}, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Hexes for {relative}")
        if not rules:
            print("  (no matching rules)")
        for rule in rules:
            print(f"  • {rule['hex']}")
            if rule.get("why"):
                print(f"      why: {rule['why']}")
            for name, value in (rule.get("checks") or {}).items():
                print(f"      {name}: {value}")
    return 0


def _sync(args: argparse.Namespace) -> int:
    contract_path = _config(args.config, Path.cwd().resolve())
    root = project_root_for_contract(contract_path)
    contract = load_contract(contract_path)
    if contract.vocabulary != "hexes":
        raise HexContractError("sync requires hexes:/hex: vocabulary")
    agents = Path(args.agents)
    agents = (agents if agents.is_absolute() else root / agents).resolve(strict=False)
    try:
        agents.relative_to(root)
    except ValueError as exc:
        raise HexContractError("AGENTS output must stay inside the project") from exc
    existing = agents.read_text(encoding="utf-8") if agents.exists() else ""
    updated = replace_agent_digest(existing, render_agent_digest(contract))
    if updated != existing:
        atomic_write_text(str(agents), updated)
    print(f"Hexes: synced {agents.relative_to(root)} from {contract_path.relative_to(root)}")
    return 0


def _install_hooks() -> int:
    root = _repo_root(".")
    hooks = _git_dir(root) / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    body = """#!/bin/sh
# Open Clank Hexes managed hook. The contract engine is first-party.
ROOT=$(git rev-parse --show-toplevel) || exit 1
if [ -x "$ROOT/venv/bin/python" ]; then
  PY="$ROOT/venv/bin/python"
elif [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
else
  PY=python3
fi
exec "$PY" "$ROOT/scripts/openclank" hex hook STAGE "$@"
"""
    for name, stage in (("pre-commit", "pre-commit"), ("pre-push", "pre-push")):
        path = hooks / name
        atomic_write_text(str(path), body.replace("STAGE", stage))
        os.chmod(path, 0o755, follow_symlinks=False)
    print("Hexes: installed pre-commit and pre-push hooks")
    return 0


def _git_output(root: Path, *arguments: str, check: bool = True) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if check and result.returncode:
        raise HexContractError((result.stderr or b"git operation failed").decode("utf-8", errors="replace")[:2048])
    return result.stdout


def _paths(root: Path, filter_value: str) -> frozenset[str]:
    output = _git_output(root, "diff", "--cached", "--name-only", f"--diff-filter={filter_value}", "-z")
    return frozenset(value.decode("utf-8", errors="surrogateescape") for value in output.split(b"\0") if value)


def _blob(root: Path, spec: str) -> Optional[bytes]:
    result = subprocess.run(
        ["git", "-C", str(root), "show", spec],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _index_diff(root: Path, snapshot: Path) -> CandidateDiff:
    added, modified, deleted = _paths(root, "A"), _paths(root, "MRT"), _paths(root, "D")
    changed = added | modified | deleted
    old = {path: _blob(root, f"HEAD:{path}") for path in changed}
    new = {path: _blob(root, f":{path}") for path in changed}
    return CandidateDiff(snapshot, root, added, modified, deleted, old, new)


def _index_snapshot(root: Path, destination: Path) -> None:
    prefix = str(destination.resolve()) + os.sep
    _git_output(root, "checkout-index", "--all", f"--prefix={prefix}")


def _git_dir(root: Path) -> Path:
    value = _git_output(root, "rev-parse", "--git-dir").decode("utf-8").strip()
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve(strict=True)


def _blessing_path(root: Path) -> Path:
    return _git_dir(root) / _BLESSING_FILE


def _action_digest(root: Path, action: str) -> str:
    if action == "delete":
        material = _git_output(root, "diff", "--cached", "--binary")
    else:
        material = _git_output(root, "rev-parse", "HEAD")
    return hashlib.sha256(action.encode("ascii") + b"\0" + material).hexdigest()


def _bless(args: argparse.Namespace) -> int:
    root = _repo_root(".")
    reason = str(args.reason or "").strip()
    if not reason:
        raise HexContractError("a non-empty blessing reason is required")
    now = int(time.time())
    payload = {
        "version": 1,
        "action": args.action,
        "digest": _action_digest(root, args.action),
        "reason": reason,
        "issued_at": now,
        "expires_at": now + _BLESSING_TTL_SECONDS,
        "uid": os.getuid() if hasattr(os, "getuid") else None,
    }
    path = _blessing_path(root)
    atomic_write_text(str(path), json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    try:
        os.chmod(path, 0o600, follow_symlinks=False)
    except (OSError, NotImplementedError):
        pass
    print(f"Hexes: issued one {args.action} blessing for the current digest (expires in 15 minutes)")
    return 0


def _consume_blessing(root: Path, action: str) -> bool:
    path = _blessing_path(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return False
    valid = (
        payload.get("version") == 1
        and payload.get("action") == action
        and payload.get("digest") == _action_digest(root, action)
        and int(payload.get("expires_at") or 0) >= int(time.time())
        and payload.get("uid") == (os.getuid() if hasattr(os, "getuid") else None)
        and bool(str(payload.get("reason") or "").strip())
    )
    if valid:
        try:
            path.unlink()
        except FileNotFoundError:
            return False
    return valid


def _remove_blessed_finding(result: dict[str, Any], *, root: Path, action: str, sentence: str) -> None:
    findings = list(result.get("findings") or [])
    matching = [item for item in findings if item.get("level") == "block" and item.get("hex") == sentence]
    if not matching or not _consume_blessing(root, action):
        return
    result["findings"] = [item for item in findings if item not in matching]
    result["warnings"] = [item for item in result.get("findings") or [] if item.get("level") != "block"]
    result["allowed"] = not any(item.get("level") == "block" for item in result["findings"])


def _hook(args: argparse.Namespace) -> int:
    root = _repo_root(".")
    if args.stage == "pre-commit":
        with tempfile.TemporaryDirectory(prefix="openclank-hex-index-") as temporary:
            snapshot = Path(temporary)
            _index_snapshot(root, snapshot)
            config = snapshot / ".hex"
            if not config.is_file():
                config = _config(None, root)
                snapshot = root
            diff = _index_diff(root, snapshot)
            result = evaluate_contract(
                config,
                root=snapshot,
                files=discover(snapshot),
                diff=diff,
                stage="pre-commit",
                command_root=root,
            )
        _remove_blessed_finding(
            result,
            root=root,
            action="delete",
            sentence="Destructive changes require an explicit Open Clank Hexes blessing",
        )
    else:
        config = _config(None, root)
        result = evaluate_contract(config, root=root, files=discover(root), stage="pre-push", command_root=root)
        _remove_blessed_finding(
            result,
            root=root,
            action="push",
            sentence="Pushing requires an explicit Open Clank Hexes blessing",
        )
    return _render(result, json_output=False)


def main(argv: Optional[list[str]] = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "init":
            from src.hex_defaults import initialize_defaults
            path, created = initialize_defaults(args.path)
            print(f"Hexes: {'created defaults at' if created else 'preserved existing contract at'} {path}")
            return 0
        if args.command == "check":
            return _check(args)
        if args.command == "explain":
            return _explain(args)
        if args.command == "sync":
            return _sync(args)
        if args.command == "install-hooks":
            return _install_hooks()
        if args.command == "bless":
            return _bless(args)
        if args.command == "hook":
            return _hook(args)
        return 2
    except (HexContractError, OSError, subprocess.SubprocessError) as exc:
        print(f"openclank hex: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
