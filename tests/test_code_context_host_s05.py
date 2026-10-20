"""Actual ordinary-host context seam; no app/lifespan/native/model execution."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def host(monkeypatch, request):
    # Validate startup isolation before importing any application module.
    root = Path(os.environ["OPEN_CLANK_DATA_DIR"]).resolve()
    assert root.name.startswith("openclank-s03-s06-projection.")
    assert root.is_relative_to(Path("/tmp").resolve())
    assert Path(str(request.config.option.basetemp)).resolve() == root / "tests"
    for key in ("ODYSSEUS_DATA_DIR", "OPEN_CLANK_AUTHORITY_DB_PATH", "OPEN_CLANK_AUTHORITY_AUTH_PATH",
                "FM_DB_PATH", "OPENCLANK_CONVERSATION_ARCHIVE_DB"):
        assert Path(os.environ[key]).resolve().is_relative_to(root), key
    assert os.environ["DATABASE_URL"] == "sqlite:///:memory:"
    assert os.environ["ODYSSEUS_INPROCESS_POLLERS"] == "0"
    assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "OPEN_CLANK_AGENT_HOME" not in os.environ and "THESIUS_AGENT_HOME" not in os.environ

    from routes import chat_helpers as ch
    from src.chat_processor import ChatProcessor
    from src.openclank import code_context as cc, file_policy
    from src import general_hex_runtime, general_hex_composition, user_time
    from tests.test_code_context_retained_s05 import setup_context, retained_manual

    args, calls, read, _, _ = setup_context(monkeypatch)
    captured = dict(hook=[], compact=[], trim=[], preface=[], hex=[])
    prefs = args["prefs"]
    session = args["session"]
    session.endpoint_url, session.model, session.headers = "http://unused.invalid", "fixture-model", {}
    session.history = []
    session.messages = [dict(role="system", content="DROP_COMPACT"), dict(role="assistant", content="DROP_TRIM")]
    session.get_context_messages = lambda: copy.deepcopy(session.messages)
    provider, auth, repository = object(), object(), object()
    processor = ChatProcessor(None, None, memory_provider=provider)
    monkeypatch.setattr(processor, "_trust_prefs", lambda owner: dict(prefs))
    monkeypatch.setattr(file_policy, "FilePolicyRepository", lambda: repository)
    monkeypatch.setattr(ch, "load_prefs_for_user", lambda owner: dict(prefs))
    monkeypatch.setattr(ch, "extract_preset", lambda *a, **kw: ch.PresetInfo(.7, 1024, None, None))
    monkeypatch.setattr(ch, "session_persona_override", lambda sid, preset: preset)
    monkeypatch.setattr(ch, "fire_message_event", lambda *a, **kw: None)
    monkeypatch.setattr(ch, "build_uploaded_file_manifest", lambda *a: [])
    monkeypatch.setattr(user_time, "current_datetime_context_message", lambda: dict(role="user", content="CLOCK"))
    monkeypatch.setattr(ch, "_INCOGNITO_CONTEXTS", {})

    async def preprocess(*a, **kw):
        return ch.PreprocessedMessage("Explain function target", "Explain function target",
            "Explain function target", [], [])
    def add_message(sess, handler, processed, **kw):
        sess.messages.append(dict(role="user", content=processed.user_content))
    async def preface(**kw):
        captured["preface"].append(kw)
        return [dict(role="system", content="PREFACE")], [], []
    async def compact(sess, endpoint, model, messages, headers, **kw):
        captured["compact"].append(copy.deepcopy(messages))
        return [m for m in messages if m["content"] != "DROP_COMPACT"], 32000, True
    def trim(messages, context_length):
        captured["trim"].append(copy.deepcopy(messages))
        return [m for m in messages if m["content"] != "DROP_TRIM"]
    def hex_snapshot(*a, **kw):
        captured["hex"].append(kw)
        return dict(snapshot_hash="fixture-hex")
    real_append = cc.append_code_context
    async def append(**kw):
        captured["hook"].append({**kw, "messages": copy.deepcopy(kw["messages"])})
        return await real_append(**kw)
    monkeypatch.setattr(ch, "preprocess", preprocess)
    monkeypatch.setattr(ch, "add_user_message", add_message)
    monkeypatch.setattr(processor, "build_context_preface", preface)
    monkeypatch.setattr(ch, "maybe_compact", compact)
    monkeypatch.setattr(ch, "trim_for_context", trim)
    monkeypatch.setattr(general_hex_runtime, "turn_hex_snapshot", hex_snapshot)
    monkeypatch.setattr(general_hex_composition, "defaults_instruction", lambda snapshot: "GENERAL_HEX")
    monkeypatch.setattr(cc, "append_code_context", append)
    request_obj = SimpleNamespace(state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth, memory_provider=provider)))
    async def build(**kw):
        return await ch.build_chat_context(sess=session, request=request_obj, chat_handler=SimpleNamespace(),
            chat_processor=processor, message="Explain function target", session_id="chat-a",
            root_operation_id="operation-a", **kw)
    return SimpleNamespace(build=build, session=session, prefs=prefs, calls=calls, captured=captured,
        cc=cc, read=read, retained_manual=retained_manual, provider=provider, auth=auth, repository=repository)


async def test_actual_host_hook_sees_final_compacted_trimmed_hex_frame(host):
    ctx = await host.build()
    assert len(host.captured["hook"]) == 1
    hook = host.captured["hook"][0]
    assert hook["provider"] is host.provider and hook["repository"] is host.repository
    assert hook["auth_manager"] is host.auth and hook["owner"] == "alice"
    assert hook["context_length"] == 32000
    assert [m["content"] for m in hook["messages"]] == ["GENERAL_HEX", "PREFACE", "CLOCK", "Explain function target"]
    assert "DROP_COMPACT" in str(host.captured["compact"]) and "DROP_COMPACT" not in str(host.captured["trim"])
    assert "DROP_TRIM" in str(host.captured["trim"])
    assert ctx.was_compacted and ctx.context_trimmed
    assert ctx.messages[-1]["content"] == "Explain function target"
    assert host.cc.SENTINEL in ctx.messages[-2]["content"]
    assert len(json.dumps(ctx.messages[-2], ensure_ascii=False, separators=(",", ":")).encode()) <= 2048
    assert host.cc.SENTINEL not in str(host.session.messages)
    assert host.session.history == []
    assert [c["query"]["operation"] for c in host.calls] == ["search", "read"]


@pytest.mark.parametrize("gate", ["persistent_engine", "default_off", "no_memory", "incognito", "research", "auxiliary"])
async def test_host_negative_gates_do_not_query_code(host, gate):
    kwargs = {}
    if gate == "persistent_engine":
        kwargs["has_persistent_engine"] = True
    elif gate == "default_off":
        host.prefs.pop("code_context_enabled")
    elif gate in {"no_memory", "incognito"}:
        kwargs[gate] = True
    elif gate == "research":
        host.session.history = [dict(role="system", content="report", metadata=dict(research_spinoff_from="source-chat"))]
    else:
        kwargs["allow_tool_preprocessing"] = False
    ctx = await host.build(**kwargs)
    assert host.calls == []
    assert host.cc.SENTINEL not in str(ctx.messages)
    assert host.cc.SENTINEL not in str(host.session.messages)
    if gate == "persistent_engine":
        assert host.captured["hook"] == []
    if gate in {"no_memory", "incognito", "research", "auxiliary"}:
        assert host.captured["preface"][0]["use_memory"] is False


async def test_actual_host_retained_manual_dedup_and_context_reset(host):
    host.session.messages.append(host.retained_manual(host.read, "sdk"))
    first = await host.build()
    assert host.cc.SENTINEL not in str(first.messages)
    host.session.messages = []
    second = await host.build()
    assert sum(host.cc.SENTINEL in str(m["content"]) for m in second.messages) == 1
    assert host.cc.SENTINEL not in str(host.session.messages)


@pytest.mark.parametrize("part", [{"type": "image_url", "image_url": {"url": "https://not-fetched.invalid/image"}}, {"type": []}, {"type": "tool-result", "output": {"type": {}}}])
async def test_actual_host_unknown_media_cost_skips_request_local_card(host, part):
    host.session.messages.append(dict(role="user", content=[part]))
    ctx = await host.build()
    assert len(host.captured["hook"]) == 1
    assert host.calls == []
    assert host.cc.SENTINEL not in str(ctx.messages)
