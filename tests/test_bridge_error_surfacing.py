"""Provider errors from mimo must reach the chat stream, not vanish.

Live failure 2026-07-19: z.ai's weekly quota 429 ("Weekly/Monthly Limit
Exhausted…") died inside mimo — session.prompt resolved as a clean empty
end_turn, the bus-level session.error never crossed the ACP seam, and the
user stared at silence. mimo now forwards session.error / session.retry.attempt
as _odysseus_error / _odysseus_retry session updates; the bridge renders them
as an SSE error event (with the real provider message) and a retry_notice.
"""
import json
from pathlib import Path

import pytest

from src.openclank.acp_bridge import ACPBridge, _TurnState

_REPO = Path(__file__).resolve().parent.parent


class _Client:
    def register_callback(self, *_args):
        pass

    def on_session_update(self, *_args):
        pass


@pytest.fixture
def bridge(tmp_path):
    return ACPBridge(_Client(), str(tmp_path), owner="alice")


def test_odysseus_error_becomes_sse_error_with_provider_message(bridge):
    state = _TurnState()
    message = "Weekly/Monthly Limit Exhausted. Your limit will reset at 2026-07-23 03:49:14"
    sses = bridge._process_update(
        "ses",
        {"sessionUpdate": "_odysseus_error", "name": "APIError", "message": message},
        state,
    )
    assert len(sses) == 1
    event_line, data_line = sses[0].strip().split("\n")
    assert event_line == "event: error"
    payload = json.loads(data_line[len("data: "):])
    assert payload["error"] == message
    assert payload["status"] == 502
    assert state.error_streamed is True


def test_odysseus_error_falls_back_to_name(bridge):
    state = _TurnState()
    sses = bridge._process_update(
        "ses", {"sessionUpdate": "_odysseus_error", "name": "UnknownError"}, state
    )
    payload = json.loads(sses[0].strip().split("\n")[1][len("data: "):])
    assert payload["error"] == "UnknownError"


def test_odysseus_retry_becomes_retry_notice(bridge):
    state = _TurnState()
    sses = bridge._process_update(
        "ses",
        {
            "sessionUpdate": "_odysseus_retry",
            "attempt": 3,
            "maxAttempts": 10,
            "reason": "Too Many Requests",
            "nextDelayMs": 2000,
        },
        state,
    )
    assert len(sses) == 1
    event = json.loads(sses[0][len("data: "):])
    assert event["type"] == "retry_notice"
    assert event["data"]["attempt"] == 3
    assert event["data"]["maxAttempts"] == 10
    assert "sessionUpdate" not in event["data"]
    assert state.error_streamed is False
