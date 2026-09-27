import pytest
from src.agent_settings import validate_agent_settings, effective_agent_settings, admit_agent_settings

def test_agent_settings_defaults_and_dynamic_effective_values():
    value = effective_agent_settings({}, context_window=100000, max_output=16000)
    assert value["compaction"]["preserve_recent_tokens"] == 8000
    assert value["compaction"]["reserved"] is None
    assert value["checkpoint"]["reserved"] == 13000

def test_agent_settings_validation_rejects_invalid_and_only_lowers_window():
    with pytest.raises(ValueError): validate_agent_settings({"compaction": {"tail_turns": -1}})
    parsed = validate_agent_settings({"compaction": {"max_context": "300K"}, "checkpoint": {"fork": True}})
    assert effective_agent_settings(parsed, context_window=1000000, max_output=50000)["compaction"]["effective_window"] == 300000

def test_agent_settings_owner_payload_is_plain_and_resettable():
    assert validate_agent_settings(None) == {}
    assert validate_agent_settings({"compaction": {"auto": False}})["compaction"]["auto"] is False
    with pytest.raises(ValueError): validate_agent_settings({"checkpoint": {"thresholds": ["0%"]}})
    with pytest.raises(ValueError): validate_agent_settings({"checkpoint": {"thresholds": ["101%"]}})
    with pytest.raises(ValueError): validate_agent_settings({"checkpoint": {"max_writer_failures": 0}})

def test_admitted_turn_keeps_snapshot_until_next_generation():
    old = admit_agent_settings("alice", {"compaction": {"tail_turns": 2}}, generation=1, context_window=100000, max_output=20000)
    new = admit_agent_settings("alice", {"compaction": {"tail_turns": 7}}, generation=2, context_window=100000, max_output=20000)
    assert old.generation == 1 and old.effective["compaction"]["tail_turns"] == 2
    assert new.generation == 2 and new.effective["compaction"]["tail_turns"] == 7
