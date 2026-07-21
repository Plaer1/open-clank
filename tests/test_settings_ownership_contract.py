import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_settings_has_exactly_thirteen_owned_panels():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    tabs = set(re.findall(r'data-settings-tab="([^"]+)"', html))
    panels = set(re.findall(r'data-settings-panel="([^"]+)"', html))
    # mimo-providers is no longer a tab: provider connections live inside
    # added-models so users see ONE providers menu, not two apps.
    expected = {
        "services", "added-models", "ai", "search",
        "integrations", "email", "reminders", "appearance", "shortcuts",
        "account", "tools", "users", "system",
    }
    assert tabs == panels == expected
    assert 'id="mimo-providers-section"' in html, "provider connections must live in the added-models panel"
    assert "const SETTINGS_OWNERSHIP" in javascript
    for panel in expected:
        token = f"'{panel}':" if "-" in panel else f"  {panel}:"
        assert token in javascript
    assert "control.dataset.settingsScope" in javascript
    assert "new MutationObserver" in javascript


def test_settings_mutations_use_checked_responses_and_mcp_list_is_secret_free():
    settings = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    admin = (ROOT / "static/js/admin.js").read_text(encoding="utf-8")
    mcp_routes = (ROOT / "routes/mcp_routes.py").read_text(encoding="utf-8")
    assert "await fetch(" not in settings
    assert "await fetch(" not in admin
    assert '"env": json.loads(srv.env)' not in mcp_routes
    assert '"env_keys":' in mcp_routes
    assert '"args": json.loads(srv.args)' not in mcp_routes


def test_added_models_use_a_default_on_tools_toggle_without_affecting_visibility():
    admin = (ROOT / "static/js/admin.js").read_text(encoding="utf-8")
    model_controls = admin.split("const capabilityControls", 1)[1].split("function initEndpointForm", 1)[0]

    assert "cap.tools_enabled ?? (cap.tools_declared !== false)" in model_controls
    assert "tools_declared: checkbox.checked" in model_controls
    assert "data-ep-probe-model" not in model_controls
    assert "Tools: yes" not in model_controls
    assert "input[type=checkbox]" not in model_controls
    assert "panel.querySelectorAll('input[data-ep-model-id]')" in model_controls
    assert "row.querySelector('input[data-ep-model-id]')" in model_controls


def test_copal_notes_preferences_live_in_appearance_and_use_the_live_workspace_api():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    settings = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    copal = (ROOT / "static/js/copal.js").read_text(encoding="utf-8")
    app = (ROOT / "static/app.js").read_text(encoding="utf-8")
    appearance = html.split('data-settings-panel="appearance"', 1)[1].split('data-settings-panel="shortcuts"', 1)[0]

    assert appearance.count("data-copal-notes-appearance-card") == 1
    assert appearance.count("data-copal-notes-setting=") == 4
    for setting in ("previewLayout", "lineNumbers", "readableLineWidth", "ribbon"):
        assert f'data-copal-notes-setting="{setting}"' in appearance
    assert "_copalModule.updateNotesSettings" in settings
    assert "_copalModule.getNotesSettings" in settings
    assert "export function updateNotesSettings" in copal
    assert "settingsModule.setCopalModule(copalModule)" in app
