"""Named profiles/literal prompts use existing host control authority."""
import asyncio
from types import SimpleNamespace
import pytest
from src.openclank.acp_bridge import ACPBridge, _build_prompt_parts, _literal_slash

class Client:
    def register_callback(self, *args): pass
    def on_session_update(self, *args): pass
    async def set_session_config_option(self, *args): pytest.fail("host profile selection must not change native/public mode")

def test_named_profile_selection_keeps_canonical_model_and_validates_eligibility(tmp_path, monkeypatch):
    import src.openclank.conversation_archive as archive
    monkeypatch.setattr(archive, "get_conversation_archive", lambda: SimpleNamespace())
    bridge = ACPBridge(Client(), str(tmp_path), owner="alice", session_map_path=tmp_path / "map.json")
    bridge._session_map["chat"] = "ses_ready"
    bridge._session_state["ses_ready"] = {"modes": {"availableModes": [{"id": "research"}, {"id": "plan"}, {"id": "chat"}]}, "desired": {"model": "canonical/model", "mode": "build"}}
    bridge._persist_session_state = lambda *args: None
    state = asyncio.run(bridge.set_config_option("chat", "agent_profile", "research", owner="alice"))
    assert state["desired"] == {"model": "canonical/model", "mode": "build", "agent_profile": "research"}
    with pytest.raises(ValueError, match="eligible"): asyncio.run(bridge.set_config_option("chat", "agent_profile", "hidden", owner="alice"))
    assert state["desired"]["agent_profile"] == "research"
    for reserved in ("plan", "chat"):
        with pytest.raises(ValueError, match="eligible"): asyncio.run(bridge.set_config_option("chat", "agent_profile", reserved, owner="alice"))
    asyncio.run(bridge.set_config_option("chat", "agent_profile", "", owner="alice"))
    assert state["desired"]["model"] == "canonical/model" and state["desired"]["agent_profile"] == ""

def test_literal_prompt_preserves_slash_text_and_structured_files():
    messages = [{"role": "user", "content": [{"type": "text", "text": "/literal /mimo:compact  exact"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}}]}]
    assert _literal_slash(messages) is True
    parts = _build_prompt_parts(messages, turn_id="root_exact")
    assert parts[0]["text"] == "[turn_source_id=root_exact]\n/mimo:compact  exact"
    assert any(part["type"] == "image" for part in parts)
    assert messages[0]["content"][0]["text"] == "/literal /mimo:compact  exact"
    assert not _literal_slash([{"role": "user", "content": "/literally test"}])
