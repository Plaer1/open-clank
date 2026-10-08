#!/usr/bin/env python3
"""Fail closed when local secrets or database artifacts can ship.

Git index paths and bytes come from the index, never the working tree.
An explicit export directory is checked as assembled, including every file.
History scans inspect the selected HEAD ancestry with exact classifications.
Docker is unsupported; this gate never constructs or scans a Docker context.

Scanner stdout/stderr is intentionally discarded. A scanner finding is useful
as a gate, but matched material must never be copied into CI output.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
DATABASE_SUFFIXES = (
    ".db",
    ".sqlite",
    ".sqlite3",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3-journal",
    ".sqlite3-shm",
    ".sqlite3-wal",
)
SAFE_ENV_EXAMPLES = {".env.example", "secrets.env.example"}


class ArtifactHygieneError(RuntimeError):
    """A value-silent release-artifact policy failure."""


def _run_quiet(
    command: Sequence[str],
    *,
    cwd: Path,
    label: str,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ArtifactHygieneError(f"{label} could not start") from exc
    if result.returncode:
        raise ArtifactHygieneError(f"{label} failed (exit {result.returncode})")
    return result


def git_index_paths(root: Path) -> tuple[str, ...]:
    result = _run_quiet(
        ("git", "ls-files", "--cached", "-z"),
        cwd=root,
        label="Git index enumeration",
    )
    return tuple(
        sorted(
            item.decode("utf-8", "surrogateescape")
            for item in result.stdout.split(b"\0")
            if item
        )
    )


def prohibited_artifact(relative_path: str, *, image_context: bool) -> bool:
    path = PurePosixPath(relative_path)
    name = path.name
    if name == "move-data.json" or relative_path in {
        "packages/Copal/worklog.md",
        "packages/Copal/move-timeline.desktop",
        "libexec/openclank/.engine.build.lock",
    } or ".s26-visual" in path.parts:
        return True
    # These namespaces contain private recovery/study/user state even when a
    # force-add or a future ignore exception accidentally admits their paths.
    if any(part in {".clanker", ".clankers", ".references", ".archive", ".obsidian", ".mimocode"} for part in path.parts):
        return True
    if fnmatchcase(name, "history-credentials*.json") or fnmatchcase(
        name, ".history-credentials*.tmp"
    ):
        return True
    if name in SAFE_ENV_EXAMPLES:
        return False
    if name.lower().endswith(DATABASE_SUFFIXES):
        return True
    if len(path.parts) == 1 and (
        name == ".env"
        or fnmatchcase(name, ".env.bak.*")
        or fnmatchcase(name, ".env.bak-*")
        or fnmatchcase(name, ".env.*-source")
    ):
        return True
    if name.startswith("secrets.env."):
        return True
    return image_context and name == "secrets.env"


def assert_path_contract(paths: Iterable[str], *, scope: str) -> None:
    image_context = scope == "image context"
    violations = [
        path for path in paths
        if prohibited_artifact(path, image_context=image_context)
        or (scope == "Git index" and path == "static/vendor/google-emoji/emoji-assets.pack")
    ]
    if violations:
        # Filenames identify the broken exclusion without disclosing contents.
        joined = ", ".join(violations[:10])
        remainder = len(violations) - 10
        suffix = f" (+{remainder} more)" if remainder > 0 else ""
        raise ArtifactHygieneError(
            f"{scope} contains prohibited artifact path(s): {joined}{suffix}"
        )


def _materialize_index(root: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    prefix = str(destination.resolve()) + os.sep
    _run_quiet(
        ("git", "checkout-index", "--all", f"--prefix={prefix}"),
        cwd=root,
        label="Git index materialization",
    )


def check_export(root: Path, export: Path, *, gitleaks: str | None, paths_only: bool) -> int:
    """Check an already assembled package/tree, without applying ignore rules.

    Ignore files cannot certify an archive's actual contents. External links
    are rejected because the packaged path list cannot qualify their payloads.
    """
    export = export.resolve()
    if not export.is_dir():
        raise ArtifactHygieneError("export root must be an existing directory")
    paths: list[str] = []
    for directory, names, filenames in os.walk(export, followlinks=False):
        for name in names + filenames:
            source = Path(directory) / name
            relative = source.relative_to(export).as_posix()
            if source.is_symlink():
                target = Path(os.readlink(source))
                if target.is_absolute() or not source.resolve().is_relative_to(export):
                    raise ArtifactHygieneError(f"export contains external symlink: {relative}")
                if not source.exists():
                    raise ArtifactHygieneError(f"export contains broken symlink: {relative}")
            if source.is_file() or source.is_symlink():
                paths.append(relative)
    assert_path_contract(paths, scope="export")
    if not paths_only:
        if not gitleaks:
            raise ArtifactHygieneError("gitleaks executable is required for content scans")
        _scan_directory(gitleaks, export, root=root, label="export secret scan")
    return len(paths)


def _scan_directory(gitleaks: str, directory: Path, *, root: Path, label: str) -> None:
    executable = Path(gitleaks)
    if not executable.is_absolute():
        repository_executable = (root / executable).resolve()
        if repository_executable.is_file():
            gitleaks = str(repository_executable)
    command = [
        gitleaks,
        "dir",
        "--no-banner",
        "--redact=100",
        "--exit-code",
        "1",
    ]
    config = root / ".gitleaks.toml"
    if config.is_file():
        command.extend(("--config", str(config)))
    ignore = root / ".gitleaksignore"
    if ignore.is_file():
        command.extend(("--gitleaks-ignore-path", str(ignore)))
    # Scan from the materialized root so directory-mode fingerprints are stable
    # repository-relative paths rather than random temporary-directory paths.
    command.append(".")
    _run_quiet(
        command,
        cwd=directory,
        label=label,
    )


def scan_history(root: Path, *, gitleaks: str) -> None:
    executable = str(Path(gitleaks).resolve())
    _run_quiet(
        (executable, "git", "--no-banner", "--redact=100", "--exit-code", "1",
         "--log-opts=HEAD", "--config", str(root / ".gitleaks.toml"),
         "--gitleaks-ignore-path", str(root / ".gitleaksignore"), "."),
        cwd=root, label="selected HEAD ancestry secret scan",
    )


def run(root: Path, *, gitleaks: str | None, paths_only: bool) -> int:
    root = root.resolve()
    index_paths = git_index_paths(root)
    assert_path_contract(index_paths, scope="Git index")
    if paths_only:
        return len(index_paths)
    if not gitleaks:
        raise ArtifactHygieneError("gitleaks executable is required for content scans")
    with tempfile.TemporaryDirectory(prefix="openclank-artifact-scan-") as temp:
        index_root = Path(temp) / "index"
        _materialize_index(root, index_root)
        _scan_directory(gitleaks, index_root, root=root, label="Git index secret scan")
    return len(index_paths)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--gitleaks", help="path to the pinned gitleaks executable")
    parser.add_argument("--history", action="store_true", help="also scan the selected HEAD ancestry (never all refs)")
    parser.add_argument("--export-root", type=Path, help="check actual assembled package contents instead of the Git index")
    parser.add_argument(
        "--paths-only",
        action="store_true",
        help="validate index/export path policy without reading file contents",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.export_root is not None:
            count = check_export(args.root.resolve(), args.export_root, gitleaks=args.gitleaks, paths_only=args.paths_only)
            mode = "path check" if args.paths_only else "secret scan"
            print(f"artifact hygiene export {mode} passed: files={count}")
            return 0
        if args.history:
            if args.paths_only or not args.gitleaks:
                raise ArtifactHygieneError("history scanning requires gitleaks and content scanning")
            scan_history(args.root.resolve(), gitleaks=args.gitleaks)
        index_count = run(
            args.root,
            gitleaks=args.gitleaks,
            paths_only=args.paths_only,
        )
    except ArtifactHygieneError as exc:
        print(f"artifact hygiene check failed: {exc}", file=sys.stderr)
        return 1

    mode = "path check" if args.paths_only else "secret scan"
    print(
        f"artifact hygiene {mode} passed: "
        f"index={index_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
