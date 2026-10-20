"""Static regressions for the normalized provider-control UI."""

from pathlib import Path


_REPO = Path(__file__).resolve().parent.parent
_INDEX = (_REPO / "static" / "index.html").read_text(encoding="utf-8")
_ADMIN = (_REPO / "static" / "js" / "admin.js").read_text(encoding="utf-8")
_SETTINGS = (_REPO / "static" / "js" / "settings.js").read_text(encoding="utf-8")
_CONTROL = (_REPO / "static" / "js" / "providerControl.js").read_text(encoding="utf-8")


def test_settings_mounts_one_normalized_provider_control_surface():
    for element_id in (
        "provider-control-root",
        "provider-control-summary",
        "provider-control-create",
        "provider-control-oauth",
        "provider-control-connections",
        "provider-control-bindings",
        "provider-control-added-status",
    ):
        assert f'id="{element_id}"' in _INDEX

    assert "import providerControl from './providerControl.js'" in _SETTINGS
    assert "providerControl.init" in _SETTINGS
    assert "const API_ROOT = '/api/v1/providers';" in _CONTROL


def test_active_admin_modules_do_not_call_retired_provider_apis():
    active_source = _ADMIN + _SETTINGS + _CONTROL
    for retired in (
        "/api/model-endpoints",
        "/api/discover",
        "/api/ping",
        "/api/probe",
        "providerDeviceFlow",
        "mimoProviders",
        "modelSharing",
    ):
        assert retired not in active_source


def test_oauth_uses_managed_flow_lifecycle_and_safe_external_link():
    assert "/connections/${encodeURIComponent(connection.id)}/oauth/start" in _CONTROL
    assert "/oauth/flows/${encodeURIComponent(flowId)}" in _CONTROL
    assert "/oauth/flows/${encodeURIComponent(result.flow_id)}/callback" in _CONTROL
    assert "rel: 'noopener noreferrer'" in _CONTROL
    assert "Open provider login" in _CONTROL


def test_api_keys_are_write_only_and_cleared_immediately():
    assert "type: 'password'" in _CONTROL
    assert "body: { label: label.value.trim(), api_key: value }" in _CONTROL
    assert _CONTROL.count("secret.value = ''") >= 3
    assert "Write-only: the browser clears this field immediately after submission." in _CONTROL


def test_normalized_surface_covers_every_routable_purpose():
    for purpose in (
        "chat",
        "utility",
        "research",
        "tasks",
        "vision",
        "images",
        "tts",
        "stt",
        "embeddings",
    ):
        assert f"['{purpose}'," in _CONTROL
