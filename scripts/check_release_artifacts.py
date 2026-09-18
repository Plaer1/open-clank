#!/usr/bin/env python3
"""Fail closed when local secrets or database artifacts can ship.

The check has two inputs with different authorities:

* Git index paths and bytes come from the index, never the working tree.
* The representative image context comes from the working tree after applying
  the repository's ``.dockerignore`` contract.

Scanner stdout/stderr is intentionally discarded. A scanner finding is useful
as a gate, but matched material must never be copied into CI output.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
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


@dataclass(frozen=True)
class IgnoreRule:
    negative: bool
    pattern: str
    regex: re.Pattern[str]


def _glob_regex(pattern: str) -> str:
    """Translate the shared Git/Docker wildmatch subset used by this repo."""

    chunks: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    chunks.append("(?:.*/)?")
                    index += 1
                else:
                    chunks.append(".*")
                continue
            chunks.append("[^/]*")
        elif char == "?":
            chunks.append("[^/]")
        elif char == "[":
            end = pattern.find("]", index + 1)
            if end == -1:
                chunks.append(r"\[")
            else:
                body = pattern[index + 1 : end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                chunks.append("[" + body.replace("\\", r"\\") + "]")
                index = end
        else:
            chunks.append(re.escape(char))
        index += 1
    return "".join(chunks)


class DockerIgnore:
    """Match the intentionally shared subset used in root ``.dockerignore``."""

    def __init__(self, lines: Iterable[str]) -> None:
        rules: list[IgnoreRule] = []
        for source_line in lines:
            line = source_line.rstrip("\r\n")
            if not line or line.startswith("#"):
                continue
            negative = line.startswith("!")
            if negative:
                line = line[1:]
            line = line.strip()
            if not line or line == ".":
                continue

            anchored = line.startswith("/")
            line = line.strip("/")
            if not line:
                continue

            has_separator = "/" in line
            prefix = "^" if anchored or has_separator else r"(?:^|.*/)"
            expression = prefix + _glob_regex(line) + r"(?:$|/.*$)"
            rules.append(
                IgnoreRule(
                    negative=negative,
                    pattern=line,
                    regex=re.compile(expression),
                )
            )
        self.rules = tuple(rules)

    @classmethod
    def from_path(cls, path: Path) -> "DockerIgnore":
        return cls(path.read_text(encoding="utf-8").splitlines())

    def ignored(self, relative_path: str) -> bool:
        normalized = PurePosixPath(relative_path).as_posix()
        while normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.lstrip("/")
        ignored = False
        for rule in self.rules:
            if rule.regex.match(normalized):
                ignored = not rule.negative
        return ignored


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


def docker_context_paths(root: Path) -> tuple[str, ...]:
    matcher = DockerIgnore.from_path(root / ".dockerignore")
    included: list[str] = []

    for directory, names, filenames in os.walk(root, topdown=True, followlinks=False):
        relative_directory = Path(directory).relative_to(root)

        kept_names: list[str] = []
        for name in names:
            relative = (relative_directory / name).as_posix()
            if matcher.ignored(relative):
                continue
            kept_names.append(name)
            candidate = Path(directory) / name
            if candidate.is_symlink():
                included.append(relative)
        names[:] = kept_names

        for name in filenames:
            relative = (relative_directory / name).as_posix()
            if not matcher.ignored(relative):
                included.append(relative)

    return tuple(sorted(included))


def prohibited_artifact(relative_path: str, *, image_context: bool) -> bool:
    path = PurePosixPath(relative_path)
    name = path.name
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
        path for path in paths if prohibited_artifact(path, image_context=image_context)
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


def _materialize_context(root: Path, paths: Iterable[str], destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for relative in paths:
        source = root / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            # Scan the link text without following a target outside the context.
            target.write_text(os.readlink(source), encoding="utf-8")
        else:
            shutil.copyfile(source, target)


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
        "--redact",
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


def run(root: Path, *, gitleaks: str | None, paths_only: bool) -> tuple[int, int]:
    root = root.resolve()
    index_paths = git_index_paths(root)
    context_paths = docker_context_paths(root)
    assert_path_contract(index_paths, scope="Git index")
    assert_path_contract(context_paths, scope="image context")

    if paths_only:
        return len(index_paths), len(context_paths)
    if not gitleaks:
        raise ArtifactHygieneError("gitleaks executable is required for content scans")

    with tempfile.TemporaryDirectory(prefix="openclank-artifact-scan-") as temp:
        temporary_root = Path(temp)
        index_root = temporary_root / "index"
        context_root = temporary_root / "context"
        _materialize_index(root, index_root)
        _materialize_context(root, context_paths, context_root)
        _scan_directory(gitleaks, index_root, root=root, label="Git index secret scan")
        _scan_directory(
            gitleaks,
            context_root,
            root=root,
            label="image-context secret scan",
        )

    return len(index_paths), len(context_paths)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--gitleaks", help="path to the pinned gitleaks executable")
    parser.add_argument(
        "--paths-only",
        action="store_true",
        help="validate index/context path policy without reading file contents",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        index_count, context_count = run(
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
        f"index={index_count}, image-context={context_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
