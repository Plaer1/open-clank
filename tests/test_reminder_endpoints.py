from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from src import reminder_endpoints


def test_legacy_rows_migrate_to_stable_v1_ids_and_aliases():
    first = reminder_endpoints.normalize_endpoints([
        {"channel": "email", "recipient": "Team@Example.com"},
        {"channel": "ntfy", "topic": "reminders"},
    ])
    second = reminder_endpoints.normalize_endpoints(first)
    assert first["version"] == 1
    assert [row["id"] for row in first["endpoints"]] == [row["id"] for row in second["endpoints"]]
    assert first["endpoints"][0]["email_to"] == "Team@Example.com"


def test_duplicate_destination_and_invalid_draft_are_rejected():
    with pytest.raises(reminder_endpoints.ReminderEndpointError, match="duplicates"):
        reminder_endpoints.normalize_endpoints([
            {"channel": "browser"},
            {"channel": "browser"},
        ])
    with pytest.raises(reminder_endpoints.ReminderEndpointError, match="valid JSON"):
        reminder_endpoints.normalize_endpoints([
            {"channel": "webhook", "integration_id": "hook", "payload_template": "{"},
        ])


def test_supplied_endpoint_ids_are_unique_and_safe_for_receipt_keys():
    with pytest.raises(reminder_endpoints.ReminderEndpointError, match="id collides"):
        reminder_endpoints.normalize_endpoints([
            {"id": "same", "channel": "email", "email_to": "a@example.test"},
            {"id": "same", "channel": "email", "email_to": "b@example.test"},
        ])
    with pytest.raises(reminder_endpoints.ReminderEndpointError, match="id must be"):
        reminder_endpoints.normalize_endpoints([
            {"id": "bad id", "channel": "browser"},
        ])


def test_receipt_claim_is_serialized_and_unknown_requires_deliberate_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(reminder_endpoints, "DATA_DIR", str(tmp_path))
    first = reminder_endpoints.claim_receipt("alice", "note:occurrence", "email-1")
    second = reminder_endpoints.claim_receipt("alice", "note:occurrence", "email-1")
    assert first["claimed"] is True
    assert second["claimed"] is False
    assert second["unknown"] is False
    path = reminder_endpoints.receipt_path("alice")
    payload = json.loads(path.read_text())
    key = first["key"]
    payload[key]["claimed_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(payload))
    stale = reminder_endpoints.claim_receipt("alice", "note:occurrence", "email-1")
    assert stale["claimed"] is False and stale["unknown"] is True
    retried = reminder_endpoints.claim_receipt(
        "alice", "note:occurrence", "email-1", retry_unknown=True
    )
    assert retried["claimed"] is True
    reminder_endpoints.finish_receipt(retried, "sent")
    sent = reminder_endpoints.claim_receipt("alice", "note:occurrence", "email-1")
    assert sent["claimed"] is False
    assert sent["status"] == "sent"


def test_old_attempt_cannot_finish_over_an_explicit_unknown_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(reminder_endpoints, "DATA_DIR", str(tmp_path))
    old = reminder_endpoints.claim_receipt("alice", "occurrence", "email-1")
    path = reminder_endpoints.receipt_path("alice")
    payload = json.loads(path.read_text())
    payload[old["key"]]["claimed_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(payload))
    retry = reminder_endpoints.claim_receipt("alice", "occurrence", "email-1", retry_unknown=True)
    assert retry["claimed"] is True and retry["attempt_token"] != old["attempt_token"]
    reminder_endpoints.finish_receipt(old, "sent")
    still_sending = json.loads(path.read_text())[old["key"]]
    assert still_sending["status"] == "sending"
    reminder_endpoints.finish_receipt(retry, "sent")
    assert json.loads(path.read_text())[old["key"]]["status"] == "sent"


def test_concurrent_claims_have_one_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(reminder_endpoints, "DATA_DIR", str(tmp_path))

    def claim():
        return reminder_endpoints.claim_receipt("alice", "same", "browser-1")

    with ThreadPoolExecutor(max_workers=6) as pool:
        claims = list(pool.map(lambda _: claim(), range(6)))
    assert sum(item["claimed"] for item in claims) == 1


def test_dispatch_fanout_delivers_two_same_channel_endpoints_once(tmp_path, monkeypatch):
    import routes.note.note_routes as note_routes
    import src.settings as settings

    monkeypatch.setattr(reminder_endpoints, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(note_routes, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "load_settings", lambda: {
        "reminder_channel": "browser",
        "reminder_endpoints": {
            "version": 1,
            "endpoints": [
                {"id": "email-a", "channel": "email", "email_to": "a@example.test"},
                {"id": "email-b", "channel": "email", "email_to": "b@example.test"},
            ],
        },
    })
    monkeypatch.setattr("routes.email_routes._get_email_config", lambda **_: {
        "smtp_host": "smtp.example.test", "smtp_user": "sender@example.test",
        "smtp_password": "secret", "from_address": "sender@example.test",
    })
    monkeypatch.setattr("routes.email_routes._smtp_ready", lambda cfg: True)
    deliveries = []
    monkeypatch.setattr("routes.email_helpers._send_smtp_message", lambda cfg, sender, recipients, raw: deliveries.append(recipients[0]))

    import asyncio
    result = asyncio.run(note_routes.dispatch_reminder(
        "Title", "Body", "note-1", owner="alice", occurrence_key="note-1:2026-09-07"
    ))
    assert result["aggregate"] == "sent"
    assert deliveries == ["a@example.test", "b@example.test"]
    again = asyncio.run(note_routes.dispatch_reminder(
        "Title", "Body", "note-1", owner="alice", occurrence_key="note-1:2026-09-07"
    ))
    assert again["aggregate"] == "skipped"
    assert all(row["status"] == "skipped" for row in again["endpoints"])
    assert deliveries == ["a@example.test", "b@example.test"]


def test_partial_failure_retries_only_failed_endpoint(tmp_path, monkeypatch):
    import routes.note.note_routes as note_routes
    import src.settings as settings
    monkeypatch.setattr(reminder_endpoints, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(note_routes, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "load_settings", lambda: {
        "reminder_channel": "browser",
        "reminder_endpoints": {"version": 1, "endpoints": [
            {"id": "email-a", "channel": "email", "email_to": "a@example.test"},
            {"id": "email-b", "channel": "email", "email_to": "b@example.test"},
        ]},
    })
    monkeypatch.setattr("routes.email_routes._get_email_config", lambda **_: {
        "smtp_host": "smtp.example.test", "smtp_user": "sender@example.test",
        "smtp_password": "secret", "from_address": "sender@example.test",
    })
    monkeypatch.setattr("routes.email_routes._smtp_ready", lambda cfg: True)
    deliveries = []
    def flaky(cfg, sender, recipients, raw):
        if recipients[0] == "b@example.test":
            raise RuntimeError("temporary SMTP failure")
        deliveries.append(recipients[0])
    monkeypatch.setattr("routes.email_helpers._send_smtp_message", flaky)
    import asyncio
    first = asyncio.run(note_routes.dispatch_reminder("T", "B", "n", owner="alice", occurrence_key="n:o"))
    assert first["aggregate"] == "partial"
    assert deliveries == ["a@example.test"]
    monkeypatch.setattr("routes.email_helpers._send_smtp_message", lambda cfg, sender, recipients, raw: deliveries.append(recipients[0]))
    second = asyncio.run(note_routes.dispatch_reminder("T", "B", "n", owner="alice", occurrence_key="n:o"))
    assert second["aggregate"] == "sent"
    assert deliveries == ["a@example.test", "b@example.test"]


def test_ntfy_fanout_uses_selected_integration_stub(tmp_path, monkeypatch):
    import asyncio
    import routes.note.note_routes as note_routes
    import src.settings as settings

    monkeypatch.setattr(reminder_endpoints, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(note_routes, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "load_settings", lambda: {
        "reminder_endpoints": {"version": 1, "endpoints": [
            {"id": "ntfy-2-target", "channel": "ntfy", "ntfy_topic": "alerts", "ntfy_integration_id": "ntfy-2"},
        ]},
    })
    monkeypatch.setattr("src.integrations.load_integrations", lambda: [
        {"id": "ntfy-1", "preset": "ntfy", "base_url": "http://127.0.0.1:8091", "enabled": True},
        {"id": "ntfy-2", "preset": "ntfy", "base_url": "http://127.0.0.1:8092", "enabled": True},
    ])
    monkeypatch.setattr("src.url_safety.check_outbound_url", lambda *_args, **_kwargs: (True, ""))
    calls = []

    class _Response:
        is_success = True

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            calls.append(url)
            return _Response()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _Client())
    result = asyncio.run(note_routes.dispatch_reminder("T", "B", "n", owner="alice", occurrence_key="n:o"))
    assert result["aggregate"] == "sent"
    assert calls == ["http://127.0.0.1:8092/alerts"]


def test_ntfy_missing_selected_integration_does_not_fall_back_to_another_target(
    tmp_path, monkeypatch
):
    import asyncio
    import routes.note.note_routes as note_routes
    import src.settings as settings

    monkeypatch.setattr(reminder_endpoints, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(note_routes, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "load_settings", lambda: {
        "reminder_endpoints": {"version": 1, "endpoints": [
            {
                "id": "ntfy-missing-target",
                "channel": "ntfy",
                "ntfy_topic": "alerts",
                "ntfy_integration_id": "owned-by-someone-else",
            },
        ]},
    })
    monkeypatch.setattr("src.integrations.load_integrations", lambda: [
        {
            "id": "alice-ntfy",
            "preset": "ntfy",
            "base_url": "http://127.0.0.1:8092",
            "enabled": True,
        },
    ])
    monkeypatch.setattr("src.url_safety.check_outbound_url", lambda *_args, **_kwargs: (True, ""))
    calls = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            calls.append(url)
            raise AssertionError("inaccessible selected integration was used")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _Client())
    result = asyncio.run(note_routes.dispatch_reminder("T", "B", "n", owner="alice", occurrence_key="n:o"))
    assert result["aggregate"] == "error"
    assert result["endpoints"] == [{
        "id": "ntfy-missing-target",
        "channel": "ntfy",
        "status": "error",
        "error": "Selected ntfy integration is unavailable",
    }]
    assert calls == []
