import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_settings_has_exactly_fourteen_owned_panels():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    tabs = set(re.findall(r'data-settings-tab="([^"]+)"', html))
    panels = set(re.findall(r'data-settings-panel="([^"]+)"', html))
    # mimo-providers is no longer a tab: provider connections live inside
    # added-models so users see ONE providers menu, not two apps.
    expected = {
        "services", "added-models", "ai", "personas", "search",
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
