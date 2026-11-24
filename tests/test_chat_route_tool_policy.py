"""Issue #3229 and explicit web-toggle regressions.

Bug: allow_bash and allow_web_search were only read from form_data, so JSON
API callers (Content-Type: application/json) always had bash disabled.

Fix: (1) Read from JSON body as fallback.
     (2) Keep bash on the privilege fallback when unset.
     (3) Require an explicit per-turn web setting before exposing web tools.
"""

import ast
from pathlib import Path

_CHAT_ROUTES = Path(__file__).resolve().parent.parent / "routes" / "chat_routes.py"


# ── Source-level guards ─────────────────────────────────────────


def test_allow_bash_reads_from_body_as_fallback():
    """chat_stream must read allow_bash from the JSON body, not just form_data."""
    source = _CHAT_ROUTES.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Find the chat_stream function
    chat_stream_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "chat_stream":
            chat_stream_func = node
            break
    assert chat_stream_func is not None, "chat_stream function not found"

    # Look for an assignment to allow_bash that references 'body'
    found_body_fallback = False
    for node in ast.walk(chat_stream_func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "allow_bash":
                    # Check if 'body' appears in the value
                    src_segment = ast.get_source_segment(source, node)
                    if src_segment and "body" in src_segment:
                        found_body_fallback = True
    assert found_body_fallback, (
        "allow_bash assignment in chat_stream must fall back to JSON body"
    )


def test_allow_web_search_reads_from_body_as_fallback():
    """chat_stream must read allow_web_search from the JSON body, not just form_data."""
    source = _CHAT_ROUTES.read_text(encoding="utf-8")
    tree = ast.parse(source)

    chat_stream_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "chat_stream":
            chat_stream_func = node
            break
    assert chat_stream_func is not None

    found_body_fallback = False
    for node in ast.walk(chat_stream_func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "allow_web_search":
                    src_segment = ast.get_source_segment(source, node)
                    if src_segment and "body" in src_segment:
                        found_body_fallback = True
    assert found_body_fallback, (
        "allow_web_search assignment in chat_stream must fall back to JSON body"
    )


def test_browser_form_followups_include_approval_and_send_phrases():
    """Short approval replies after a form/browser turn must keep browser tools available."""
    source = _CHAT_ROUTES.read_text(encoding="utf-8")
    assert "approved" in source
    assert "proceed" in source
    assert "send(?:\\s+it)?" in source
    assert "submit(?:\\s+it)?" in source


def test_agent_loop_expands_browser_mcp_tools_from_connected_server():
    """Browser intent must not depend on stale hardcoded Playwright tool names."""
    source = (Path(__file__).resolve().parent.parent / "src" / "agent_loop.py").read_text(encoding="utf-8")
    assert "def _expand_browser_mcp_tools" in source
    assert "server_id\") == \"builtin_browser\"" in source
    assert "_relevant_tools = _expand_browser_mcp_tools(_relevant_tools, mcp_mgr)" in source


def test_disabled_tools_respects_missing_vs_explicit_toggles():
    """Bash still defers to privileges, but web is an explicit per-turn opt-in.
    """
    source = _CHAT_ROUTES.read_text(encoding="utf-8")

    assert '"allowBash": None if allow_bash is None' in source, (
        "the canonical snapshot must preserve the Bash inherit state"
    )
    assert '"allowWebSearch": None if allow_web_search is None' in source
    assert "resolve_turn_policy(" in source, "chat sends must use the shared policy resolver"
    assert "_forced_tools = set(WEB_TOOL_NAMES)" in source, (
        "web tools should only be forced visible from the explicit web setting"
    )


def test_chat_mode_is_a_read_only_lane_not_an_error():
    """Chat mode is a first-class read-only lane (QOL-pass slice 3): the route
    accepts chat/plan/agent, rejects unknown modes, and chat applies the same
    fail-closed read-only tool gate as plan mode. Shell authority stays under
    the normal privilege gate for agent mode."""
    source = _CHAT_ROUTES.read_text(encoding="utf-8")
    assert 'if requested_mode not in ("", "agent", "chat", "plan"):' in source
    policy_source = (Path(__file__).resolve().parents[1] / "src" / "tool_policy.py").read_text(encoding="utf-8")
    assert "chat_mode_disabled_tools" in policy_source
    assert "resolve_turn_policy(" in source
    assert "Chat mode was removed" not in source
    assert "if not _privs.get(\"can_use_bash\", True):" in source


def test_streaming_receipt_freezes_turn_choices_before_async_work():
    source = (Path(__file__).resolve().parent.parent / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    start = source.index("async function _submitStreamingReceipt")
    end = source.index("export function queueStreamingComposerRequest", start)
    receipt_flow = source[start:end]
    assert receipt_flow.index("const turnPolicy = _captureTurnPolicy()") < receipt_flow.index("await _thinkingSavePromise")
    assert "uploadIDs, mode, turnPolicy" in receipt_flow
    assert "...(request.turnPolicy ? { turnPolicy: request.turnPolicy } : {})" in source
    assert "questionRefs: [ref], questionAnswers: [answer], actorID, actorSessionID," in source
    assert "      turnPolicy,\n      mode: isStreaming && !_inputQueueEnabled ? 'steer' : 'after-turn'," in source


def test_active_copal_pointer_is_injected_for_classic_and_acp_agents():
    """Copal context must not be hidden behind the ACP-only branch."""
    source = _CHAT_ROUTES.read_text(encoding="utf-8")
    messages_line = source.index("messages = list(_ensure_current_request_is_latest_user")
    pointer_line = source.index('"active Copal resource pointer"', messages_line)
    transport_line = source.index('if model_target.transport == "acp":', messages_line)
    assert pointer_line < transport_line
