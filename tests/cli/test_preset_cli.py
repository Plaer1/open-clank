from types import SimpleNamespace

import pytest

from tests.helpers.cli_loader import load_script


@pytest.fixture
def cli():
    return load_script("odysseus-preset")


def test_entry_or_fail_rejects_non_object_entries(cli):
    try:
        cli._entry_or_fail({"broken": "raw prompt"}, "broken")
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("expected invalid preset entry to exit")


def test_entry_or_fail_returns_valid_entry(cli):
    assert cli._entry_or_fail({"ok": {"name": "ok"}}, "ok") == {"name": "ok"}


def test_set_replaces_corrupt_existing_entry(cli, monkeypatch):
    saved = {}
    emitted = {}

    monkeypatch.setattr(cli, "_load", lambda: {"broken": "raw prompt"})
    monkeypatch.setattr(cli, "_save", lambda data: saved.update(data))
    monkeypatch.setattr(cli, "emit", lambda payload, _args: emitted.update(payload))

    args = SimpleNamespace(
        name="broken",
        prompt="new prompt",
        prompt_file=None,
        temperature=0.7,
        display_name=None,
    )

    cli.cmd_set(args)

    assert saved["broken"] == {
        "name": "broken",
        "system_prompt": "new prompt",
        "temperature": 0.7,
    }
    assert emitted["ok"] is True


def test_load_rejects_non_object_preset_store(cli, tmp_path, capsys):
    cli._PATH = tmp_path / "presets.json"
    cli._PATH.write_text("[]")

    with pytest.raises(SystemExit):
        cli._load()

    assert "expected an object" in capsys.readouterr().err
