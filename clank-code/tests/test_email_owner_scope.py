import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


def _route_endpoint(router, path: str, method: str):
    method = method.upper()
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def test_email_model_inference_has_no_direct_provider_execution_paths():
    for path in (Path("routes/email_routes.py"), Path("mcp_servers/email_server.py")):
        source = path.read_text(encoding="utf-8")
        assert "llm_call_async" not in source
        assert "resolve_endpoint" not in source
        assert "resolve_utility_fallback_candidates" not in source
        assert "resolve_chat_fallback_candidates" not in source
        assert "from src.openclank.modality_facade import complete_text" in source


@pytest.mark.asyncio
async def test_email_summary_uses_owner_scoped_managed_utility_completion(monkeypatch):
    import routes.email_routes as email_routes
    from src.openclank import modality_facade

    calls = []

    async def complete_text(**kwargs):
        calls.append(kwargs)
        return "<<<SUMMARY>>>\n- Reply by Friday\n<<<END>>>"

    monkeypatch.setattr(modality_facade, "complete_text", complete_text)
    monkeypatch.setattr(
        email_routes,
        "_managed_email_model_label",
        lambda owner: "managed-test-model",
    )

    summarize = _route_endpoint(
        email_routes.setup_email_routes(),
        "/api/email/summarize",
        "POST",
    )
    result = await summarize(
        {
            "body": "Please reply by Friday.",
            "subject": "Deadline",
            "from": "sender@example.com",
            "root_operation_id": "root-email-summary-1",
        },
        owner="alice",
    )

    assert result == {
        "success": True,
        "summary": "- Reply by Friday",
        "model_used": "managed-test-model",
    }
    assert calls[0]["owner"] == "alice"
    assert calls[0]["purpose"] == "utility"
    assert calls[0]["max_output_tokens"] == 8192
    assert calls[0]["root_operation_id"] == "root-email-summary-1"
    assert not {"url", "headers", "api_key"}.intersection(calls[0])


@pytest.mark.asyncio
async def test_email_style_extraction_uses_managed_utility_completion(monkeypatch):
    import routes.email_helpers as email_helpers
    import routes.email_routes as email_routes
    from src.openclank import modality_facade

    calls = []
    saved = {}
    raw = (
        b"From: Alice <alice@example.com>\r\n"
        b"Subject: Sample\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Hello there, this sample email is comfortably long enough."
    )

    class FakeImap:
        def select(self, *_args, **_kwargs):
            return "OK", []

        def search(self, *_args, **_kwargs):
            return "OK", [b"1 2 3"]

        def fetch(self, *_args, **_kwargs):
            return "OK", [(b"1 (RFC822)", raw)]

    @contextmanager
    def fake_imap(_account_id=None, owner=""):
        assert owner == "alice"
        yield FakeImap()

    async def complete_text(**kwargs):
        calls.append(kwargs)
        return "Write emails in this style: concise and warm."

    monkeypatch.setattr(email_routes, "_imap", fake_imap)
    monkeypatch.setattr(email_routes, "_load_settings", lambda: {})
    monkeypatch.setattr(
        email_routes,
        "_save_settings",
        lambda settings: saved.update(settings),
    )
    monkeypatch.setattr(modality_facade, "complete_text", complete_text)

    extract_style = _route_endpoint(
        email_routes.setup_email_routes(),
        "/api/email/extract-style",
        "POST",
    )
    result = await extract_style(
        email_helpers.ExtractStyleRequest(sample_count=3),
        account_id=None,
        owner="alice",
    )

    assert result["success"] is True
    assert saved["email_writing_style"].startswith("Write emails in this style:")
    assert calls[0]["owner"] == "alice"
    assert calls[0]["purpose"] == "utility"
    assert calls[0]["max_output_tokens"] == 2048
    assert not {"url", "headers", "api_key"}.intersection(calls[0])


@pytest.mark.asyncio
async def test_email_translation_uses_managed_utility_completion(
    tmp_path,
    monkeypatch,
):
    import routes.email_helpers as email_helpers
    import routes.email_routes as email_routes
    from src.openclank import modality_facade

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    monkeypatch.setattr(email_routes, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()
    calls = []

    async def complete_text(**kwargs):
        calls.append(kwargs)
        return "<<<TRANSLATION>>>\nBonjour.\n<<<END>>>"

    monkeypatch.setattr(modality_facade, "complete_text", complete_text)
    monkeypatch.setattr(
        email_routes,
        "_managed_email_model_label",
        lambda owner: "managed-test-model",
    )

    translate = _route_endpoint(
        email_routes.setup_email_routes(),
        "/api/email/translate",
        "POST",
    )
    result = await translate(
        {
            "body": "Hello.",
            "target_language": "French",
            "root_operation_id": "root-email-translation-1",
        },
        owner="alice",
    )

    assert result == {
        "success": True,
        "translation": "Bonjour.",
        "language": "French",
        "model_used": "managed-test-model",
    }
    assert calls[0]["owner"] == "alice"
    assert calls[0]["purpose"] == "utility"
    assert calls[0]["max_output_tokens"] == 8192
    assert calls[0]["root_operation_id"] == "root-email-translation-1"
    assert not {"url", "headers", "api_key"}.intersection(calls[0])


@pytest.mark.asyncio
async def test_email_ai_reply_preserves_limits_and_root_on_managed_completion(monkeypatch):
    import routes.email_routes as email_routes
    from src.openclank import chat_routing, modality_facade

    calls = []

    async def complete_text(**kwargs):
        calls.append(kwargs)
        return "<<<REPLY>>>\nThanks, I'll reply Friday.\n<<<END>>>"

    monkeypatch.setattr(modality_facade, "complete_text", complete_text)
    monkeypatch.setattr(
        chat_routing,
        "resolve_chat_model_spec",
        lambda **kwargs: SimpleNamespace(
            model_route_id="managed-route-1",
            provider_grant_id="grant-1",
            display_name="Managed Test Model",
        ),
    )
    monkeypatch.setattr(email_routes, "_load_settings", lambda: {})
    monkeypatch.setattr(
        email_routes,
        "_managed_email_model_label",
        lambda owner, **kwargs: "managed-test-model",
    )

    ai_reply = _route_endpoint(
        email_routes.setup_email_routes(),
        "/api/email/ai-reply",
        "POST",
    )
    result = await ai_reply(
        {
            "to": "sender@example.com",
            "subject": "Deadline",
            "original_body": "Please reply by Friday.",
            "fast": True,
            "model": "managed-route-1",
            "rootOperationID": "root-email-reply-1",
        },
        owner="alice",
    )

    assert result == {
        "success": True,
        "reply": "Thanks, I'll reply Friday.",
        "model_used": "managed-test-model",
    }
    assert calls[0]["owner"] == "alice"
    assert calls[0]["purpose"] == "utility"
    assert calls[0]["max_output_tokens"] == 1024
    assert calls[0]["model_route_id"] == "managed-route-1"
    assert calls[0]["grant_id"] == "grant-1"
    assert calls[0]["root_operation_id"] == "root-email-reply-1"
    assert not {"url", "headers", "api_key"}.intersection(calls[0])


@pytest.mark.asyncio
async def test_email_ai_reply_retries_only_blank_draft_with_same_root(monkeypatch):
    import routes.email_routes as email_routes
    from src.openclank import modality_facade

    calls = []
    outputs = iter(
        (
            "<<<REPLY>>>\n<<<END>>>",
            "<<<REPLY>>>\nUsable retry.\n<<<END>>>",
        )
    )

    async def complete_text(**kwargs):
        calls.append(kwargs)
        return next(outputs)

    monkeypatch.setattr(modality_facade, "complete_text", complete_text)
    monkeypatch.setattr(email_routes, "_load_settings", lambda: {})
    monkeypatch.setattr(
        email_routes,
        "_managed_email_model_label",
        lambda owner, **kwargs: "managed-test-model",
    )

    ai_reply = _route_endpoint(
        email_routes.setup_email_routes(),
        "/api/email/ai-reply",
        "POST",
    )
    result = await ai_reply(
        {
            "to": "sender@example.com",
            "subject": "Deadline",
            "original_body": "Please reply by Friday.",
            "fast": True,
            "root_operation_id": "root-email-retry-1",
        },
        owner="alice",
    )

    assert result["reply"] == "Usable retry."
    assert len(calls) == 2
    assert [call["max_output_tokens"] for call in calls] == [1024, 1536]
    assert {call["root_operation_id"] for call in calls} == {
        "root-email-retry-1"
    }


def test_email_tag_clause_excludes_legacy_owner_rows_for_authenticated_owner(monkeypatch):
    import routes.email_routes as email_routes

    monkeypatch.setattr(
        email_routes,
        "_email_tag_owner_aliases",
        lambda account_id, owner="": ["alice", "alice@example.com"],
    )

    clause, params = email_routes._email_tag_owner_clause("acct-alice", "alice")

    assert clause == "owner IN (?,?)"
    assert params == ["alice", "alice@example.com"]
    assert "owner IS NULL" not in clause


def test_email_tag_clause_keeps_legacy_rows_for_single_user_mode(monkeypatch):
    import routes.email_routes as email_routes

    monkeypatch.setattr(
        email_routes,
        "_email_tag_owner_aliases",
        lambda account_id, owner="": [""],
    )

    clause, params = email_routes._email_tag_owner_clause(None, "")

    assert clause == "(owner IN (?) OR owner IS NULL)"
    assert params == [""]


def test_email_ai_cache_tables_are_owner_scoped_and_migrate_legacy_rows(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE email_summaries (
            message_id TEXT PRIMARY KEY,
            uid TEXT,
            folder TEXT,
            subject TEXT,
            sender TEXT,
            summary TEXT NOT NULL,
            model_used TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO email_summaries
        (message_id, uid, folder, subject, sender, summary, model_used, created_at)
        VALUES ('<shared@example.com>', '1', 'INBOX', 'Subject', 'a@example.com', 'legacy', 'm', '2026-01-01')
        """
    )
    conn.commit()
    conn.close()

    email_helpers._init_scheduled_db()

    conn = sqlite3.connect(db_path)
    try:
        for table in (
            "email_summaries",
            "email_ai_replies",
            "email_calendar_extractions",
            "email_urgency_alerts",
        ):
            info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            pk_cols = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
            assert pk_cols == ["message_id", "owner"]
        assert conn.execute(
            "SELECT owner, summary FROM email_summaries WHERE message_id=?",
            ("<shared@example.com>",),
        ).fetchone() == ("", "legacy")

        conn.execute(
            """
            INSERT INTO email_summaries
            (message_id, owner, uid, folder, subject, sender, summary, model_used, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("<shared@example.com>", "alice", "2", "INBOX", "Subject", "a@example.com", "alice", "m", "2026-01-02"),
        )
        conn.execute(
            """
            INSERT INTO email_summaries
            (message_id, owner, uid, folder, subject, sender, summary, model_used, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("<shared@example.com>", "bob", "3", "INBOX", "Subject", "a@example.com", "bob", "m", "2026-01-03"),
        )
        rows = conn.execute(
            "SELECT owner, summary FROM email_summaries WHERE message_id=? ORDER BY owner",
            ("<shared@example.com>",),
        ).fetchall()
        assert rows == [("", "legacy"), ("alice", "alice"), ("bob", "bob")]
    finally:
        conn.close()


def test_sender_signature_cache_is_owner_scoped_and_migrates_legacy_rows(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE sender_signatures (
            from_address TEXT PRIMARY KEY,
            signature_text TEXT,
            sample_count INTEGER,
            last_built_at TEXT NOT NULL,
            model_used TEXT,
            source TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO sender_signatures
        (from_address, signature_text, sample_count, last_built_at, model_used, source)
        VALUES ('writer@example.com', 'legacy sig', 3, '2026-01-01', 'm', 'llm')
        """
    )
    conn.commit()
    conn.close()

    email_helpers._init_scheduled_db()

    conn = sqlite3.connect(db_path)
    try:
        info = conn.execute("PRAGMA table_info(sender_signatures)").fetchall()
        pk_cols = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
        assert pk_cols == ["from_address", "owner"]
        assert conn.execute(
            "SELECT owner, signature_text FROM sender_signatures WHERE from_address=?",
            ("writer@example.com",),
        ).fetchone() == ("", "legacy sig")
        conn.execute(
            """
            INSERT INTO sender_signatures
            (from_address, owner, signature_text, sample_count, last_built_at, model_used, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("writer@example.com", "alice", "alice sig", 3, "2026-01-02", "m", "llm"),
        )
        conn.execute(
            """
            INSERT INTO sender_signatures
            (from_address, owner, signature_text, sample_count, last_built_at, model_used, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("writer@example.com", "bob", "bob sig", 3, "2026-01-03", "m", "llm"),
        )
        rows = conn.execute(
            "SELECT owner, signature_text FROM sender_signatures WHERE from_address=? ORDER BY owner",
            ("writer@example.com",),
        ).fetchall()
        assert rows == [("", "legacy sig"), ("alice", "alice sig"), ("bob", "bob sig")]
    finally:
        conn.close()


def test_email_message_index_is_owner_account_folder_scoped(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)

    email_helpers._init_scheduled_db()

    conn = sqlite3.connect(db_path)
    try:
        info = conn.execute("PRAGMA table_info(email_message_index)").fetchall()
        pk_cols = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
        assert pk_cols == ["owner", "account_key", "folder", "uid"]

        conn.execute(
            """
            INSERT INTO email_message_index
            (owner, account_key, folder, uid, message_id, subject, updated_at)
            VALUES ('alice', 'acct-a', 'INBOX', '7', '<same@example.com>', 'Alice', '2026-01-01')
            """
        )
        conn.execute(
            """
            INSERT INTO email_message_index
            (owner, account_key, folder, uid, message_id, subject, updated_at)
            VALUES ('bob', 'acct-a', 'INBOX', '7', '<same@example.com>', 'Bob', '2026-01-01')
            """
        )
        rows = conn.execute(
            "SELECT owner, subject FROM email_message_index WHERE account_key='acct-a' AND folder='INBOX' AND uid='7' ORDER BY owner"
        ).fetchall()
        assert rows == [("alice", "Alice"), ("bob", "Bob")]
    finally:
        conn.close()


def test_email_index_helpers_roundtrip_and_update_flags(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers
    import routes.email_routes as email_routes

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    monkeypatch.setattr(email_routes, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()

    email_routes._email_index_upsert(
        "alice",
        "acct-a",
        "INBOX",
        [{
            "uid": "11",
            "message_id": "<m@example.com>",
            "subject": "Cached",
            "from_name": "Sender",
            "from_address": "sender@example.com",
            "to": "alice@example.com",
            "cc": "",
            "date": "2026-01-01T00:00:00+00:00",
            "date_display": "Thu, 1 Jan 2026 00:00:00 +0000",
            "date_epoch": 1767225600,
            "size": 123,
            "flags": "\\Seen",
            "has_attachments": True,
        }],
    )

    rows = email_routes._email_index_rows("alice", "acct-a", "INBOX", ["11", "12"])
    assert rows["11"]["subject"] == "Cached"
    assert rows["11"]["is_read"] is True
    assert rows["11"]["has_attachments"] is True

    email_routes._email_index_update_flags("alice", "acct-a", "INBOX", "11", "\\Seen", False)
    email_routes._email_index_update_flags("alice", "acct-a", "INBOX", "11", "\\Flagged", True)
    rows = email_routes._email_index_rows("alice", "acct-a", "INBOX", ["11"])
    assert rows["11"]["is_read"] is False
    assert rows["11"]["is_flagged"] is True

    email_routes._email_index_delete("alice", "acct-a", "INBOX", "11")
    assert email_routes._email_index_rows("alice", "acct-a", "INBOX", ["11"]) == {}


@pytest.mark.asyncio
async def test_ai_reply_cache_lookup_is_owner_scoped(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers
    import routes.email_routes as email_routes

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    monkeypatch.setattr(email_routes, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO email_ai_replies
        (message_id, owner, uid, folder, reply, model_used, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("<shared@example.com>", "alice", "1", "INBOX", "alice private draft", "m-a", "2026-01-01"),
    )
    conn.execute(
        """
        INSERT INTO email_ai_replies
        (message_id, owner, uid, folder, reply, model_used, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("<shared@example.com>", "bob", "2", "INBOX", "bob private draft", "m-b", "2026-01-02"),
    )
    conn.commit()
    conn.close()

    router = email_routes.setup_email_routes()
    ai_reply = _route_endpoint(router, "/api/email/ai-reply", "POST")

    result = await ai_reply(
        {
            "to": "sender@example.com",
            "subject": "Subject",
            "original_body": "Body",
            "message_id": "<shared@example.com>",
        },
        owner="bob",
    )

    assert result["success"] is True
    assert result["cached"] is True
    assert result["reply"] == "bob private draft"
    assert result["model_used"] == "m-b"


@pytest.mark.asyncio
async def test_sender_signature_read_lookup_is_owner_scoped(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers
    import routes.email_routes as email_routes

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    monkeypatch.setattr(email_routes, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO sender_signatures
        (from_address, owner, signature_text, sample_count, last_built_at, model_used, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("writer@example.com", "alice", "alice private sig", 3, "2026-01-01", "m-a", "llm"),
    )
    conn.execute(
        """
        INSERT INTO sender_signatures
        (from_address, owner, signature_text, sample_count, last_built_at, model_used, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("writer@example.com", "bob", "bob private sig", 3, "2026-01-02", "m-b", "llm"),
    )
    conn.commit()
    conn.close()

    raw = (
        b"From: Writer <writer@example.com>\r\n"
        b"To: Bob <bob@example.com>\r\n"
        b"Subject: Hello\r\n"
        b"Message-ID: <shared@example.com>\r\n"
        b"Date: Tue, 01 Jan 2026 12:00:00 +0000\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Body"
    )

    class FakeImap:
        def select(self, *_args, **_kwargs):
            return "OK", []

        def uid(self, command, _uid, query):
            assert command == "FETCH"
            assert query.startswith("(BODY.PEEK[HEADER] BODY.PEEK[TEXT]<0.")
            header, body = raw.split(b"\r\n\r\n", 1)
            return "OK", [
                (b"1 (UID 1 BODY[HEADER])", header + b"\r\n\r\n"),
                (b"1 (UID 1 BODY[TEXT]<0>)", body),
            ]

    @contextmanager
    def fake_imap(_account_id=None, owner=""):
        assert owner == "bob"
        yield FakeImap()

    monkeypatch.setattr(email_routes, "_imap", fake_imap)
    router = email_routes.setup_email_routes()
    read_email = _route_endpoint(router, "/api/email/read/{uid}", "GET")

    result = await read_email("1", folder="INBOX", account_id=None, owner="bob", mark_seen=False)

    assert result["sender_signature"] == "bob private sig"


@pytest.mark.asyncio
async def test_sender_signature_clear_cache_keeps_other_owner_rows(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers
    import routes.task_routes as task_routes

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO sender_signatures
        (from_address, owner, signature_text, sample_count, last_built_at, model_used, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("writer@example.com", "alice", "alice private sig", 3, "2026-01-01", "m-a", "llm"),
    )
    conn.execute(
        """
        INSERT INTO sender_signatures
        (from_address, owner, signature_text, sample_count, last_built_at, model_used, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("writer@example.com", "bob", "bob private sig", 3, "2026-01-02", "m-b", "llm"),
    )
    conn.commit()
    conn.close()

    class FakeQuery:
        def filter(self, *_args):
            return self

        def first(self):
            return SimpleNamespace(
                id="task-1",
                owner="alice",
                action="learn_sender_signatures",
            )

    class FakeDb:
        def query(self, _model):
            return FakeQuery()

        def close(self):
            pass

    monkeypatch.setattr(task_routes, "SessionLocal", lambda: FakeDb())
    monkeypatch.setattr(task_routes, "get_current_user", lambda _request: "alice")

    router = task_routes.setup_task_routes(task_scheduler=SimpleNamespace(pop_notifications=lambda owner: []))
    clear_cache = _route_endpoint(router, "/api/tasks/{task_id}/clear-cache", "POST")

    result = await clear_cache(SimpleNamespace(), "task-1")

    assert result["cleared"]["sender_signatures"] == 1
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT owner, signature_text FROM sender_signatures ORDER BY owner",
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("bob", "bob private sig")]


@pytest.mark.asyncio
async def test_scheduled_email_routes_are_owner_scoped(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers
    import routes.email_routes as email_routes

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    monkeypatch.setattr(email_routes, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()

    router = email_routes.setup_email_routes()
    schedule_email = _route_endpoint(router, "/api/email/schedule", "POST")
    list_scheduled = _route_endpoint(router, "/api/email/scheduled", "GET")
    cancel_scheduled = _route_endpoint(router, "/api/email/scheduled/{sid}", "DELETE")

    send_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    alice = await schedule_email(
        {"to": "a@example.com", "body": "alice body", "send_at": send_at},
        owner="alice",
    )
    bob = await schedule_email(
        {"to": "b@example.com", "body": "bob body", "send_at": send_at},
        owner="bob",
    )

    assert alice["success"] is True
    assert bob["success"] is True

    alice_rows = await list_scheduled(owner="alice")
    bob_rows = await list_scheduled(owner="bob")

    assert [row["id"] for row in alice_rows["scheduled"]] == [alice["id"]]
    assert [row["id"] for row in bob_rows["scheduled"]] == [bob["id"]]

    await cancel_scheduled(bob["id"], owner="alice")
    bob_rows = await list_scheduled(owner="bob")
    assert [row["id"] for row in bob_rows["scheduled"]] == [bob["id"]]

    await cancel_scheduled(alice["id"], owner="alice")
    alice_rows = await list_scheduled(owner="alice")
    assert alice_rows["scheduled"] == []


@pytest.mark.asyncio
async def test_pending_agent_draft_routes_do_not_expose_ownerless_rows(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers
    import routes.email_routes as email_routes

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    monkeypatch.setattr(email_routes, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()

    conn = sqlite3.connect(db_path)
    conn.executemany(
        """
        INSERT INTO scheduled_emails
        (id, to_addr, subject, body, attachments, send_at, created_at, status, account_id, owner)
        VALUES (?, ?, ?, ?, '[]', '9999-12-31T00:00:00', ?, 'agent_draft', ?, ?)
        """,
        [
            ("draft-ownerless", "nobody@example.com", "Ownerless", "old", "2026-01-01", "acct-a", ""),
            ("draft-bob", "bob@example.com", "Bob", "bob body", "2026-01-02", "acct-b", "bob"),
        ],
    )
    conn.commit()
    conn.close()

    router = email_routes.setup_email_routes()
    list_pending = _route_endpoint(router, "/api/email/pending", "GET")
    approve_pending = _route_endpoint(router, "/api/email/pending/{sid}/approve", "POST")
    cancel_pending = _route_endpoint(router, "/api/email/pending/{sid}", "DELETE")

    alice_rows = await list_pending(owner="alice")
    bob_rows = await list_pending(owner="bob")

    assert alice_rows["pending"] == []
    assert [row["id"] for row in bob_rows["pending"]] == ["draft-bob"]
    assert (await approve_pending("draft-ownerless", owner="alice"))["success"] is False
    assert (await cancel_pending("draft-ownerless", owner="bob"))["success"] is False

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, status FROM scheduled_emails ORDER BY id",
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("draft-bob", "agent_draft"), ("draft-ownerless", "agent_draft")]


@pytest.mark.asyncio
async def test_pending_agent_draft_routes_block_cross_owner_actions(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers
    import routes.email_routes as email_routes

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    monkeypatch.setattr(email_routes, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()

    conn = sqlite3.connect(db_path)
    conn.executemany(
        """
        INSERT INTO scheduled_emails
        (id, to_addr, subject, body, attachments, send_at, created_at, status, account_id, owner)
        VALUES (?, ?, ?, ?, '[]', '9999-12-31T00:00:00', ?, 'agent_draft', ?, ?)
        """,
        [
            ("draft-alice", "alice@example.com", "Alice", "alice body", "2026-01-01", "acct-a", "alice"),
            ("draft-bob", "bob@example.com", "Bob", "bob body", "2026-01-02", "acct-b", "bob"),
        ],
    )
    conn.commit()
    conn.close()

    router = email_routes.setup_email_routes()
    list_pending = _route_endpoint(router, "/api/email/pending", "GET")
    approve_pending = _route_endpoint(router, "/api/email/pending/{sid}/approve", "POST")
    cancel_pending = _route_endpoint(router, "/api/email/pending/{sid}", "DELETE")

    alice_rows = await list_pending(owner="alice")
    assert [row["id"] for row in alice_rows["pending"]] == ["draft-alice"]

    assert (await approve_pending("draft-bob", owner="alice"))["success"] is False
    assert (await cancel_pending("draft-bob", owner="alice"))["success"] is False

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, status, send_at FROM scheduled_emails ORDER BY id",
        ).fetchall()
    finally:
        conn.close()
    assert rows == [
        ("draft-alice", "agent_draft", "9999-12-31T00:00:00"),
        ("draft-bob", "agent_draft", "9999-12-31T00:00:00"),
    ]


def test_scheduled_poller_resolves_config_with_row_owner(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers
    import routes.email_pollers as email_pollers

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    monkeypatch.setattr(email_pollers, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO scheduled_emails
        (id, to_addr, subject, body, attachments, send_at, created_at, status, account_id, owner)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            "sched-1",
            "recipient@example.com",
            "Subject",
            "Body",
            "[]",
            "2000-01-01T00:00:00",
            "1999-12-31T00:00:00",
            "acct-alice",
            "alice",
        ),
    )
    conn.commit()
    conn.close()

    calls = []

    def fake_get_email_config(account_id=None, owner=""):
        calls.append(("config", account_id, owner))
        return {
            "from_address": "alice@example.com",
            "smtp_host": "smtp.example.com",
            "smtp_user": "alice@example.com",
            "smtp_password": "secret",
        }

    class FakeImap:
        def __init__(self, account_id=None, owner=""):
            calls.append(("imap", account_id, owner))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def append(self, folder, flags, date_time, message):
            calls.append(("append", folder))

    monkeypatch.setattr(email_pollers, "_get_email_config", fake_get_email_config)
    monkeypatch.setattr(email_pollers, "_send_smtp_message", lambda *args, **kwargs: calls.append(("send", args[1], args[2])))
    monkeypatch.setattr(email_pollers, "_imap", FakeImap)
    monkeypatch.setattr(email_pollers, "_detect_sent_folder", lambda imap: "Sent")
    monkeypatch.setattr(email_pollers, "_cleanup_compose_uploads", lambda attachments: calls.append(("cleanup", attachments)))

    result = email_pollers._scheduled_poll_once()

    assert result == {"sent": ["sched-1"], "failed": []}
    assert ("config", "acct-alice", "alice") in calls
    assert ("imap", "acct-alice", "alice") in calls


def test_scheduled_poller_filters_blank_rows_by_effective_account_owner(tmp_path, monkeypatch):
    import routes.email_helpers as email_helpers
    import routes.email_pollers as email_pollers

    db_path = tmp_path / "scheduled-effective-owner.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    monkeypatch.setattr(email_pollers, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()

    conn = sqlite3.connect(db_path)
    for sid, account_id, owner in (
        ("sched-alice-blank", "acct-alice", ""),
        ("sched-bob-blank", "acct-bob", ""),
    ):
        conn.execute(
            """
            INSERT INTO scheduled_emails
            (id, to_addr, subject, body, attachments, send_at, created_at, status, account_id, owner)
            VALUES (?, ?, ?, ?, '[]', ?, ?, 'pending', ?, ?)
            """,
            (sid, "recipient@example.com", sid, "Body", "2000-01-01T00:00:00", "1999-12-31T00:00:00", account_id, owner),
        )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        email_pollers,
        "_owner_for_email_account",
        lambda account_id: {"acct-alice": "alice", "acct-bob": "bob"}[account_id],
    )
    monkeypatch.setattr(
        email_pollers,
        "_get_email_config",
        lambda account_id=None, owner="": {"from_address": f"{owner}@example.com"},
    )

    class _NoopImap:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def append(self, *_args):
            return None

    monkeypatch.setattr(email_pollers, "_send_smtp_message", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(email_pollers, "_imap", lambda *_args, **_kwargs: _NoopImap())
    monkeypatch.setattr(email_pollers, "_detect_sent_folder", lambda _imap: "Sent")
    monkeypatch.setattr(email_pollers, "_cleanup_compose_uploads", lambda _attachments: None)

    result = email_pollers._scheduled_poll_once(owner="alice")

    assert result == {"sent": ["sched-alice-blank"], "failed": []}
    conn = sqlite3.connect(db_path)
    statuses = dict(conn.execute("SELECT id, status FROM scheduled_emails"))
    conn.close()
    assert statuses == {"sched-alice-blank": "sent", "sched-bob-blank": "pending"}
