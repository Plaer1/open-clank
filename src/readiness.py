"""Ithaca anchor — local-instance readiness / integrity self-check.

Beyond ``/api/health``'s liveness ping, this confirms the self-hosted instance is
whole and at home: the database is reachable, the data directory is present and
writable, and storage is local-first. Served by ``GET /api/ready`` and suitable
for an orchestrator readiness probe (200 only when every critical check passes).
"""

import os
import uuid
from datetime import datetime
from typing import Any, Callable, Dict


def _engine_readiness(engine_verifier: Callable[[], Any] | None = None) -> Dict[str, object]:
    try:
        if engine_verifier is None:
            from core.constants import APP_VERSION
            from src.openclank.engine_build import source_fingerprint, verify_install
            from pathlib import Path

            repository = Path(__file__).resolve().parents[1]
            vendor = repository / "packages" / "mimo-code"
            # Source installs bind readiness to the tracked vendor tree. Frozen
            # packages do not ship that build tree: the pre-app portable
            # checksum/provenance gate binds the exact source fingerprint before
            # this lighter in-process readiness check runs.
            source = source_fingerprint(vendor) if vendor.is_dir() else None
            verification = verify_install(
                expected_version=APP_VERSION,
                expected_source_sha256=source,
                run_smoke=False,
                acp_smoke=False,
            )
        else:
            verification = engine_verifier()
        return {
            "ok": bool(getattr(verification, "ok", False)),
            "version": getattr(verification, "version", None),
            "target": getattr(verification, "target", None),
            "source_sha256": getattr(verification, "source_sha256", None),
            "errors": list(getattr(verification, "errors", ()) or ()),
        }
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)]}


def check_readiness(
    *,
    engine_verifier: Callable[[], Any] | None = None,
    supervisor: Any | None = None,
) -> Dict[str, object]:
    """Run the readiness checks and return a JSON-serialisable report.

    ``ready`` is True only when every critical check (database, data_dir) passes.
    ``local_first`` is informational — a remote database is a valid deployment, so
    it never fails readiness, it only reports whether storage stays on this host.
    """
    from core.constants import APP_VERSION, DATA_DIR
    from core.database import DATABASE_URL, engine
    from sqlalchemy import text as sql_text

    checks: Dict[str, Dict[str, object]] = {}

    # The managed engine is an installation invariant, not an optional model
    # integration.  Hash/version/schema checks are repeated here without
    # spawning another ACP child; startup performs the full handshake smoke.
    checks["engine"] = _engine_readiness(engine_verifier)

    # Database reachable — the simplest honest probe that the engine is live.
    try:
        with engine.connect() as conn:
            conn.execute(sql_text("SELECT 1"))
        checks["database"] = {"ok": True}
    except Exception as e:
        checks["database"] = {"ok": False, "error": str(e)}

    # Data directory present and writable — home must be able to hold its own data.
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        probe = os.path.join(DATA_DIR, f".ready_probe_{uuid.uuid4().hex}")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        checks["data_dir"] = {"ok": True, "path": DATA_DIR}
    except Exception as e:
        checks["data_dir"] = {"ok": False, "error": str(e)}

    # Local-first: storage stays on the home machine (informational, never fatal).
    local_first = (
        DATABASE_URL.startswith("sqlite")
        or "localhost" in DATABASE_URL
        or "127.0.0.1" in DATABASE_URL
    )
    checks["local_first"] = {"ok": True, "local": local_first}

    if supervisor is None:
        checks["engine_supervisor"] = {
            "ok": False,
            "error": "managed engine supervisor is not initialized",
        }
    else:
        try:
            snapshot = supervisor.readiness()
            checks["engine_supervisor"] = dict(snapshot)
        except Exception as exc:
            checks["engine_supervisor"] = {"ok": False, "error": str(exc)}

    ready = all(bool(c.get("ok")) for c in checks.values())
    return {
        "ready": ready,
        "version": APP_VERSION,
        "checks": checks,
        "timestamp": datetime.utcnow().isoformat(),
    }
