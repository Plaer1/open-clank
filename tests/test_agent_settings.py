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


def test_unknown_keys_fail_with_path_instead_of_silent_loss():
    for value, path in [({"memory": {}}, "memory"), ({"compaction": {"unknown": 1}}, "compaction.unknown"), ({"checkpoint": {"push_caps": {"unknown": 1}}}, "checkpoint.push_caps.unknown")]:
        with pytest.raises(ValueError, match=path): validate_agent_settings(value)


def test_empty_arrays_zero_caps_and_nonfinite_quantities():
    assert validate_agent_settings({"checkpoint": {"thresholds": [], "push_caps": {"recent_user": 0}}})["checkpoint"]["thresholds"] == []
    for quantity in [float("inf"), float("nan"), True, "101%"]:
        with pytest.raises(ValueError): validate_agent_settings({"compaction": {"max_context": quantity}})


def test_admitted_settings_are_deeply_immutable():
    value = {"checkpoint": {"thresholds": ["20%"], "push_caps": {"notes": 5000}}}
    admitted = admit_agent_settings("alice", value, generation=1, context_window=100000, max_output=1000)
    value["checkpoint"]["push_caps"]["notes"] = 1
    assert admitted.effective["checkpoint"]["push_caps"]["notes"] == 5000
    with pytest.raises(TypeError): admitted.effective["checkpoint"]["push_caps"]["notes"] = 1


def test_empty_structural_merge_preserves_unrelated_and_quantity_empty_resets_value():
    from src.agent_settings import patch_agent_settings
    current = {"compaction": {"preserve_recent_tokens": 12000, "max_context": {"provider/model*": "50%"}}, "checkpoint": {"push_caps": {"notes": 9000000}}}
    merged = patch_agent_settings(current, {"compaction": {}, "checkpoint": {"fork": False}})
    assert merged["compaction"] == current["compaction"]
    assert merged["checkpoint"]["push_caps"]["notes"] == 9000000
    assert patch_agent_settings(merged, {"compaction": {"max_context": {}}})["compaction"]["max_context"] == {}


def test_consumer_backed_lifecycle_fields_and_detection_limits():
    assert validate_agent_settings({"dream": {"auto": False, "interval_days": 0}, "experimental": {"try_best": {"edit_similarity": 0.2}}})["dream"]["interval_days"] == 0
    with pytest.raises(ValueError): validate_agent_settings({"experimental": {"try_best": {"edit_similarity": 2}}})


def test_typed_profile_union_records_and_secret_env_rejection():
    from src.agent_settings import profile_agent_settings, agent_settings_schema
    value = {"formatter": False, "lsp": {"custom": {"command": ["server"], "extensions": [], "env": {"STATE": "{env:XDG_STATE_HOME}"}, "initialization": {"setting": [0, False]}}}, "instructions": [], "command": {"review": {"template": "Review $ARGUMENTS", "subtask": False}}}
    assert profile_agent_settings(value) == value
    assert next(item for item in agent_settings_schema()["fields"] if item["path"] == "lsp")["apply"] == "next_generation"
    for invalid in [{"lsp": {"custom": {"command": ["server"]}}}, {"command": {"review": {"template": "{env:OPENAI_API_KEY}"}}}, {"formatter": {"custom": {"environment": {"KEY": "literal-secret"}}}}]:
        with pytest.raises(ValueError): validate_agent_settings(invalid)


def test_named_profiles_preserve_inheritance_and_reject_competing_authority():
    from src.agent_settings import profile_agent_settings, model_route_references
    settings = {"agent": {"research": {"description": "", "mode": "subagent", "prompt": "", "tool_allowlist": []}, "review": {"model_route_id": "pmr_explicit"}}, "model_groups": {"reviewers": {"default_model_route_id": "pmr_default", "model_route_ids": []}}, "default_agent": "build"}
    parsed = validate_agent_settings(settings)
    assert "model_route_id" not in parsed["agent"]["research"]
    assert profile_agent_settings(parsed) == settings
    assert list(model_route_references(parsed)) == [("agent.review.model_route_id", "pmr_explicit"), ("model_groups.reviewers.default_model_route_id", "pmr_default")]
    for entry in [{"permission": {"*": "allow"}}, {"model": "raw/model"}, {"model_route_id": "a", "model_group": "b"}, {"disable": True}, {"mode": "subagent"}]:
        with pytest.raises(ValueError): validate_agent_settings({"agent": {"build": entry}})
    with pytest.raises(ValueError): validate_agent_settings({"agent": {"helper": {"options": {"api_key": "x"}}}})
    with pytest.raises(ValueError): validate_agent_settings({"command": {"example": {"template": "{file:/operator/private}"}}})


def test_exact_native_reference_templates_do_not_advertise_other_agent_owners():
    from src.agent_settings import agent_settings_schema
    fields = {row["path"]: row for row in agent_settings_schema()["fields"]}
    paths = fields["agent"]["reference_paths"]
    assert "agent.*.model" in paths and "agent.*.tool_allowlist[]" in paths
    assert all(item not in paths for item in ["agent.*.options", "agent.*.permission", "agent.*.tools"])
    for reserved in ["plan", "chat"]:
        with pytest.raises(ValueError): validate_agent_settings({"default_agent": reserved})
    assert validate_agent_settings({"default_agent": "build"}) == {"default_agent": "build"}


def test_legacy_null_quantity_inherits_and_null_patch_deletes_override():
    from src.agent_settings import patch_agent_settings
    assert validate_agent_settings({"compaction": {"max_context": None, "auto": False}}) == {"compaction": {"auto": False}}
    assert patch_agent_settings({"compaction": {"max_context": "300K", "auto": False}}, {"compaction": {"max_context": None}}) == {"compaction": {"auto": False}}
    for section, key in [("formatter", "environment"), ("lsp", "env")]:
        entry = {"command": ["tool"], "extensions": [], key: {"API_KEY": "{env:HOME}"}}
        with pytest.raises(ValueError): validate_agent_settings({section: {"custom": entry}})


def test_nonempty_instruction_sources_are_unavailable_but_reset_is_scoped():
    from src.agent_settings import agent_settings_schema, patch_agent_settings
    field = next(item for item in agent_settings_schema()["fields"] if item["path"] == "instructions")
    assert field["available"] is False and field["supported"] is False and field["resettable"] is True
    for source in ["../private", "/operator/key", "~/private", "workspace/symlink", "https://example.test/policy"]:
        with pytest.raises(ValueError, match="INSTRUCTION_SOURCE_OWNER_REQUIRED"): validate_agent_settings({"instructions": [source]})
    assert patch_agent_settings({"instructions": ["/unsafe/legacy"], "checkpoint": {"fork": False}}, {}, reset=["instructions"]) == {"checkpoint": {"fork": False}}
    assert validate_agent_settings({"instructions": []}) == {"instructions": []}
    for bad in [{"unknown": None}, {"compaction": None}]:
        with pytest.raises(ValueError): validate_agent_settings(bad)
