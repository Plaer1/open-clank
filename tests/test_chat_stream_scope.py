from pathlib import Path
import re


def test_stream_render_helpers_are_visible_to_catch_block():
    source = Path("static/js/chat.js").read_text(encoding="utf-8")
    try_start = source.index("    try {\n      // Re-enable auto-scroll")
    catch_start = source.index("    } catch (err) {", try_start)

    outer_scope = source[:try_start]
    try_body = source[try_start:catch_start]

    assert "let _renderStream = () => {};" in outer_scope
    assert "let _cancelThinkingTimer = () => {};" in outer_scope
    assert "let _removeThinkingSpinner = () => {};" in outer_scope
    assert "let abortCtrl = null;" in outer_scope
    assert "const _isAgent = true;" in outer_scope
    assert "let streamingTTS = false;" in outer_scope

    assert "_renderStream = () => {" in try_body
    assert "_cancelThinkingTimer = () => {" in try_body
    assert "_removeThinkingSpinner = () => {" in try_body
    assert "abortCtrl = new AbortController();" in try_body
    assert "streamingTTS = !!" in try_body
    assert "const abortCtrl =" not in try_body
    assert "const streamingTTS =" not in try_body
    assert "function _renderStream()" not in try_body


def test_chat_module_graph_uses_one_url_per_stateful_module():
    files = [
        Path("static/index.html"),
        Path("static/app.js"),
        Path("static/js/chat.js"),
        Path("static/js/sessions.js"),
        Path("static/js/cookbookServe.js"),
        Path("static/js/group.js"),
        Path("static/js/slashCommands.js"),
        Path("static/js/models.js"),
        Path("static/js/compare/stream.js"),
        Path("static/js/compare/vote.js"),
    ]
    sources = "\n".join(path.read_text(encoding="utf-8") for path in files)
    assert "chat.js?v=20260804doneframe1" in sources
    assert "chat.js?v=20260803chatloop1" not in sources
    assert "chat.js?v=20260719errsurface" not in sources
    assert "chat.js?v=20260722ctxheader4" not in sources
    renderer_refs = re.findall(
        r'''(?:from\s+|src=)["']([^"']*chatRenderer\.js[^"']*)["']''',
        sources,
    )
    assert len(renderer_refs) == 10
    assert all("?" not in ref and ref.endswith("chatRenderer.js") for ref in renderer_refs)
    assert "chatRenderer.js?v=20260722ctxheader1" not in sources
    assert "chatRenderer.js?v=20260722emailfastindex1" not in sources

    stateful_targets = {
        Path("static/js/chatRenderer.js").resolve(),
        Path("static/js/sessions.js").resolve(),
        Path("static/js/models.js").resolve(),
        Path("static/js/slashCommands.js").resolve(),
        Path("static/js/settings.js").resolve(),
        # Images are opened through Files and Imps rather than a separate applet.
    }
    identities = {target: set() for target in stateful_targets}
    ref_pattern = re.compile(
        r'''(?:from\s+|import\s*\(\s*|import\s+|src=)["']([^"']+\.js(?:\?[^"']*)?)["']'''
    )
    for path in [Path("static/index.html"), *Path("static").rglob("*.js")]:
        for ref in ref_pattern.findall(path.read_text(encoding="utf-8")):
            ref_path, _, query = ref.partition("?")
            if ref_path.startswith("/static/"):
                target = Path(ref_path.lstrip("/")).resolve()
            elif ref_path.startswith("."):
                target = (path.parent / ref_path).resolve()
            else:
                continue
            if target in identities:
                identities[target].add(query)

    # Cache-busting a separate module such as compare/models.js is fine. Each
    # primary state owner above must itself resolve to one unqueried identity.
    assert all(queries == {""} for queries in identities.values())


def test_agent_only_submit_has_no_orphaned_workspace_intent_gate():
    source = Path("static/js/chat.js").read_text(encoding="utf-8")
    # Submit binds a chat mode whose product default is agent (the mode is
    # user-selectable now, so the contract is the default rather than a
    # hardcoded literal).
    assert "fd.append('mode', chatMode)" in source
    assert "|| 'agent';" in source
    assert "workspaceAgentIntent" not in source


def test_final_generated_image_render_uses_final_background_state():
    source = Path("static/js/chat.js").read_text(encoding="utf-8")
    final_render = source[source.index("const _isBgFinal ="):source.index("} // end if (!_isBgFinal)")]
    assert "_generatedImagesForTurn.length && !_isBgFinal" in final_render
    assert "_generatedImagesForTurn.length && !_isBg)" not in final_render


def test_early_submit_failure_gets_its_own_error_bubble():
    source = Path("static/js/chat.js").read_text(encoding="utf-8")
    # Anchor on the early-failure handler itself so the slice is the catch
    # block that owns this contract, not the first catch in the file.
    anchor = source.index("let errorHolder = holder?.querySelector('.body')")
    catch_body = source[
        source.rindex("    } catch (err) {", 0, anchor):source.index("    } finally {", anchor)
    ]
    assert "document.querySelector('.msg-ai:last-of-type .body')" not in catch_body
    assert "holder?.querySelector('.body')" in catch_body
    assert "addMessage('assistant', '', finalMeta?.model || '')" in catch_body


def test_selected_endpoint_reconciliation_uses_normalized_route_authority():
    """Browser selectors resolve only through the normalized provider store."""
    source = Path("routes/chat_routes.py").read_text(encoding="utf-8")
    marker = "def _reconcile_selected_route_from_request"
    end = source.index("\ndef ", source.index(marker) + len(marker))
    body = source[source.index(marker):end]
    assert "resolve_chat_route(" in body
    assert "provider_model_route_id" in body
    assert "MANAGED_ENGINE_PUBLIC_URL" in body
    assert "build_chat_url" not in body
    assert "build_headers" not in body


def test_chat_reader_stops_at_terminal_sse_frame_without_waiting_for_eof():
    source = Path("static/js/chat.js").read_text(encoding="utf-8")
    body = source[source.index("let _streamSawDone = false;"):]
    assert "streamReadLoop:" in body
    assert "_streamSawDone = true;" in body
    assert "await reader.cancel();" in body
    assert "break streamReadLoop;" in body


def test_stop_submit_bypasses_fresh_send_debounce():
    """The stop button must work even during the send-start debounce window."""
    source = Path("static/app.js").read_text(encoding="utf-8")
    body = source[source.index("function handleSubmit(e) {"):source.index("// Compare mode:", source.index("function handleSubmit(e) {"))]
    assert "const _isImmediateStop" in body
    assert "if (_submitting && !_isImmediateStop) return;" in body
    assert "if (!_isImmediateStop) {" in body


def test_agent_error_sse_parser_keeps_typed_recovery_fields():
    from routes.chat_routes import _agent_error_from_sse

    frame = (
        "event: error\n"
        'data: {"code":"MODEL_CAPABILITY_UNKNOWN","error":"Certify tools.",'
        '"status":409,"actions":["certify_or_decline_tools"],'
        '"details":{"endpoint_id":"ep","model_id":"model"}}\n\n'
    )
    assert _agent_error_from_sse(frame) == {
        "code": "MODEL_CAPABILITY_UNKNOWN",
        "error": "Certify tools.",
        "status": 409,
        "actions": ["certify_or_decline_tools"],
        "details": {"endpoint_id": "ep", "model_id": "model"},
    }
