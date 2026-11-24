"""Tests for the readiness / integrity self-check (src/readiness.py)."""

from types import SimpleNamespace

from src.readiness import check_readiness


def test_readiness_reports_core_subsystems():
    result = check_readiness(
        engine_verifier=lambda: SimpleNamespace(
            ok=True,
            version="test",
            target="test-target",
            source_sha256="0" * 64,
            errors=(),
        ),
        supervisor=SimpleNamespace(readiness=lambda: {"ok": True}),
    )

    assert {"ready", "version", "checks", "timestamp"}.issubset(result.keys())
    checks = result["checks"]
    for name in ("database", "data_dir", "local_first", "engine", "engine_supervisor"):
        assert name in checks, f"missing check: {name}"

    # In the dev/test environment the local SQLite DB and data dir are present,
    # so the critical checks must pass and overall readiness must be True.
    assert checks["database"]["ok"] is True, checks["database"]
    assert checks["data_dir"]["ok"] is True, checks["data_dir"]
    assert result["ready"] is True, result


def test_local_first_check_is_informational_never_fatal():
    result = check_readiness(
        engine_verifier=lambda: SimpleNamespace(ok=True, errors=()),
        supervisor=SimpleNamespace(readiness=lambda: {"ok": True}),
    )
    lf = result["checks"]["local_first"]
    # local_first reports whether storage stays on-host but must never gate
    # readiness — a remote database is a valid deployment.
    assert lf["ok"] is True
    assert "local" in lf


def test_missing_engine_or_supervisor_fails_readiness():
    result = check_readiness(
        engine_verifier=lambda: SimpleNamespace(ok=False, errors=("missing",)),
    )
    assert result["ready"] is False
    assert result["checks"]["engine"]["ok"] is False
    assert result["checks"]["engine_supervisor"]["ok"] is False
