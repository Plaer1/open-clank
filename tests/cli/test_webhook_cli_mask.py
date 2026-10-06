from types import SimpleNamespace

from tests.helpers.cli_loader import load_script
from tests.helpers.db_stubs import make_core_db_stub


def test_mask_token_handles_short_values(monkeypatch):
    make_core_db_stub(monkeypatch, models=["ScheduledTask"])
    cli = load_script("odysseus-webhook")

    assert cli._mask_token("") == ""
    assert cli._mask_token("short") == "***"
    assert cli._mask_token("abcdef1234567890") == "abcdef…7890"
    assert cli._mask_token("short", reveal=True) == "short"


def test_task_webhook_url_uses_the_canonical_task_route_and_escapes_path_values(monkeypatch):
    make_core_db_stub(monkeypatch, models=["ScheduledTask"])
    cli = load_script("odysseus-webhook")

    assert (
        cli._task_webhook_url("https://app.example.com/", "task / one", "token/with?query")
        == "https://app.example.com/api/tasks/task%20%2F%20one/webhook/token%2Fwith%3Fquery"
    )


def test_cmd_url_emits_the_canonical_task_route(monkeypatch):
    make_core_db_stub(monkeypatch, models=["ScheduledTask"])
    cli = load_script("odysseus-webhook")
    task = SimpleNamespace(id="task / one", name="Task", webhook_token="token/with?query")
    session = SimpleNamespace(get=lambda _model, task_id: task, close=lambda: None)
    emitted = []

    monkeypatch.setattr(cli, "SessionLocal", lambda: session)
    monkeypatch.setattr(cli, "emit", lambda payload, _args: emitted.append(payload))

    cli.cmd_url(SimpleNamespace(id=task.id, base="https://app.example.com/"))

    assert emitted[0]["url"] == "https://app.example.com/api/tasks/task%20%2F%20one/webhook/token%2Fwith%3Fquery"
