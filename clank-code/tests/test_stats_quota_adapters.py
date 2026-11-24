from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
from core.provider_models import ProviderAccount, ProviderBase, ProviderConnection, ProviderOperationBinding
from core.stats_models import StatsEvent, StatsQuotaObservation
from core.database import Session as DbSession
from services.stats.quota import quota_snapshot
from services.stats.quota import QuotaError
from services.stats.quota_adapters import (NormalizedQuotaObservation,
                                            adapt_anthropic_capacity,
                                            adapt_capacity_error,
                                            adapt_openai_capacity,
                                            admit_terminal_quota,
                                            admit_terminal_quota_payload,
                                            native_capacity_envelope)
from src.openclank.operation_router import ManagedOperationRequest, ManagedOperationRouter, OperationRoute
from core.stats_models import StatsQuotaObservation


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'adapter.db'}")
    Base.metadata.create_all(engine)
    ProviderBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(ProviderConnection(id="conn", owner="alice", family_id="openai",
                                  adapter_id="fixture", kind="fixture", billing_lane="api",
                                  label="fixture"))
        db.flush()
        db.add(ProviderAccount(id="acct", connection_id="conn", owner="alice", label="fixture",
                               auth_method="fixture", auth_class="fixture", safe_identity={}))
        db.flush()
        db.add(ProviderOperationBinding(id="binding", owner="alice", credential_owner="alice",
                                        root_operation_id="op-1", connection_id="conn",
                                        provider_id="openai", billing_lane="api", model_id="model",
                                        selected_account_id="acct", state="committed",
                                        committed_at=datetime(2026, 1, 1)))
        db.commit()
    return factory


def test_documented_adapters_are_separate_and_reject_raw_secrets():
    payload = {"requests": {"limit": 100, "remaining": 80},
               "tokens": {"limit": 1000, "remaining": 900}}
    assert {item.window_id for item in adapt_openai_capacity(payload, observed_at=datetime(2026, 1, 1))} == {"requests", "tokens"}
    assert {item.window_id for item in adapt_anthropic_capacity(payload, observed_at=datetime(2026, 1, 1))} == {"requests", "tokens"}
    with pytest.raises(QuotaError, match="sensitive"):
        adapt_openai_capacity({**payload, "authorization": "Bearer secret"}, observed_at=datetime(2026, 1, 1))
    assert adapt_capacity_error(429, observed_at=datetime(2026, 1, 1)).error_class == "rate_limited"
    assert adapt_capacity_error("timeout", observed_at=datetime(2026, 1, 1)).source_kind == "error"


def test_native_terminal_capacity_envelope_is_allowlisted_and_versioned():
    payload = {"requests": {"limit": 100, "remaining": 80}}
    envelope = native_capacity_envelope("openai", payload,
                                       observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert envelope["adapter_revision"] == "openai-capacity-v1"
    assert envelope["observations"][0]["remaining_value"] == 80
    with pytest.raises(QuotaError):
        native_capacity_envelope("openai", {"headers": {"authorization": "secret"}},
                                 observed_at=datetime(2026, 1, 1))


def test_terminal_admission_derives_identity_from_committed_binding(tmp_path):
    factory = _factory(tmp_path)
    observation = NormalizedQuotaObservation(
        window_id="tokens", window_kind="continuous", observed_at=datetime(2026, 1, 1),
        source_kind="official", adapter_revision="fixture-v1", limit_value=1000,
        remaining_value=900,
    )
    with factory() as db:
        row = admit_terminal_quota(db, owner="alice", operation_id="op-1", observation=observation)
        db.commit()
        assert row.account_id == "acct"
        assert row.provider_id == "openai"
        assert row.operation_id == "op-1"
        assert db.query(StatsQuotaObservation).count() == 1
    with factory() as db:
        with pytest.raises(QuotaError, match="binding"):
            admit_terminal_quota(db, owner="bob", operation_id="op-1", observation=observation)


def test_trusted_terminal_envelope_reaches_ledger_without_caller_identity(tmp_path):
    factory = _factory(tmp_path)
    envelope = {
        "adapter_revision": "fixture-native-v2",
        "observed_at": "2026-01-01T00:00:00+00:00",
        "observations": [{"window_id": "tokens", "window_kind": "continuous",
                          "source_kind": "official", "limit_value": 1000,
                          "remaining_value": 900}],
    }
    with factory() as db:
        rows = admit_terminal_quota_payload(db, owner="alice", operation_id="op-1",
                                             payload=envelope)
        db.commit()
        assert len(rows) == 1
        assert rows[0].account_id == "acct"
        assert rows[0].provider_id == "openai"
        assert rows[0].operation_id == "op-1"
        assert "authorization" not in rows[0].__dict__
    with factory() as db:
        with pytest.raises(QuotaError):
            admit_terminal_quota_payload(db, owner="alice", operation_id="op-1",
                                         payload={**envelope, "owner": "attacker"})


def test_managed_terminal_quota_reaches_host_ledger_with_distinct_root(tmp_path):
    factory = _factory(tmp_path)
    with factory() as db:
        db.add(ProviderOperationBinding(id="binding-root", owner="alice", credential_owner="alice",
                                        root_operation_id="root-1", connection_id="conn",
                                        provider_id="openai", billing_lane="api", model_id="model",
                                        selected_account_id="acct", state="committed",
                                        committed_at=datetime(2026, 1, 1)))
        db.commit()
    route = OperationRoute("conn", "openai", "api", "route", "model")

    async def execute(_owner, _payload):
        return {"operationID": "terminal-1", "rootOperationID": "root-1",
                "operation": "text.generate", "state": "complete", "committed": True,
                "replayed": False, "modelRouteID": "route", "connectionID": "conn",
                "billingLane": "api", "selectedAccountID": "acct", "output": {"text": "ok"},
                "usage": {}, "quota": {"requests": {"limit": 100, "remaining": 80}}}

    router = ManagedOperationRouter(session_factory=factory, executor=execute)
    router._resolve_routes = lambda _request: (route,)
    request = ManagedOperationRequest(owner="alice", operation="text.generate", input={},
                                      root_operation_id="root-1", idempotency_key="idempotency-key-1")
    import asyncio
    result = asyncio.run(router.execute(request))
    assert "_openclank_quota" not in result.output
    with factory() as db:
        row = db.query(StatsQuotaObservation).one()
        assert row.operation_id == "terminal-1"
        assert row.account_id == "acct"


def test_quota_snapshot_projects_only_owner_attributed_current_session(tmp_path):
    factory = _factory(tmp_path)
    with factory() as db:
        db.add(DbSession(id="session-a", name="A", endpoint_url="fixture", model="model", owner="alice"))
        db.add(DbSession(id="session-b", name="B", endpoint_url="fixture", model="model", owner="bob"))
        db.add(StatsEvent(id="event-a", replay_key="event-a", owner="alice", session_id="session-a",
                          event_kind="response", observation_scope="message", event_time=datetime(2026, 1, 1),
                          source="fixture", producer_revision="fixture-v1", input_tokens=7, output_tokens=5,
                          input_tokens_state="reported", output_tokens_state="reported", token_state="reported"))
        db.add(StatsEvent(id="event-b", replay_key="event-b", owner="bob", session_id="session-b",
                          event_kind="response", observation_scope="message", event_time=datetime(2026, 1, 1),
                          source="fixture", producer_revision="fixture-v1", input_tokens=99, output_tokens=99,
                          input_tokens_state="reported", output_tokens_state="reported", token_state="reported"))
        db.commit()
        alice = quota_snapshot(db, owner="alice", session_id="session-a")
        assert alice["current_session"] == {"attributed": True, "tokens": 12, "token_state": "reported", "cost": {"state": "unpriced"}, "event_count": 1}
        assert quota_snapshot(db, owner="alice", session_id="session-b")["current_session"]["attributed"] is False
