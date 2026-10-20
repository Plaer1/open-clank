"""Disposable managed logging qualification; no live app bootstrap/provider calls."""
import os
from pathlib import Path
import tempfile

# This entrypoint is safe even with pytest --noconftest: establish disposable
# authority BEFORE core.database or anything which may import its initializer.
_fixture = Path(tempfile.mkdtemp(prefix="openclank-capture-probe-"))
for _key, _value in {"OPEN_CLANK_DATA_DIR": str(_fixture / "data"),
                     "DATABASE_URL": "sqlite:///" + str(_fixture / "initializer.sqlite3"),
                     "FM_DB_PATH": str(_fixture / "fm.sqlite3")}.items():
    os.environ[_key] = _value

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.database import Base
from core.stats_models import StatsEvent
from services.stats.ledger import capture_operation_result, project_admitted_events
from src.openclank.logging_models import LoggingAttemptDetail, LoggingCaptureBody, LoggingCaptureEvent
from src.openclank.logging_policy import LoggingPolicyStore
from src.openclank.logging_capture_store import LoggingCaptureStore, CaptureError
from src.openclank.sql_owner_lifecycle import CommonSqlOwnerLifecycle


def test_managed_capture_policy_gap_retention_and_owner_lifecycle(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "captures.sqlite3"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    preferences = {}
    policy = LoggingPolicyStore(loader=lambda owner: preferences.get(owner, {}), saver=lambda owner, value: preferences.__setitem__(owner, value))
    policy.update_policy("a", {"advanced_enabled": True}, expected_revision=1)
    store = LoggingCaptureStore(factory, policy_store=policy)
    context = {"owner": "a", "holder_id": "worker", "root_operation_id": "root", "operation_id": "operation", "operation_type": "chat"}
    binding = SimpleNamespace(root_operation_id="root", provider_id="openai", connection_id="connection", selected_account_id="account", billing_lane="api", model_id="selected-model")
    admission = store.admit(owner="a", holder_id="worker", context=context, binding=binding, attempt_id="attempt", wire_format="responses.v1")
    payload = {"admissionID": admission["admissionID"], "sequence": 1, "terminal": True, "outcome": "completed", "metrics": {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 5}, "actualModel": "returned-model", "timing": {"terminal_ms": 12.6},
               "requestBody": {"password": "must disappear", "source": {"type": "base64", "data": "YWJj"}}, "responseBody": {"content": "hello"}, "events": [{"text": "hello"}]}
    with pytest.raises(CaptureError):
        store.events(owner="b", holder_id="worker", payload=payload)
    assert store.events(owner="a", holder_id="worker", payload=payload)["accepted"]
    assert store.events(owner="a", holder_id="worker", payload=payload)["replayed"]
    listing = store.attempts("a", limit=1)
    handle = listing["items"][0]["handle"]
    resource = store.attempt("a", handle, include_bodies=True)["resource"]
    assert resource["actual_model"] == "returned-model" and resource["requested_model"] == "selected-model"
    request = json.loads(next(body["text"] for body in resource["bodies"] if body["category"] == "request"))
    assert "password" not in request and request["source"]["data"]["omitted"] == "embedded_media"
    db = factory()
    before = project_admitted_events(db.query(StatsEvent).filter_by(owner="a").all())
    assert sum((row.input_tokens or 0)+(row.output_tokens or 0) for row in before.events) == 120
    # Actual SDK-operation producer observation reconciles with wire evidence.
    sdk = SimpleNamespace(state="complete", operation_id="operation", root_operation_id="root", model_route_id="route", selected_account_id="account", billing_lane="metered_api", normalization_profile="openai-wire-inclusive-v1", model_fingerprint=None, usage={"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 5})
    capture_operation_result(db, SimpleNamespace(owner="a"), sdk)
    db.commit()
    reconciled = project_admitted_events(db.query(StatsEvent).filter_by(owner="a").all())
    assert sum((row.input_tokens or 0)+(row.output_tokens or 0) for row in reconciled.events) == 120
    assert len(reconciled.excluded) == 1
    db.close()
    store.finish_operation("a", "worker", "root")
    # A missing capture terminal must not invent a provider cancellation.
    gap = store.admit(owner="a", holder_id="worker", context=context, binding=binding, attempt_id="gap", wire_format="responses.v1")
    store.events(owner="a", holder_id="worker", payload={"admissionID": gap["admissionID"], "sequence": 0})
    store.finish_operation("a", "worker", "root")
    db = factory()
    assert db.query(StatsEvent).filter_by(owner="a", event_kind="cancellation").count() == 0
    gap_row = db.query(LoggingAttemptDetail).filter_by(attempt_id="gap").one()
    assert gap_row.outcome is None and gap_row.capture_state == "dropped"
    assert not store.admissions
    # Body-OFF still retains numeric usage but cannot persist raw event text.
    policy.update_policy("a", {"response_body_enabled": False, "binary_body_enabled": True}, expected_revision=2)
    third = store.admit(owner="a", holder_id="worker", context=context, binding=binding, attempt_id="body-off", wire_format="responses.v1")
    store.events(owner="a", holder_id="worker", payload={**payload, "admissionID": third["admissionID"], "requestBody": {"source": {"type": "base64", "data": "YWJj"}}})
    third_id = store.admissions[third["admissionID"]]["detail_id"]
    assert db.query(LoggingCaptureEvent).filter_by(attempt_detail_id=third_id).count() == 0
    assert db.query(LoggingCaptureBody).filter_by(attempt_detail_id=third_id, category="response").count() == 0
    third_resource = store.attempt("a", third_id, include_bodies=True)["resource"]
    assert "YWJj" in third_resource["bodies"][0]["text"]
    db.expire_all()
    distinct = project_admitted_events(db.query(StatsEvent).filter_by(owner="a").all())
    assert sum((row.input_tokens or 0)+(row.output_tokens or 0) for row in distinct.events) == 240
    assert len({row.attempt_id for row in distinct.events if row.attempt_id}) == 3
    # Disable new operation admissions while existing immutable policy drains.
    policy.update_policy("a", {"ordinary_enabled": False}, expected_revision=3)
    assert policy.logging_status("a")["draining"]
    drained = store.admit(owner="a", holder_id="worker", context=context, binding=binding, attempt_id="drain", wire_format="responses.v1")
    assert drained["admissionID"]
    new_binding = SimpleNamespace(**{**vars(binding), "root_operation_id": "new-root"})
    fresh = store.admit(owner="a", holder_id="worker", context={**context, "root_operation_id": "new-root", "operation_id": "new-operation"}, binding=new_binding, attempt_id="new", wire_format="responses.v1")
    assert fresh["admissionID"] == ""
    # Host typed-operation incognito propagation reaches admission before call.
    from src.openclank.mimo_supervisor import MimoSupervisor
    class Callbacks:
        def register_logging_operation(self, root, supplied):
            private = store.admit(owner="a", holder_id="worker", context={**context, **supplied, "root_operation_id": root}, binding=binding, attempt_id="private", wire_format="responses.v1")
            assert private["admissionID"] == "" and not private["policy"]["ordinary_enabled"]
        def finish_logging_operation(self, root):
            pass
    class Client:
        async def execute_managed_operation(self, payload):
            return {"ok": True}
    supervisor = SimpleNamespace(_client=Client(), _managed_callbacks=Callbacks(), is_alive=lambda: True)
    assert asyncio.run(MimoSupervisor.execute_operation(supervisor, {"rootOperationID": "root", "operation": "text.generate", "options": {"incognito": True}}))["ok"]
    # Optional raw-detail failure is isolated from already-admitted usage.
    failed_detail = store.admit(owner="a", holder_id="worker", context=context, binding=binding, attempt_id="detail-failure", wire_format="responses.v1")
    missing_id = store.admissions[failed_detail["admissionID"]]["detail_id"]
    db.query(LoggingAttemptDetail).filter_by(id=missing_id).delete()
    db.commit()
    assert store.events(owner="a", holder_id="worker", payload={**payload, "admissionID": failed_detail["admissionID"]})["accepted"]
    assert store.status("a")["losses"]["write_failed"] >= 1
    store.recover_generation("a", "next-worker")
    assert store.attempt("a", store.admissions[drained["admissionID"]]["detail_id"])["resource"]["capture_state"] == "dropped"
    from src.openclank.logging_models import recover_pending_captures
    db.add(LoggingAttemptDetail(id="unclean-restart", owner="a", instance_id="old-worker", operation_id="past", attempt_id="past", root_operation_id="past", policy_revision=1, attribution={}, loss_reasons=["truncated"]))
    db.commit()
    assert recover_pending_captures(engine) == 1
    db.expire_all()
    restarted = db.query(LoggingAttemptDetail).filter_by(id="unclean-restart").one()
    assert restarted.capture_state == "dropped" and restarted.outcome is None and restarted.loss_reasons == ["truncated", "interrupted"]
    assert db.query(LoggingAttemptDetail).filter_by(id=handle).one().capture_state == "complete"
    fixture_path = os.environ.get("LOGGING_UI_FIXTURE_PATH")
    if fixture_path:
        resources = [store.attempt("a", item["handle"], include_bodies=True) for item in store.attempts("a")["items"]]
        Path(fixture_path).write_text(json.dumps({"attempts": store.attempts("a"), "resources": resources}, indent=2))
    preview = store.prune(owner="a", action="preview", payload={"target": "advanced_bodies", "retention": {"mode": "size", "max_bytes": 1}})
    assert preview["preview"]["owner_body_bytes"] > 0
    applied = store.prune(owner="a", action="apply", payload={"target": "advanced_bodies", "preview_id": preview["preview_id"]})
    assert applied["applied"]["body_bytes"] > 0
    assert db.query(StatsEvent).filter_by(owner="a").count() == 5
    db.close()
    lifecycle = CommonSqlOwnerLifecycle(factory)
    assert any(binding.model is LoggingAttemptDetail for binding in lifecycle.bindings)
    lifecycle.rename_owner("a", "renamed")
    assert store.attempt("renamed", handle)["resource"]["handle"] == handle
    with pytest.raises(CaptureError):
        store.attempt("a", handle)
    lifecycle.purge_owner("renamed")
    db = factory()
    assert db.query(LoggingAttemptDetail).count() == 0
    assert db.query(LoggingCaptureBody).count() == 0
    assert db.query(LoggingCaptureEvent).count() == 0
    db.close()
    # Shared preference authority merges a concurrent ordinary key with policy.
    import routes.prefs_routes as prefs
    prefs.PREFS_FILE = str(tmp_path / "prefs.json")
    real = LoggingPolicyStore()
    with ThreadPoolExecutor(max_workers=2) as pool:
        logging = pool.submit(real.update_policy, "owner", {"advanced_enabled": True}, expected_revision=1)
        ordinary = pool.submit(prefs._update_for_user, "owner", {"theme": "dark"})
        logging.result(); ordinary.result()
    saved = prefs._load_for_user("owner")
    assert saved["theme"] == "dark" and saved["logging_preferences"]["advanced_enabled"]
