from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, Session as DbSession
from core.provider_models import (ProviderAccount, ProviderBase, ProviderConnection,
                                  ProviderOperationBinding)
from core.stats_models import StatsQuotaCycle, StatsQuotaObservation
from routes.stats_routes import setup_stats_routes
from services.stats.quota import (QuotaError, admit_quota_observation, erase_owner_quota,
                                  enforce_quota_retention, quota_snapshot)
from services.stats.privacy import identity_handle


OWNER = "local-installation"
T0 = datetime(2026, 3, 1, 12)


@pytest.fixture
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    ProviderBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(ProviderConnection(id="conn-a", owner=OWNER, family_id="provider-a",
                                  adapter_id="fixture", kind="fixture", billing_lane="api",
                                  label="fixture"))
        db.flush()
        db.add(ProviderAccount(id="acct-a", connection_id="conn-a", owner=OWNER,
                               label="fixture", auth_method="fixture", auth_class="fixture",
                               credential_envelope=None, safe_identity={}))
        db.commit()
    return factory


def admit(factory, **overrides):
    values = dict(owner=OWNER, account_id="acct-a", provider_id="provider-a",
                  window_id="window", window_kind="discrete", observed_at=T0,
                  source_kind="official", adapter_revision="fixture-v1",
                  reset_at=T0 + timedelta(hours=1), limit_value=100,
                  remaining_value=80)
    values.update(overrides)
    with factory() as db:
        row = admit_quota_observation(db, **values)
        db.commit()
        return row.id if row else None


def test_discrete_cycle_requires_later_reset_and_is_restart_safe(factory):
    assert admit(factory) is not None
    with factory() as db:
        assert db.query(StatsQuotaCycle).count() == 0
    # A later reset closes exactly the prior window.
    admit(factory, observed_at=T0 + timedelta(hours=2),
          reset_at=T0 + timedelta(hours=3))
    with factory() as db:
        assert db.query(StatsQuotaCycle).count() == 1
    # Replaying the same observation after a session restart is a no-op.
    assert admit(factory, observed_at=T0 + timedelta(hours=2),
                 reset_at=T0 + timedelta(hours=3)) is None
    with factory() as db:
        assert db.query(StatsQuotaCycle).count() == 1


def test_correction_late_sample_and_continuous_refill_do_not_make_cycles(factory):
    admit(factory)
    # A correction that moves reset backwards is not a transition.
    admit(factory, observed_at=T0 + timedelta(minutes=30), reset_at=T0 + timedelta(minutes=45))
    # An out-of-order sample cannot retroactively close a later observation.
    admit(factory, observed_at=T0 - timedelta(minutes=1), reset_at=T0 + timedelta(hours=2))
    admit(factory, window_id="continuous", window_kind="continuous",
          reset_at=None, observed_at=T0 + timedelta(hours=4))
    with factory() as db:
        assert db.query(StatsQuotaCycle).count() == 0


def test_admission_requires_live_owner_provider_account(factory):
    with pytest.raises(QuotaError, match="live account"):
        admit(factory, owner="other-owner")
    with pytest.raises(QuotaError, match="live account"):
        admit(factory, account_id="missing")
    with pytest.raises(QuotaError, match="provider"):
        admit(factory, provider_id="provider-b")


def test_admission_normalizes_full_replay_and_rejects_unsafe_values(factory):
    with pytest.raises(QuotaError):
        admit(factory, source_kind="probe")
    with pytest.raises(QuotaError):
        admit(factory, label="x\x00bad")
    overage = admit(factory, utilization_numerator=11, utilization_denominator=10)
    assert overage is not None
    with pytest.raises(QuotaError):
        admit(factory, remaining_value=101, limit_value=100)
    with pytest.raises(QuotaError):
        admit(factory, utilization_numerator=2**63)
    first = admit(factory, limit_value=100, remaining_value=80)
    corrected = admit(factory, limit_value=90, remaining_value=70)
    assert corrected is not None and corrected != first
    assert admit(factory, limit_value=90, remaining_value=70) is None
    with factory() as db:
        assert db.query(StatsQuotaObservation).count() == 3


def test_disabled_canonical_rows_and_uncommitted_operation_binding_are_rejected(factory):
    with factory() as db:
        db.query(ProviderAccount).filter_by(id="acct-a").update({ProviderAccount.enabled: False})
        db.commit()
    with pytest.raises(QuotaError, match="live account"):
        admit(factory)
    with factory() as db:
        db.query(ProviderAccount).filter_by(id="acct-a").update({ProviderAccount.enabled: True})
        db.add(ProviderOperationBinding(id="binding", owner=OWNER, credential_owner=OWNER,
                                        root_operation_id="op-1", connection_id="conn-a",
                                        provider_id="provider-a", billing_lane="api",
                                        model_id="model", selected_account_id="acct-a"))
        db.commit()
    with pytest.raises(QuotaError, match="operation binding"):
        admit(factory, operation_id="op-1")
    with factory() as db:
        binding = db.get(ProviderOperationBinding, "binding")
        binding.state = "committed"
        binding.committed_at = T0
        db.commit()
    assert admit(factory, operation_id="op-1") is not None


def test_snapshot_preserves_official_error_stale_and_noisy_account(factory):
    admit(factory, source_kind="local", observed_at=T0 + timedelta(minutes=1))
    admit(factory, source_kind="error", observed_at=T0 + timedelta(minutes=2),
          error_class="rate_limited")
    # More than the old 100-row window for A must not hide B.
    with factory() as db:
        db.add(ProviderConnection(id="conn-b", owner=OWNER, family_id="provider-b",
                                  adapter_id="fixture", kind="fixture", billing_lane="api",
                                  label="fixture"))
        db.flush()
        db.add(ProviderAccount(id="acct-b", connection_id="conn-b", owner=OWNER,
                               label="fixture", auth_method="fixture", auth_class="fixture",
                               credential_envelope=None, safe_identity={}))
        db.commit()
    for i in range(101):
        admit(factory, observed_at=T0 + timedelta(minutes=10 + i),
              source_kind="local", window_kind="continuous", reset_at=None)
    with factory() as db:
        for i in range(1):
            admit_quota_observation(db, owner=OWNER, account_id="acct-b", provider_id="provider-b",
                                    window_id="window", window_kind="continuous",
                                    observed_at=T0 + timedelta(minutes=20), source_kind="error",
                                    adapter_revision="fixture-v1", error_class="offline")
        db.commit()
        report = quota_snapshot(db, owner=OWNER, now=T0 + timedelta(days=2), stale_after_seconds=3600)
    accounts = {row["account_id"]: row for row in report["observations"]}
    assert "acct-a" in accounts and "acct-b" in accounts
    assert accounts["acct-b"]["state"] == "stale"


def test_quota_route_is_owner_scoped_and_serializes_supported_account(factory, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(setup_stats_routes(session_factory=factory))
    client = TestClient(app)
    admit(factory, window_kind="continuous", reset_at=None)
    with factory() as db:
        db.add(ProviderAccount(id="acct-unused", connection_id="conn-a", owner=OWNER,
                               label="unused", auth_method="fixture", auth_class="fixture",
                               credential_envelope=None, safe_identity={}))
        db.commit()
    account_handle = client.get("/api/stats/v1/quota").json()["observations"][0]["account_identity"]["handle"]
    rejected = client.get("/api/stats/v1/quota", params={"account_id": "acct-a"})
    assert rejected.status_code == 422
    all_accounts = client.get("/api/stats/v1/quota").json()["observations"]
    assert any(item["account_identity"]["handle"] == account_handle for item in all_accounts)
    assert all("credential" not in str(item).lower() for item in all_accounts)
    unavailable = {item["account_identity"]["handle"]: item for item in all_accounts}[identity_handle(OWNER, "account_id", "acct-unused")]
    assert unavailable["state"] == "unavailable"
    unknown = client.get("/api/stats/v1/quota", params={"account_id": "missing"})
    assert unknown.status_code == 422


def test_current_session_handoff_returns_owner_bound_no_store_handle(factory, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    with factory() as db:
        db.add(DbSession(id="session-current", name="Current", endpoint_url="fixture", model="fixture-model", owner=OWNER))
        db.commit()
    app = FastAPI()
    app.include_router(setup_stats_routes(session_factory=factory))
    client = TestClient(app)

    response = client.post("/api/stats/v1/session-handle", json={"session_id": "session-current"})
    assert response.status_code == 200
    handle = response.json()["handle"]
    assert handle.startswith("session_")
    assert "session-current" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert client.post("/api/stats/v1/session-handle", json={"session_id": "missing"}).status_code == 404
    assert client.get("/api/stats/v1/quota", params={"session_id": handle}).status_code == 200
    assert client.get("/api/stats/v1/quota", params={"session_id": "session-current"}).status_code == 422


def test_provider_tombstones_erase_account_and_connection_quota_rows(factory):
    from src.openclank.provider_store import ProviderStore

    admit(factory, window_kind="continuous", reset_at=None)
    store = ProviderStore(factory)
    store.delete_account(owner=OWNER, account_id="acct-a", expected_revision=1)
    with factory() as db:
        assert db.query(StatsQuotaObservation).filter_by(account_id="acct-a").count() == 0
        assert db.query(StatsQuotaCycle).filter_by(account_id="acct-a").count() == 0
        row = db.get(ProviderAccount, "acct-a")
        assert row.deleted_at is not None

    # A separate live account proves connection deletion purges all linked
    # facts in the same SessionLocal transaction, rather than only tombstoning.
    with factory() as db:
        db.add(ProviderAccount(id="acct-c", connection_id="conn-a", owner=OWNER,
                               label="fixture", auth_method="fixture", auth_class="fixture",
                               credential_envelope=None, safe_identity={}))
        db.commit()
    with factory() as db:
        admit_quota_observation(db, owner=OWNER, account_id="acct-c", provider_id="provider-a",
                                window_id="w", window_kind="continuous", observed_at=T0,
                                source_kind="official", adapter_revision="fixture-v1")
        db.commit()
    store.delete_connection(owner=OWNER, connection_id="conn-a", expected_revision=2)
    with factory() as db:
        assert db.query(StatsQuotaObservation).filter_by(account_id="acct-c").count() == 0
        assert db.get(ProviderConnection, "conn-a").deleted_at is not None


def test_provider_tombstone_failure_rolls_back_account_and_quota(factory):
    from sqlalchemy.orm import Session
    from src.openclank.provider_store import ProviderStore

    admit(factory, window_kind="continuous", reset_at=None)

    class FailingSession(Session):
        def flush(self, *args, **kwargs):
            raise RuntimeError("fixture flush failure")

    failing_factory = sessionmaker(bind=factory.kw["bind"], class_=FailingSession)
    with pytest.raises(RuntimeError, match="flush failure"):
        ProviderStore(failing_factory).delete_account(owner=OWNER, account_id="acct-a",
                                                       expected_revision=1)
    with factory() as db:
        assert db.get(ProviderAccount, "acct-a").deleted_at is None
        assert db.query(StatsQuotaObservation).filter_by(account_id="acct-a").count() == 1


def test_owner_purge_removes_quota_rows_without_touching_other_owner(factory):
    with factory() as db:
        db.add(StatsQuotaObservation(id="alice-fact", replay_key="alice-fact", owner="alice",
                                     account_id="acct-a", provider_id="provider-a", window_id="w",
                                     window_kind="continuous", observed_at=T0, source_kind="local",
                                     adapter_revision="fixture", state="local"))
        db.add(StatsQuotaObservation(id="bob-fact", replay_key="bob-fact", owner="bob",
                                     account_id="acct-a", provider_id="provider-a", window_id="w",
                                     window_kind="continuous", observed_at=T0, source_kind="local",
                                     adapter_revision="fixture", state="local"))
        db.commit()
        result = erase_owner_quota(db, "alice")
        db.commit()
        assert result["observations"] == 1
        assert db.query(StatsQuotaObservation).filter_by(owner="alice").count() == 0
        assert db.query(StatsQuotaObservation).filter_by(owner="bob").count() == 1


def test_common_owner_lifecycle_renames_and_purges_quota_rows(factory):
    from src.openclank.sql_owner_lifecycle import CommonSqlOwnerLifecycle

    with factory() as db:
        db.add(StatsQuotaObservation(id="rename-a", replay_key="rename-a", owner="alice",
                                     account_id="acct-a", provider_id="provider-a", window_id="w",
                                     window_kind="continuous", observed_at=T0, source_kind="local",
                                     adapter_revision="v", state="local"))
        db.add(StatsQuotaObservation(id="rename-b", replay_key="rename-b", owner="bob",
                                     account_id="acct-a", provider_id="provider-a", window_id="w",
                                     window_kind="continuous", observed_at=T0, source_kind="local",
                                     adapter_revision="v", state="local"))
        db.commit()
    lifecycle = CommonSqlOwnerLifecycle(factory)
    lifecycle.rename_owner("alice", "alice-renamed")
    with factory() as db:
        assert db.query(StatsQuotaObservation).filter_by(owner="alice").count() == 0
        assert db.query(StatsQuotaObservation).filter_by(owner="alice-renamed").count() == 1
        assert db.query(StatsQuotaObservation).filter_by(owner="bob").count() == 1
    lifecycle.purge_owner("alice-renamed")
    with factory() as db:
        assert db.query(StatsQuotaObservation).filter_by(owner="alice-renamed").count() == 0
        assert db.query(StatsQuotaObservation).filter_by(owner="bob").count() == 1


def test_snapshot_fair_keys_and_official_headline_behind_noisy_local_history(factory):
    with factory() as db:
        db.add(ProviderConnection(id="conn-b", owner=OWNER, family_id="provider-b",
                                  adapter_id="fixture", kind="fixture", billing_lane="api",
                                  label="fixture"))
        db.flush()
        db.add(ProviderAccount(id="acct-b", connection_id="conn-b", owner=OWNER,
                               label="b", auth_method="fixture", auth_class="fixture",
                               credential_envelope=None, safe_identity={}))
        rows = [StatsQuotaObservation(id="official", replay_key="official", owner=OWNER,
                                      account_id="acct-a", provider_id="provider-a", window_id="same",
                                      window_kind="continuous", observed_at=T0, source_kind="official",
                                      adapter_revision="v", state="official", utilization_numerator=1,
                                      utilization_denominator=10)]
        for i in range(1001):
            rows.append(StatsQuotaObservation(id=f"a-{i}", replay_key=f"a-{i}", owner=OWNER,
                         account_id="acct-a", provider_id="provider-a", window_id=f"noise-{i}",
                         window_kind="continuous", observed_at=T0 + timedelta(minutes=i + 1),
                         source_kind="local", adapter_revision="v", state="local"))
        rows.append(StatsQuotaObservation(id="b-one", replay_key="b-one", owner=OWNER,
                     account_id="acct-b", provider_id="provider-b", window_id="same",
                     window_kind="continuous", observed_at=T0, source_kind="official",
                     adapter_revision="v", state="official"))
        for i in range(101):
            rows.append(StatsQuotaObservation(id=f"local-{i}", replay_key=f"local-{i}", owner=OWNER,
                         account_id="acct-a", provider_id="provider-a", window_id="same",
                         window_kind="continuous", observed_at=T0 + timedelta(minutes=i + 2),
                         source_kind="local", adapter_revision="v", state="local",
                         utilization_numerator=2, utilization_denominator=10))
        db.add_all(rows)
        db.commit()
        report = quota_snapshot(db, owner=OWNER, now=T0 + timedelta(minutes=2000),
                                stale_after_seconds=10**9)
    by_key = {(row["account_id"], row["window_id"]): row for row in report["observations"]}
    assert ("acct-b", "same") in by_key
    assert by_key[("acct-b", "same")]["state"] == "official"
    assert by_key[("acct-a", "same")]["state"] == "official"
    assert by_key[("acct-a", "same")]["utilization_numerator"] == 1
    assert by_key[("acct-a", "same")]["diagnostics_truncated"] is True
    assert by_key[("acct-a", "same")]["signed_delta"]["numerator"] == 1


def test_retention_cap_keeps_latest_official_and_bounds_diagnostics(factory):
    with factory() as db:
        for i in range(5):
            db.add(StatsQuotaObservation(id=f"ret-{i}", replay_key=f"ret-{i}", owner=OWNER,
                         account_id="acct-a", provider_id="provider-a", window_id="ret",
                         window_kind="continuous", observed_at=T0 + timedelta(minutes=i),
                         source_kind="local", adapter_revision="v", state="local"))
        db.add(StatsQuotaObservation(id="ret-official", replay_key="ret-official", owner=OWNER,
                     account_id="acct-a", provider_id="provider-a", window_id="ret",
                     window_kind="continuous", observed_at=T0, source_kind="official",
                     adapter_revision="v", state="official"))
        db.commit()
        result = enforce_quota_retention(db, owner=OWNER, max_observations_per_key=2)
        db.commit()
        assert result["observations"] == 4
        assert db.get(StatsQuotaObservation, "ret-official") is not None
        assert db.query(StatsQuotaObservation).filter_by(window_id="ret").count() == 2
