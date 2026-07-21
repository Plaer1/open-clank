import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from core.database import Base, ModelCapability, ModelEndpoint
from src import model_capabilities as caps


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'capabilities.db'}")
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def endpoint(db, *, legacy=None):
    row = ModelEndpoint(
        id="ep", name="Endpoint", base_url="https://example.test/v1",
        cached_models=json.dumps(["capable", "blocked", "new"]),
        supports_tools=legacy,
    )
    db.add(row)
    db.flush()
    return row


def test_tools_default_on_and_probe_is_diagnostic_only(db):
    ep = endpoint(db)
    state = caps.capability_state(ep, "new", None)
    assert state["tools_enabled"] is True
    assert state["status"] == "eligible"
    assert state["eligible"] is True
    assert state["probe_status"] == "unknown"

    row = caps.set_declared(db, ep, "capable", True)
    caps.set_verified(db, ep, "capable", False)
    state = caps.capability_state(ep, "capable", db.get(ModelCapability, (ep.id, "capable")))
    assert row.declaration_fingerprint is None
    assert state["tools_declared"] is True
    assert state["tools_enabled"] is True
    assert state["eligible"] is True
    assert state["probe_status"] == "unsupported"


def test_explicit_false_survives_identity_change_while_probe_stales(db):
    ep = endpoint(db)
    row = caps.set_declared(db, ep, "blocked", False)
    caps.set_verified(db, ep, "blocked", True)
    assert row.declaration_fingerprint is None

    state = caps.capability_state(ep, "blocked", row)
    assert state["tools_enabled"] is False
    assert state["status"] == "blocked"
    assert state["probe_status"] == "supported"

    ep.base_url = "https://replacement.test/v1"
    state = caps.capability_state(ep, "blocked", row)
    assert state["tools_declared"] is False
    assert state["tools_enabled"] is False
    assert state["probe_status"] == "stale"


def test_legacy_migration_clears_old_declarations_and_preserves_manual_and_probe(db, monkeypatch):
    ep = endpoint(db, legacy=False)
    old = ModelCapability(
        endpoint_id=ep.id,
        model_id="capable",
        tools_declared=False,
        declaration_fingerprint="legacy-marker",
        tools_verified=True,
        verification_fingerprint=caps.capability_fingerprint(ep, "capable"),
    )
    manual = ModelCapability(
        endpoint_id=ep.id,
        model_id="blocked",
        tools_declared=False,
        declaration_fingerprint=None,
    )
    db.add_all([old, manual])
    db.commit()
    factory = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr(caps, "SessionLocal", factory)
    caps.migrate_legacy_model_capabilities()

    db.expire_all()
    ep = db.get(ModelEndpoint, "ep")
    assert ep.supports_tools is None
    old = db.get(ModelCapability, (ep.id, "capable"))
    manual = db.get(ModelCapability, (ep.id, "blocked"))
    assert old.tools_declared is None
    assert old.declaration_fingerprint is None
    assert old.tools_verified is True
    assert old.verification_fingerprint == caps.capability_fingerprint(ep, "capable")
    assert manual.tools_declared is False
    assert set(caps.eligible_model_ids(db, ep)) == {"capable", "new"}
    db.delete(ep)
    db.commit()
    assert db.query(ModelCapability).count() == 0
