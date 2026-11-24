"""Preset persistence, compatibility with partial stores and failed writes."""
import json

from src.preset_manager import PresetManager


class _Unserializable:
    """A value that fails JSON serialization mid-write."""


def _write_presets(directory, data):
    (directory / "presets.json").write_text(json.dumps(data), encoding="utf-8")


def test_failed_save_does_not_truncate_existing_file(tmp_path):
    mgr = PresetManager(str(tmp_path))
    assert mgr.save({"custom": {"name": "keep"}}) is True
    before = (tmp_path / "presets.json").read_text(encoding="utf-8")

    # A payload that cannot be serialized must not clobber the good file.
    assert mgr.save({"custom": {"obj": _Unserializable()}}) is False

    after = (tmp_path / "presets.json").read_text(encoding="utf-8")
    assert after == before
    assert json.loads(after) == {"custom": {"name": "keep"}}


def test_save_round_trip(tmp_path):
    mgr = PresetManager(str(tmp_path))
    assert mgr.save({"custom": {"name": "X", "temperature": 0.5}}) is True

    reloaded = PresetManager(str(tmp_path))
    assert reloaded.presets["custom"]["name"] == "X"


def test_missing_builtin_presets_are_filled_in(tmp_path):
    # Partial file: has code_analyze + brainstorm, missing reason + custom.
    _write_presets(tmp_path, {
        "code_analyze": {"name": "Code Analyze", "temperature": 0.2,
                         "max_tokens": 8000, "system_prompt": "analyze"},
        "brainstorm": {"name": "Brainstorm", "temperature": 0.9,
                       "max_tokens": 4096, "system_prompt": "ideate"},
    })
    pm = PresetManager(str(tmp_path))
    for key in PresetManager.DEFAULT_PRESETS:
        assert key in pm.presets, f"built-in preset {key!r} should be present"
    # The fill is persisted so the next load is already complete.
    with open(tmp_path / "presets.json", encoding="utf-8") as f:
        on_disk = json.load(f)
    assert "reason" in on_disk and "custom" in on_disk


def test_fill_does_not_clobber_user_edits(tmp_path):
    # An edited `custom` (enabled, bespoke prompt) plus a missing `reason`.
    edited_custom = {
        "name": "My Persona",
        "character_name": "My Persona",
        "temperature": 0.55,
        "max_tokens": 1234,
        "system_prompt": "You are my bespoke assistant.",
        "inject_prefix": "PRE",
        "inject_suffix": "SUF",
        "enabled": True,
    }
    _write_presets(tmp_path, {
        "code_analyze": {"name": "Code Analyze", "temperature": 0.2,
                         "max_tokens": 8000, "system_prompt": "analyze"},
        "brainstorm": {"name": "Brainstorm", "temperature": 0.9,
                       "max_tokens": 4096, "system_prompt": "ideate"},
        "custom": edited_custom,
        "user_templates": [{"id": "t1", "name": "Tmpl"}],
        # missing: reason
    })
    pm = PresetManager(str(tmp_path))
    # reason was filled...
    assert "reason" in pm.presets
    # ...but the user's edited custom + templates are untouched.
    assert pm.presets["custom"] == edited_custom
    assert pm.presets["user_templates"] == [{"id": "t1", "name": "Tmpl"}]


def test_complete_file_is_not_rewritten_needlessly(tmp_path):
    # A file that already has every built-in must be returned unchanged.
    full = {k: dict(v) for k, v in PresetManager.DEFAULT_PRESETS.items()}
    full["custom"]["enabled"] = True  # a user edit that must survive
    _write_presets(tmp_path, full)
    pm = PresetManager(str(tmp_path))
    assert pm.presets["custom"]["enabled"] is True
    assert set(PresetManager.DEFAULT_PRESETS) <= set(pm.presets)


def test_non_object_preset_store_falls_back_to_defaults(tmp_path):
    (tmp_path / "presets.json").write_text(json.dumps([]))

    manager = PresetManager(str(tmp_path))

    assert manager.presets == PresetManager.DEFAULT_PRESETS
    assert manager.get("custom")["enabled"] is False
