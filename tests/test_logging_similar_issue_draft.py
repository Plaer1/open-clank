"""BQ-19: explicit, bounded similar-session comparison and local issue draft."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.logging_routes import setup_logging_routes
from services.logging.projection import LoggingProjection
from src.openclank.conversation_archive import ConversationArchive, SourcePart


def _tool(owner, chat, status, seq):
    return SourcePart(owner=owner, chat_id=chat, actor_id="main", message_id="m" + chat, part_id="t", part_type="tool",
                      content={"mimo": {"part": {"tool": "read", "state": {"status": status, "output": "SECRET-BODY", "time": {"start": 1, "end": 2}}}}},
                      event_sequence=seq)


@pytest.fixture
def client(tmp_path):
    archive = ConversationArchive(str(tmp_path / "archive.sqlite3"))
    archive.append_parts([
        _tool("alice", "chat-a", "error", 1), _tool("alice", "chat-c", "error", 2),
        _tool("alice", "chat-d", "completed", 3), _tool("bob", "chat-b", "error", 4),
    ])
    projection = LoggingProjection(archive.db_path, cursor_secret=b"fixture-secret")
    app = FastAPI()
    app.include_router(setup_logging_routes(projection=projection, owner_resolver=lambda r: r.headers.get("x-test-owner")))
    return TestClient(app), projection


def test_similar_is_owner_scoped_bounded_and_text_free(client):
    http, _ = client
    alice = {"x-test-owner": "alice"}
    handles = {i["handle"]: i for i in http.get("/api/logging/v1/sessions", headers=alice).json()["items"]}
    assert len(handles) == 3  # bob's session is never visible to alice
    target = next(h for h, i in handles.items() if i["tools"]["failures"] == 1 and h)
    result = http.post(f"/api/logging/v1/sessions/{target}/similar", headers=alice).json()
    assert result["stored"] is False and result["bounds"]["max_sessions"] == 50
    assert all(row["matched_fields"] == ["tool_failure"] for row in result["items"])
    assert len(result["items"]) == 1 and result["items"][0]["handle"] != target
    assert "SECRET-BODY" not in str(result)
    assert "error_class_stage" in result["unavailable_fields"]
    assert http.post(f"/api/logging/v1/sessions/{target}/similar").status_code == 401
    assert http.post(f"/api/logging/v1/sessions/{target}/similar", headers={"x-test-owner": "bob"}).status_code == 404


def test_issue_draft_is_local_markdown_without_conversation_text(client):
    http, _ = client
    alice = {"x-test-owner": "alice"}
    items = http.get("/api/logging/v1/sessions", headers=alice).json()["items"]
    target = next(i["handle"] for i in items if i["tools"]["failures"] == 1)
    draft = http.post(f"/api/logging/v1/sessions/{target}/issue-draft", headers=alice).json()
    assert draft["sent"] is False and draft["stored"] is False
    text = draft["draft_markdown"]
    assert text.startswith("# Issue draft") and target in text and "tool_failure: 1" in text
    assert "SECRET-BODY" not in text and "chat-a" not in text
