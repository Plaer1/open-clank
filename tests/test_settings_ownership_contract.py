import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_settings_has_simple_add_and_added_model_views_over_one_provider_api():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    tabs = set(re.findall(r'data-settings-tab="([^"]+)"', html))
    panels = set(re.findall(r'data-settings-panel="([^"]+)"', html))
    # Add and manage remain separate human-facing views, but both project the
    # same normalized owner-scoped provider authority.
    expected = {
        "services", "added-models", "ai", "search",
        "integrations", "email", "reminders", "appearance", "shortcuts",
        "account", "tools", "file-access", "users", "system", "history",
    }
    assert tabs == panels == expected
    services = html.split('data-settings-panel="services"', 1)[1].split(
        'data-settings-panel="added-models"', 1
    )[0]
    assert 'id="provider-control-root"' in services
    assert 'id="provider-control-create"' in services
    assert '<template id="provider-legacy-controls-retired">' in services
    added = html.split('data-settings-panel="added-models"', 1)[1].split(
        '<template id="provider-legacy-added-models-retired">', 1
    )[0]
    assert 'id="provider-control-connections"' in added
    assert 'id="provider-control-bindings"' in added
    assert 'id="provider-control-owned-shares"' not in added
    assert 'id="provider-control-received-shares"' not in added
    assert 'Choose one model for each purpose.' in added
    assert "const SETTINGS_OWNERSHIP" in javascript
    for panel in expected:
        token = f"'{panel}':" if "-" in panel else f"  {panel}:"
        assert token in javascript
    assert "control.dataset.settingsScope" in javascript
    assert "new MutationObserver" in javascript


def test_file_access_uses_canonical_locations_and_people_with_measured_fallback():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    settings = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    css = (ROOT / "static/style.css").read_text(encoding="utf-8")
    files = (ROOT / "static/js/files.js").read_text(encoding="utf-8")
    wizard = (ROOT / "static/js/fileLocationWizard.js").read_text(encoding="utf-8")
    location_controller = (ROOT / "static/js/fileLocationController.js").read_text(encoding="utf-8")

    panel = html.split('data-settings-panel="file-access"', 1)[1].split(
        'data-settings-panel="tools"', 1
    )[0]
    assert "Host locations &amp; agent access" in panel
    assert "Owner/admin accounts follow the OS account’s filesystem access" in panel
    assert "The editor opens at home or an assigned Location by default." in panel
    assert "Agent tools remain separate and may be narrower." in panel
    assert "People access" in panel
    assert "Share a registered Location" in panel
    assert panel.count("<h2>People access</h2>") == 1
    assert panel.count('id="odysseus-file-roots"') == 1
    assert "User-visible roots" not in panel
    assert "Assign root" not in panel
    assert 'id="odysseus-file-visibility-root"' in panel
    assert 'id="odysseus-file-visible-to-me-list"' in panel
    add_location = panel.split("Host locations &amp; agent access", 1)[1].split(
        'id="odysseus-file-roots"', 1
    )[0]
    assert '<div class="settings-col admin-only">' in add_location
    assert add_location.count('id="odysseus-file-root-add"') == 1
    assert 'id="odysseus-file-root-path"' not in add_location
    assert "./fileLocationController.js" in settings
    assert "openFileLocationWizardDialog" in location_controller
    assert "./fileLocationWizard.js" in location_controller
    assert "'/api/file-policy/location-presets'" in location_controller
    for location_kind in ("directory", "exact_file", "whole_root"):
        assert f"'{location_kind}'" in wizard
    assert "Agents may use this Location" in wizard
    assert "Browse host folders" in wizard

    access_loader = settings.split(
        "async function loadCurrentAppFilesystemAccess(policyState = undefined)", 1
    )[1].split("function populateVisibilitySelectors", 1)[0]
    assert "binding?.binding_class === 'people'" in access_loader
    assert "assignment.location_id" in access_loader
    assert access_loader.index("if (canonical)") < access_loader.index("Compatibility fallback")
    assert "'/api/odysseus-files/app-scope'" in access_loader
    assert "if (scope.host)" in access_loader
    assert "Full OS-visible filesystem" in access_loader
    assert "Editor opens at home by default" in access_loader
    assert "agent approvals remain separate" in access_loader
    assert "'/api/odysseus-files/visibility'" in access_loader
    assert "No files or folders have been shared with this account." in access_loader

    visibility_loader = settings.split("async function loadVisibilityAssignments", 1)[1].split(
        "async function loadFilesystemRoots", 1
    )[0]
    assert visibility_loader.index("await loadCurrentAppFilesystemAccess(canonical);") < visibility_loader.index(
        "if (!window._isAdmin)"
    )
    assert "binding?.binding_class === 'people'" in visibility_loader
    assert "usersByAccountId" in visibility_loader
    assert "subject?.username || assignment.subject_id" in visibility_loader
    assert "'/api/file-policy/people'" in visibility_loader
    assert "`/api/file-policy/people/${encodeURIComponent(assignment.id)}`" in visibility_loader
    assert "method: 'PATCH'" in visibility_loader
    assert "method: 'DELETE'" in visibility_loader
    assert "subject_username: subjectUsername" in visibility_loader
    assert "location_id: locationId" in visibility_loader
    assert "_fileVisibilityBackend === 'legacy'" in visibility_loader
    assert "'/api/odysseus-files/visibility'" in visibility_loader
    add_assignment = visibility_loader.split(
        "add?.addEventListener('click'", 1
    )[1]
    assert "await loadFilesystemRoots();" in add_assignment
    assert "await loadVisibilityAssignments" not in add_assignment

    selectors = settings.split("function populateVisibilitySelectors", 1)[1].split(
        "async function loadVisibilityAssignments", 1
    )[0]
    assert "user.account_id" in selectors
    assert "option.dataset.username" in selectors
    assert "option.dataset.accountId" in selectors

    location_loader = settings.split("async function loadFilesystemRoots", 1)[1].split(
        "/* ═══════════════════════════════════════════", 1
    )[0]
    assert "const canonical = await loadCanonicalPolicyState();" in location_loader
    assert "canonical.locations" in location_loader
    assert "canonical.bindings" in location_loader
    assert location_loader.index("if (canonical)") < location_loader.index("Compatibility fallback")
    assert "'/api/file-policy/locations'" in location_controller
    assert "'/api/odysseus-files/roots'" in location_controller
    assert "'/api/file-policy/agent-access'" in location_loader
    assert "`/api/file-policy/agent-access/${encodeURIComponent(activeAgent.id)}`" in location_loader
    assert "Enable agent" in location_loader
    assert "Disable agent" in location_loader
    assert "Allow agent modify" in location_loader
    assert "Agent browse only" in location_loader
    assert "canonical.is_admin" in location_loader
    assert "peopleBindingsByLocation" in location_loader
    assert "String(binding.subject_id) !== String(canonical.subject_id)" in location_loader
    assert "fileCapabilitySummary" in location_loader
    assert "People: nobody" in location_loader
    assert "Remove Location" in location_loader
    assert "Files on disk are not deleted." in location_loader
    assert "`/api/file-policy/locations/${encodeURIComponent(location.id)}`" in location_loader
    assert "method: 'DELETE'" in location_loader
    assert "'/api/odysseus-files/roots'" in location_loader

    assert "Browse / download" in panel
    assert "Modify" in panel
    assert "Allow modify" in visibility_loader
    assert "Browse only" in visibility_loader

    ownership = settings.split("const SETTINGS_OWNERSHIP", 1)[1].split("});", 1)[0]
    assert "'/api/file-policy/state + /api/file-policy/locations + /api/file-policy/people'" in ownership
    assert "/api/odysseus-files/visibility" not in ownership

    files_shortcut = files.split("async function addHostLocation()", 1)[1].split(
        "function handleWindowClosed", 1
    )[0]
    assert "openFileLocationWizard" in files_shortcut
    assert "await controller.openFileLocationWizard" in files_shortcut
    assert "./fileLocationController.js" in files_shortcut
    assert "./settings.js" not in files_shortcut
    assert "/api/odysseus-files/roots" not in files_shortcut
    assert "openclank:file-policy-changed" in settings
    assert "openclank:file-policy-changed" in files

    assert '[data-settings-panel="file-access"]:not(.hidden)' in css
    assert "#odysseus-file-visible-to-me { order: -1; }" in css


def test_settings_sidebar_has_its_own_bounded_scroll_lane():
    css = (ROOT / "static/style.css").read_text(encoding="utf-8")
    layout = css.split("/* ===== Settings Modal Layout ===== */", 1)[1].split(
        "/* ── Entrance Animations ── */", 1
    )[0]
    desktop_sidebar = layout.split("\n.settings-sidebar {", 1)[1].split("}", 1)[0]

    assert "min-height: 0;" in desktop_sidebar
    assert "overflow-y: auto;" in desktop_sidebar
    assert "overscroll-behavior: contain;" in desktop_sidebar
    assert "max-height: calc(85vh - 60px);" in layout

    mobile = layout.split("@media (max-width: 600px)", 1)[1].split(
        "@container settings-modal", 1
    )[0]
    mobile_sidebar = mobile.split(".settings-sidebar {", 1)[1].split("}", 1)[0]
    assert "overflow-x: auto;" in mobile_sidebar
    assert "overflow-y: hidden;" in mobile_sidebar

    snapped = layout.split("@container settings-modal", 1)[1]
    snapped_sidebar = snapped.split(".settings-sidebar {", 1)[1].split("}", 1)[0]
    assert "overflow-x: auto;" in snapped_sidebar
    assert "overflow-y: hidden;" in snapped_sidebar


def test_model_management_is_account_owned_without_exposing_admin_controls():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    settings = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    admin = (ROOT / "static/js/admin.js").read_text(encoding="utf-8")

    assert "services: { scope: 'per-user'" in settings
    assert "'added-models': { scope: 'per-user'" in settings
    assert "api: '/api/v1/providers'" in settings
    assert "const MODEL_MANAGEMENT_TABS = new Set(['services', 'added-models'])" in settings
    assert "const ADMIN_MODULE_TABS = new Set(['integrations', 'tools', 'users', 'system'])" in settings
    assert "if (MODEL_MANAGEMENT_TABS.has(tab)) {" in settings
    assert "providerControl.load({ view: tab });" in settings
    assert "providerControl.load({ view: activeTab });" in settings
    assert "mimoProviders.load();" not in settings
    assert "modelSharing.load();" not in settings

    assert 'id="provider-control-root"' in html
    assert "export function _initModelData()" in admin
    model_init = admin.split("export function _initModelData()", 1)[1].split("export function open", 1)[0]
    assert "open-clank:open-providers" in model_init
    assert "initModelManagement();" not in model_init
    assert "loadEndpoints();" not in model_init
    assert "initAll();" not in model_init


def test_ai_model_controls_are_lazy_and_do_not_duplicate_add_models_reads():
    settings = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    eager_init = settings.split("function initAll()", 1)[1].split(
        "function initAiSettingsOnce()", 1
    )[0]
    lazy_init = settings.split("function initAiSettingsOnce()", 1)[1].split(
        "function activateAiSettings()", 1
    )[0]

    for initializer in (
        "initDefaultChat();",
        "initTeacherModel();",
        "initUtilityModel();",
        "initImageSettings();",
        "initVisionSettings();",
        "initTtsSettings();",
        "initSttSettings();",
        "initResearchSettings();",
    ):
        assert initializer not in eager_init
        assert initializer in lazy_init

    assert "if (_aiSettingsInitialized) await refreshAiModelEndpoints();" in eager_init
    assert settings.count("if (tab === 'ai') activateAiSettings();") == 1
    assert settings.count("if (activeTab === 'ai') activateAiSettings();") == 1
    assert "if (!initializedNow) refreshAiModelEndpoints();" in settings


def test_permissions_page_shows_canonical_lifetimes_and_preserves_legacy_groups():
    settings = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    permission_ui = settings.split("async function loadPermissionGrants()", 1)[1].split(
        "/* ═══════════════════════════════════════════", 1
    )[0]

    assert "const workspaceId = String(grant.workspace_id || '');" in permission_ui
    assert "workspaceNames.get(sample.workspace_id) || 'This workspace'" in permission_ui
    assert "byWorkspace.get(scopeKey).push(grant);" in permission_ui
    assert "document.createElement('details')" in permission_ui
    assert "document.createElement('summary')" in permission_ui
    assert "permission-workspace-group" in permission_ui
    assert "permission-workspace-summary" in permission_ui
    assert "permission-workspace-path" in permission_ui
    assert "permission-workspace-count" in permission_ui
    assert "permission-workspace-grants" in permission_ui
    assert "permission-workspace-grant" in permission_ui
    assert "Imported legacy path approval" in permission_ui
    assert "Account-wide approvals" in permission_ui
    assert "workspaceGrants.forEach((grant) => {" in permission_ui
    assert "/api/mimo/permission-grants/${grant.id}" in permission_ui
    assert "/api/file-policy/state" in settings
    assert "/api/file-policy/resets" in settings
    assert "canonicalWorkspaceForPath" not in settings
    assert "const workspaceId = getWorkspaceId();" in permission_ui
    assert "{ workspace_id: workspaceId }" in permission_ui
    assert "window.sessionModule?.getCurrentSessionId?.()" in permission_ui
    assert "localStorage.getItem('currentSessionId')" not in permission_ui
    assert "scope, canonicalPayload, legacyPayload" in permission_ui
    assert "if (!canonical && legacyPayload)" in permission_ui
    assert "canonical.total_revoked ?? canonical.matched" in permission_ui
    assert "if (canonical)" in permission_ui
    assert "people_preserved" in (ROOT / "src/openclank/file_policy.py").read_text(encoding="utf-8")
    assert "uiModule.styledConfirm" in permission_ui
    assert "window.confirm('Reset" not in permission_ui
    assert "innerHTML" not in permission_ui


def test_provider_directory_uses_the_normalized_control_plane():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    providers = (ROOT / "static/js/providerControl.js").read_text(encoding="utf-8")
    admin = (ROOT / "static/js/admin.js").read_text(encoding="utf-8")

    assert "const API_ROOT = '/api/v1/providers';" in providers
    assert "/api/model-endpoints" not in providers
    assert "/api/mimo/providers" not in providers
    assert "Add account" in providers
    assert "Account rotation pool" in providers
    assert "Use next account" not in providers
    assert "laneLabel(connection.billing_lane)" in providers
    assert "credential" in providers
    assert 'id="provider-control-root"' in html
    assert "loadEndpoints();" not in admin.split("function refreshAll()", 1)[1].split("/* ═", 1)[0]
    for retired_brand in ("MiMo", "Xiaomi", "OpenCode", "Odysseus"):
        assert retired_brand not in providers


def test_provider_sharing_is_granular_safe_and_named_user_only():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    settings = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    sharing = (ROOT / "static/js/providerControl.js").read_text(encoding="utf-8")

    assert 'id="provider-control-owned-shares"' not in html
    assert 'id="provider-control-received-shares"' not in html
    assert "import providerControl from './providerControl.js';" in settings
    assert "'/share-recipients'" in sharing
    assert "/shares/${encodeURIComponent(username)}" in sharing
    assert "Share ${model.display_name || model.model_id} with ${username}" in sharing
    assert "groupedReceivedShares" in sharing
    assert "provider_group_id" in sharing
    assert "preferred-account" not in sharing
    assert "Accept share" not in sharing
    assert "account_slot_id" not in sharing
    assert "source_url" not in sharing
    assert "credential_envelope" not in sharing


def test_settings_mutations_use_checked_responses_and_mcp_list_is_secret_free():
    settings = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    admin = (ROOT / "static/js/admin.js").read_text(encoding="utf-8")
    mcp_routes = (ROOT / "routes/mcp_routes.py").read_text(encoding="utf-8")
    assert "await fetch(" not in settings
    assert "await fetch(" not in admin
    assert '"env": json.loads(srv.env)' not in mcp_routes
    assert '"env_keys":' in mcp_routes
    assert '"args": json.loads(srv.args)' not in mcp_routes


def test_admin_tools_and_reminder_rows_project_truthful_state_without_browser():
    admin = (ROOT / "static/js/admin.js").read_text(encoding="utf-8")
    settings = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    assert "Catalog freshness:" in admin
    assert "Source: ${esc(t.source" in admin
    assert "Requested: ${t.requested_enabled" in admin
    assert "Effective: ${t.effective_enabled" in admin
    assert "Availability: ${esc(t.availability" in admin
    assert "t.availability === 'unavailable'" in admin
    assert "if (!c.disabled && !c.checked)" in admin
    assert "reminder-endpoint-test" in settings
    assert "note_id: 'test-' + Date.now()" in settings
    assert "reminder-endpoint-webhook-integration" in settings
    assert "Advanced payload template (optional)" in settings
    assert "reminder-endpoint-ntfy-integration" in settings
    assert "reminder-endpoint-up" in settings and "reminder-endpoint-down" in settings
    assert "ntfy_integration_id" in settings


def test_provider_models_are_read_only_routes_with_account_eligibility():
    providers = (ROOT / "static/js/providerControl.js").read_text(encoding="utf-8")
    model_controls = providers.split("function renderModels", 1)[1].split(
        "function renderConnection", 1
    )[0]

    assert "model.display_name || model.model_id" in model_controls
    assert "model.operations" not in model_controls
    assert "Account needs attention" in model_controls
    assert "account${eligible === 1 ? '' : 's'} ready" in model_controls
    assert "api_key" not in model_controls
    assert "fetch(" not in model_controls
    assert "loadConnectionEligibility" in model_controls
    assert "section.refreshHealth" in model_controls
    connection_controls = providers.split("function renderConnection", 1)[1].split(
        "function renderConnections", 1
    )[0]
    assert "modelsView.refreshHealth" in connection_controls
    assert "expand.addEventListener('click'" in connection_controls
    assert "Refresh models" in model_controls
    assert "/models/refresh" in model_controls
    assert "revision: connection.revision" in model_controls
    assert "body: {}" in model_controls


def test_copal_notes_preferences_live_in_appearance_and_use_the_live_workspace_api():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    settings = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    copal = (ROOT / "static/js/copal.js").read_text(encoding="utf-8")
    app = (ROOT / "static/app.js").read_text(encoding="utf-8")
    appearance = html.split('data-settings-panel="appearance"', 1)[1].split('data-settings-panel="shortcuts"', 1)[0]

    assert appearance.count("data-copal-notes-appearance-card") == 1
    assert appearance.count("data-copal-notes-setting=") == 7
    for setting in ("previewLayout", "lineNumbers", "readableLineWidth", "ribbon"):
        assert f'data-copal-notes-setting="{setting}"' in appearance
    assert "_copalModule.updateNotesSettings" in settings
    assert "_copalModule.getNotesSettings" in settings
    assert "export function updateNotesSettings" in copal
    assert "settingsModule.setCopalModule(copalModule)" in app
