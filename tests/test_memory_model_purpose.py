"""Contract tests for the first-class, Utility-inheriting Memory purpose."""

from pathlib import Path
from types import SimpleNamespace

from routes.provider_v1_routes import _PURPOSES, _PURPOSE_OPERATIONS
from src.openclank.operation_router import PURPOSE_OPERATIONS


ROOT = Path(__file__).resolve().parents[1]


def test_memory_purpose_is_canonical_chat_complete_only():
    assert "memory" in _PURPOSES
    assert _PURPOSE_OPERATIONS["memory"] == {"chat.complete", "vision.describe"}
    assert PURPOSE_OPERATIONS["memory"] == {"chat.complete", "vision.describe"}
    assert "chat.stream" not in _PURPOSE_OPERATIONS["memory"]
    assert "embeddings.create" not in _PURPOSE_OPERATIONS["memory"]


def test_memory_workloads_keep_their_own_audit_purpose():
    routes = (ROOT / "routes/memory/memory_routes.py").read_text(encoding="utf-8")
    extractor = (ROOT / "services/memory/memory_extractor.py").read_text(encoding="utf-8")
    graph = (ROOT / "services/memory/graph_extractor.py").read_text(encoding="utf-8")
    actions = (ROOT / "src/builtin_actions.py").read_text(encoding="utf-8")
    assert 'purpose="memory"' in routes
    assert 'purpose="memory"' in extractor
    assert 'purpose="memory"' in graph
    assert 'purpose="memory"' in actions


def test_settings_projection_renders_memory_separately_from_utility():
    provider_control = (ROOT / "static/js/providerControl.js").read_text(encoding="utf-8")
    settings = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    assert "['utility', 'Utility'" in provider_control
    assert "['memory', 'Memory', ['chat.complete']]" in provider_control
    assert "memory_endpoint_id" in (ROOT / "src/settings.py").read_text(encoding="utf-8")
    assert "async function initMemoryModel()" in settings
    assert "initMemoryModel();" in settings
    assert "'/api/models'" in settings
    assert "'/api/v1/providers/connections'" not in settings
    assert 'id="set-memoryEpSelect"' in html
    assert 'id="set-memoryModelSelect"' in html
    assert html.count("Same as Utility") >= 2


def test_memory_ai_default_reads_current_utility_selection_each_time(monkeypatch):
    from src.openclank.modality_facade import _configured_text_route

    values = {
        "memory_endpoint_id": "",
        "memory_model": "",
        "utility_endpoint_id": "share:grant-one",
        "utility_model": "utility-a",
        "default_endpoint_id": "",
        "default_model": "",
    }
    seen = []

    monkeypatch.setattr(
        "src.settings.get_user_setting",
        lambda key, _owner, default="": values.get(key, default),
    )
    monkeypatch.setattr(
        "src.openclank.provider_store.ProviderStore",
        lambda: object(),
    )

    def resolve(**kwargs):
        seen.append(dict(kwargs))
        return SimpleNamespace(
            model_route_id=f"route-{kwargs['model_id']}",
            provider_grant_id="grant-one",
            operations=("chat.complete",),
        )

    monkeypatch.setattr("src.openclank.chat_routing.resolve_chat_route", resolve)

    first = _configured_text_route(
        owner="alice",
        purpose="memory",
    )
    values["utility_model"] = "utility-b"
    second = _configured_text_route(
        owner="alice",
        purpose="memory",
    )

    assert first.model_route_id == "route-utility-a"
    assert second.model_route_id == "route-utility-b"
    assert [call["endpoint_id"] for call in seen] == [
        "share:grant-one",
        "share:grant-one",
    ]


def test_explicit_memory_ai_default_overrides_utility(monkeypatch):
    from src.openclank.modality_facade import _configured_text_route

    values = {
        "memory_endpoint_id": "share:memory-grant",
        "memory_model": "memory-model",
        "utility_endpoint_id": "share:utility-grant",
        "utility_model": "utility-model",
    }
    monkeypatch.setattr(
        "src.settings.get_user_setting",
        lambda key, _owner, default="": values.get(key, default),
    )
    monkeypatch.setattr(
        "src.openclank.provider_store.ProviderStore",
        lambda: object(),
    )
    monkeypatch.setattr(
        "src.openclank.chat_routing.resolve_chat_route",
        lambda **kwargs: SimpleNamespace(
            model_route_id="memory-route",
            provider_grant_id="memory-grant",
            operations=("chat.complete",),
            selected_model=kwargs["model_id"],
        ),
    )

    selected = _configured_text_route(owner="alice", purpose="memory")

    assert selected.selected_model == "memory-model"
