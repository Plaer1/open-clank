import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_settings_has_exactly_thirteen_owned_panels():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    tabs = set(re.findall(r'data-settings-tab="([^"]+)"', html))
    panels = set(re.findall(r'data-settings-panel="([^"]+)"', html))
    # Provider discovery belongs in Add Models. Added Models contains only
    # connections that already belong to the signed-in user.
    expected = {
        "services", "added-models", "ai", "search",
        "integrations", "email", "reminders", "appearance", "shortcuts",
        "account", "tools", "users", "system",
    }
    assert tabs == panels == expected
    services = html.split('data-settings-panel="services"', 1)[1].split(
        'data-settings-panel="added-models"', 1
    )[0]
    added_models = html.split('data-settings-panel="added-models"', 1)[1].split(
        'data-settings-panel="integrations"', 1
    )[0]
    assert 'id="mimo-provider-directory"' in services
    assert 'id="mimo-provider-list"' in services
    assert 'id="mimo-connected-provider-list"' in added_models
    assert 'id="mimo-provider-list"' not in added_models
    assert "const SETTINGS_OWNERSHIP" in javascript
    for panel in expected:
        token = f"'{panel}':" if "-" in panel else f"  {panel}:"
        assert token in javascript
    assert "control.dataset.settingsScope" in javascript
    assert "new MutationObserver" in javascript


def test_model_management_is_account_owned_without_exposing_admin_controls():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    settings = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    admin = (ROOT / "static/js/admin.js").read_text(encoding="utf-8")

    assert "services: { scope: 'per-user'" in settings
    assert "'added-models': { scope: 'per-user'" in settings
    assert "const MODEL_MANAGEMENT_TABS = new Set(['services', 'added-models'])" in settings
    assert "const ADMIN_MODULE_TABS = new Set(['integrations', 'tools', 'users', 'system'])" in settings
    assert "if (MODEL_MANAGEMENT_TABS.has(tab)) {" in settings
    assert "mimoProviders.load();" in settings
    assert "modelSharing.load();" in settings
    assert "if (tab === 'added-models' && window._isAdmin) loadPermissionGrants();" in settings

    assert 'id="mimo-permission-grants-root" class="admin-only"' in html
    assert "export function _initModelData()" in admin
    model_init = admin.split("export function _initModelData()", 1)[1].split("export function open", 1)[0]
    assert "initModelManagement();" in model_init
    assert "loadEndpoints();" in model_init
    assert "initAll();" not in model_init


def test_provider_directory_copy_actions_and_scroll_targets_are_canonical():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    providers = (ROOT / "static/js/mimoProviders.js").read_text(encoding="utf-8")
    admin = (ROOT / "static/js/admin.js").read_text(encoding="utf-8")

    assert 'id="mimo-providers-section"' not in html
    assert "Native MiMo provider authentication" not in providers
    assert "Odysseus Settings" not in providers
    assert "— serves" not in providers
    assert "const apiKey = node('button'" not in providers
    assert "id !== 'mimo' && !id.startsWith('ody-')" in providers
    assert "providerId(provider) === 'xiaomi'" in providers
    assert "return 'Xiaomi';" in providers
    assert "? 'MiMo'" in providers
    assert "Cloud Providers" not in html
    assert ">Cloud<" not in html
    assert "Open Clank agent runtime" not in html
    assert "Open Clank agent runtime" not in admin
    assert admin.count("'[data-settings-tab=\"services\"]'") >= 2
    assert admin.count("'mimo-provider-directory'") >= 2


def test_model_sharing_is_named_user_only_and_includes_native_model_pills():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    settings = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    admin = (ROOT / "static/js/admin.js").read_text(encoding="utf-8")
    sharing = (ROOT / "static/js/modelSharing.js").read_text(encoding="utf-8")

    assert 'id="model-share-received"' in html
    assert "Shared with you" in html
    assert "import modelSharing from './modelSharing.js';" in settings
    assert "data.filter(ep => !ep.selector_only)" in admin
    assert "modelSharing.mountOwnerControls(panel, epId);" in admin
    assert "Shared by ${esc(ep.shared_by || 'another user')}" in admin
    assert "if (!endpoint.shared) modelSharing.mountOwnerControls(panel, epId);" in admin
    assert "Shared with ${recipients.length}" in sharing
    assert "Share with other Odysseus users" not in sharing
    assert "provider.connection_id || `mimo:${providerId(provider)}`" in (
        ROOT / "static/js/mimoProviders.js"
    ).read_text(encoding="utf-8")
    assert "payload?.received" in sharing
    assert "payload?.available" not in sharing
    assert "recipients: uniqueRecipients" in sharing
    assert "'xiaomi/mimo-auto'" in sharing
    for secret_field in ("source_url", "api_key", "headers"):
        assert f"value?.{secret_field}" not in sharing


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
