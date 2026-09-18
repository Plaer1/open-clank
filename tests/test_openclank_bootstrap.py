from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "openclank_bootstrap.py"


def _module():
    spec = importlib.util.spec_from_file_location("openclank_bootstrap_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_bootstrap_import_does_not_load_normal_database_or_app():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import scripts.openclank_bootstrap; "
                "assert 'app' not in sys.modules; "
                "assert 'core.database' not in sys.modules; "
                "assert 'src.constants' not in sys.modules; "
                "assert 'src.openclank.provider_cutover' not in sys.modules"
            ),
        ],
        cwd=SCRIPT.parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" YES ", "true"),
        ("Off", "false"),
        ("release", None),
        ("", None),
    ],
)
def test_bootstrap_normalizes_only_legacy_boolean_debug_values(raw, expected):
    module = _module()
    environment = {"DEBUG": raw, "OPEN_CLANK_DEBUG": "true"}

    module._normalize_application_environment(environment)

    assert environment.get("DEBUG") == expected
    assert environment["OPEN_CLANK_DEBUG"] == "true"


class _Verification:
    version = "test"
    target = "darwin-arm64"
    source_sha256 = "a" * 64
    binary = Path("/verified/openclank-engine")

    def require(self):
        return self


def test_check_verifies_engine_without_importing_app(tmp_path, monkeypatch, capsys):
    module = _module()
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(module, "ensure_engine_ready", lambda **_kwargs: _Verification())
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for name in module.PROVIDER_ENV_AUTHORITIES:
        monkeypatch.delenv(name, raising=False)
    assert module.main(["--data-dir", str(data), "check"]) == 0
    output = capsys.readouterr().out
    assert '"phase": "not_required"' in output


def test_serve_execs_only_after_engine_and_cutover(tmp_path, monkeypatch):
    module = _module()
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(module, "ensure_engine_ready", lambda **_kwargs: _Verification())
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DEBUG", "release")
    for name in module.PROVIDER_ENV_AUTHORITIES:
        monkeypatch.delenv(name, raising=False)
    captured = {}

    def fake_exec(executable, argv, environment):
        captured.update(executable=executable, argv=argv, environment=environment)
        raise RuntimeError("exec-fenced")

    monkeypatch.setattr(module.os, "execvpe", fake_exec)
    with pytest.raises(RuntimeError, match="exec-fenced"):
        module.main(
            [
                "--data-dir",
                str(data),
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "7788",
            ]
        )
    assert captured["argv"][-5:] == [
        "app:app",
        "--host",
        "127.0.0.1",
        "--port",
        "7788",
    ]
    assert captured["environment"]["OPEN_CLANK_ENGINE_BIN"] == str(
        _Verification.binary
    )
    assert "DEBUG" not in captured["environment"]


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "LLM_HOST", "EMBEDDING_URL"])
def test_provider_environment_is_named_but_value_is_never_reported(
    tmp_path, monkeypatch, capsys, name
):
    module = _module()
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(module, "ensure_engine_ready", lambda **_kwargs: _Verification())
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for name in module.PROVIDER_ENV_AUTHORITIES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(name, "never-print-this-value")
    assert module.main(["--data-dir", str(data), "check"]) == 1
    error = capsys.readouterr().err
    assert name in error
    assert "never-print-this-value" not in error


def test_runtime_command_reports_admitted_components(capsys):
    module = _module()
    assert module.main(["runtime", "--repo-root", str(SCRIPT.parents[1])]) == 0
    report = capsys.readouterr().out
    assert '"ok": true' in report
    assert '"kind": "python"' in report
    assert '"kind": "lifetools"' in report
    assert '"kind": "fm_mcp"' in report
