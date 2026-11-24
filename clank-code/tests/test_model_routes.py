"""Tombstone contract for the retired ModelEndpoint route authority."""

from pathlib import Path

from src.openclank.retired_provider_routes import (
    RETIRED_PROVIDER_ROUTE_EXACT,
    RETIRED_PROVIDER_ROUTE_PREFIXES,
)


_ROOT = Path(__file__).resolve().parent.parent
_APP = (_ROOT / "app.py").read_text(encoding="utf-8")
_NORMALIZED = (_ROOT / "routes" / "provider_v1_routes.py").read_text(
    encoding="utf-8"
)


def test_app_does_not_mount_legacy_model_routes():
    assert "setup_model_routes" not in _APP
    assert "routes.model_routes" not in _APP


def test_legacy_provider_mutation_and_probe_families_are_retired():
    assert "/api/model-endpoints" in RETIRED_PROVIDER_ROUTE_PREFIXES
    assert {
        "/api/discover",
        "/api/ping",
        "/api/probe",
        "/api/probe-selected",
    }.issubset(RETIRED_PROVIDER_ROUTE_EXACT)


def test_normalized_router_owns_model_default_and_tool_projections():
    assert '@compatibility.get("/api/models")' in _NORMALIZED
    assert '@compatibility.get("/api/default-chat")' in _NORMALIZED
    assert '@compatibility.get("/api/tools")' in _NORMALIZED
    assert '@compatibility.post("/api/tools")' in _NORMALIZED
    assert "list_chat_routes" in _NORMALIZED


def test_tool_projection_explains_requested_and_effective_state():
    tools = _NORMALIZED.split('@compatibility.get("/api/tools")', 1)[1].split(
        '@compatibility.post("/api/tools")', 1
    )[0]
    for field in ("source", "requested_enabled", "effective_enabled", "availability", "reason"):
        assert f'"{field}"' in tools
    assert '"native_registry"' in tools


def test_tool_projection_marks_managed_metadata_and_stopped_fallback_truthfully():
    provider = (_ROOT / "routes" / "provider_v1_routes.py").read_text(encoding="utf-8")
    for source in (_NORMALIZED, provider):
        projection = source.split('@compatibility.get("/api/tools")', 1)[1].split(
            '@compatibility.post("/api/tools")', 1
        )[0]
        assert '"registered"' in projection
        assert '"availability": "disabled" if not requested else "available" if registered else "unavailable"' in projection
        assert 'native_status = "engine-stopped"' in projection
        assert 'native_status = "ids-only"' in projection
        assert 'native_status = "engine-query-failed"' in projection


def test_normalized_public_catalogue_contains_no_credential_projection():
    catalogue = _NORMALIZED.split('@compatibility.get("/api/models")', 1)[1].split(
        '@compatibility.get("/api/default-chat")', 1
    )[0]
    assert "api_key" not in catalogue
    assert "headers" not in catalogue
