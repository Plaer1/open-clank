"""Release-context regressions that never print credential values."""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from scripts import check_release_artifacts as hygiene


ROOT = Path(__file__).resolve().parents[1]
AUDITED_LOCAL_ARTIFACTS = (
    ".env.bak-tunnel",
    ".env.linux-source",
    "frankenmemory.db-wal",
    "frankenmemory.db-shm",
)
AUDITED_BUILD_OUTPUTS = (
    "mcp_servers/frankenmemory/target",
    "packages/Copal/.node_modules.bak-20260708-143848",
    "packages/Copal/.next",
    "packages/Copal/db",
    "packages/Copal/out",
    "packages/Copal/treehouse",
    "packages/mimo-code/.turbo",
    "packages/openclank-agent-supervisor/target",
    "packages/odysseus-files/target",
)


def _git_ignored(relative_path: str) -> bool:
    result = subprocess.run(
        ("git", "check-ignore", "--no-index", "--quiet", "--", relative_path),
        cwd=ROOT,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def test_audited_local_artifacts_are_gitignored_but_examples_are_not():
    assert all(_git_ignored(path) for path in AUDITED_LOCAL_ARTIFACTS)
    assert not _git_ignored(".env.example")
    assert not _git_ignored("secrets.env.example")


def test_dockerignore_matcher_preserves_leading_dots():
    matcher = hygiene.DockerIgnore(
        (".env", ".env.bak-*", "*.db-wal", "secrets.env.*", "!secrets.env.example")
    )

    assert matcher.ignored(".env")
    assert matcher.ignored(".env.bak-tunnel")
    assert matcher.ignored("runtime.db-wal")
    assert not matcher.ignored("env")
    assert not matcher.ignored(".env.example")
    assert not matcher.ignored("secrets.env.example")


def test_audited_local_artifacts_are_absent_from_representative_image_context():
    context = set(hygiene.docker_context_paths(ROOT))
    assert not (set(AUDITED_LOCAL_ARTIFACTS) & context)
    for directory in AUDITED_BUILD_OUTPUTS:
        assert not any(path == directory or path.startswith(directory + "/") for path in context)
    assert ".env" not in context
    assert "secrets.env" not in context
    assert ".env.example" in context
    assert "Dockerfile" in context


def test_git_index_and_image_context_pass_prohibited_path_contract():
    index = hygiene.git_index_paths(ROOT)
    context = hygiene.docker_context_paths(ROOT)

    hygiene.assert_path_contract(index, scope="Git index")
    hygiene.assert_path_contract(context, scope="image context")


def test_standard_sqlite_runtime_suffixes_are_rejected_in_every_scope():
    for suffix in hygiene.DATABASE_SUFFIXES:
        path = f"runtime/state{suffix}"
        assert hygiene.prohibited_artifact(path, image_context=False)
        assert hygiene.prohibited_artifact(path, image_context=True)


def test_gitleaks_config_keeps_allowlists_structural_and_path_scoped():
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))

    assert config["extend"]["useDefault"] is True
    translation = config["allowlists"][0]
    assert translation["condition"] == "AND"
    assert translation["regexTarget"] == "line"
    assert translation["paths"] == [r"(^|/)static/i18n/[^/]+\.json$"]
    assert all(".*" not in expression for expression in translation["regexes"])


def test_directory_scan_uses_repository_gitleaks_config(tmp_path: Path, monkeypatch):
    (tmp_path / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n", encoding="utf-8")
    (tmp_path / ".gitleaksignore").write_text("fixture:generic-api-key:1\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def capture(command, *, cwd, label):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(hygiene, "_run_quiet", capture)
    hygiene._scan_directory("gitleaks", tmp_path / "tree", root=tmp_path, label="fixture")

    assert calls == [(
        "gitleaks",
        "dir",
        "--no-banner",
        "--redact",
        "--exit-code",
        "1",
        "--config",
        str(tmp_path / ".gitleaks.toml"),
        "--gitleaks-ignore-path",
        str(tmp_path / ".gitleaksignore"),
        ".",
    )]


def test_secret_scan_workflow_uses_config_for_history_and_release_artifacts():
    workflow = (ROOT / ".github/workflows/secret-scan.yml").read_text(encoding="utf-8")

    assert "gitleaks git --no-banner --redact --verbose --config .gitleaks.toml" in workflow
    assert "scripts/check_release_artifacts.py --gitleaks ./gitleaks" in workflow


def test_scanner_failure_discards_scanner_output(tmp_path: Path):
    marker = "scanner-output-must-stay-private"
    scanner = tmp_path / "scanner"
    scanner.write_text(
        "#!/bin/sh\n"
        f"printf '{marker}'\n"
        f"printf '{marker}' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    scanner.chmod(0o755)

    with pytest.raises(hygiene.ArtifactHygieneError) as failure:
        hygiene._scan_directory(
            str(scanner),
            tmp_path,
            root=tmp_path,
            label="fixture scan",
        )

    assert marker not in str(failure.value)


def test_context_materialization_does_not_follow_symlinks(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("outside fixture", encoding="utf-8")
    os.symlink(outside, source / "link")
    destination = tmp_path / "destination"

    hygiene._materialize_context(source, ("link",), destination)

    materialized = destination / "link"
    assert not materialized.is_symlink()
    assert materialized.read_text(encoding="utf-8") == str(outside)
