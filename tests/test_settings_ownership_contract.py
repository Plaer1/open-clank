import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ControlAncestors(HTMLParser):
    """Inspect real control ancestry without browser/layout claims."""
    def __init__(self, html):
        super().__init__()
        self.stack, self.controls = [], {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.controls.setdefault(attrs["id"], []).append([*self.stack, (tag, attrs)])
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def admin_only(self, control):
        paths = self.controls[control]
        assert len(paths) == 1, f"Duplicate control: {control}"
        return any("admin-only" in attrs.get("class", "").split() for _, attrs in paths[0])


def section(source, start, end):
    return source.split(start, 1)[1].split(end, 1)[0]


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
        "logging", "hexes", "help", "advanced",
    }
    assert tabs == panels == expected
    for name in expected:
        assert html.count(f'data-settings-tab="{name}"') == 1
        assert html.count(f'data-settings-panel="{name}"') == 1
    ownership = section(javascript, "const SETTINGS_OWNERSHIP", "});")
    scopes = dict(re.findall(r"^  '?([\w-]+)'?: \{ scope: '([^']+)'", ownership, re.M))
    assert set(scopes) == expected
    for name in ("services", "added-models", "ai", "logging", "hexes", "advanced", "account"):
        assert scopes[name] == "per-user"
    for name in ("system", "users", "tools", "search"):
        assert scopes[name] == "global-admin"
    assert scopes["appearance"] == scopes["shortcuts"] == "browser-local"
    assert scopes["file-access"] == scopes["history"] == "shared-policy"
    assert scopes["help"] == "shared-content"
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


def test_file_access_uses_canonical_locations_people_and_separate_agent_access():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    settings = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    css = (ROOT / "static/style.css").read_text(encoding="utf-8")
    files = (ROOT / "static/js/files.js").read_text(encoding="utf-8")
    wizard = (ROOT / "static/js/fileLocationWizard.js").read_text(encoding="utf-8")
    controller = (ROOT / "static/js/fileLocationController.js").read_text(encoding="utf-8")
    panel = section(html, 'data-settings-panel="file-access"', 'data-settings-panel="tools"')
    headings = [unescape(re.sub(r"<[^>]+>", "", text)).strip()
                for text in re.findall(r"<h2>(.*?)</h2>", panel, re.S)]
    assert headings == ["Your file access", "Locations", "People", "Workspaces & agent access", "Operation approvals"]
    assert "<summary>Reset agent permissions</summary>" in panel
    controls = ControlAncestors(panel)
    for control in ("odysseus-file-root-add", "odysseus-file-visibility-subject",
                    "odysseus-file-visibility-root", "odysseus-file-visibility-add"):
        assert controls.admin_only(control)
    assert not controls.admin_only("odysseus-file-visible-to-me-list")
    assert 'id="odysseus-file-root-path"' not in panel
    assert "Files and Editor use your account’s access." in panel
    assert "Browse / download" in panel and "Modify" in panel
    assert "./fileLocationController.js" in settings
    assert "openFileLocationWizardDialog" in controller and "./fileLocationWizard.js" in controller
    assert "'/api/file-policy/location-presets'" in controller
    assert "'/api/file-policy/locations'" in controller
    for kind in ("directory", "exact_file", "whole_root"):
        assert f"'{kind}'" in wizard
    assert "Agents may use this Location" in wizard and "Browse host folders" in wizard

    canonical = section(settings, "async function loadCanonicalPolicyState()", "function announceFilePolicyChanged")
    assert "'/api/file-policy/state?include_inactive=true'" in canonical
    assert "checkedFetch(" in canonical and "cache: 'no-store'" in canonical
    loader = section(settings, "async function loadFileAccessSettings()", "/* ═══════════════════════════════════════════")
    assert "const state = await loadCanonicalPolicyState();" in loader
    assert "if (state.is_admin)" in loader
    assert "loadId !== _fileAccessLoadId" in loader
    for render in ("renderCurrentAppFilesystemAccess", "renderFileLocations", "renderPeopleAccess",
                   "renderFileWorkspaces", "renderAgentAccess", "renderOperationApprovals"):
        assert render + "(state" in loader
    failure = loader.split("} catch (error)", 1)[1]
    for control in ("odysseus-file-visible-to-me-list", "odysseus-file-roots",
                    "odysseus-file-visibility-assignments", "file-policy-workspaces",
                    "file-policy-agent-access", "odysseus-agent-grants"):
        assert f"'{control}'" in failure
    assert "replaceChildren()" in failure
    assert "/api/odysseus-files" not in canonical + loader  # Fail closed, no retired read fallback.

    human = section(settings, "function renderCurrentAppFilesystemAccess", "function populateVisibilitySelectors")
    assert "state.effective_scope?.host" in human
    assert "scope.app_capabilities" in human
    assert "agent_capabilities" not in human
    assert "No files or folders are currently shared with your account." in human
    people = section(settings, "function renderPeopleAccess", "function renderFileLocations")
    assert "if (!list || !state.is_admin) return;" in people
    assert "binding.binding_class === 'people'" in people
    assert "usersById.get(String(binding.subject_id))" in people
    assert "people/${encodeURIComponent(binding.id)}" in people
    assert "'PATCH'" in people and "'DELETE'" in people
    assert "['expired', 'used'].includes(fileBindingStatus(binding))" in people
    assert "fileModifyCapabilities(binding, !hasWrite)" in people
    agent = section(settings, "function renderAgentAccess", "function renderOperationApprovals")
    assert "binding.binding_class === 'agent'" in agent
    assert "effective?.agent_capabilities" in agent
    assert "String(binding.subject_id) === String(state.subject_id)" in agent
    assert "agent-access/${encodeURIComponent(binding.id)}" in agent
    assert "status === 'expired' || status === 'used'" in agent
    locations = section(settings, "function renderFileLocations", "function renderAgentAccess")
    assert "state.is_admin" in locations
    assert "Files on disk are not deleted." in locations
    assert "uiModule.styledConfirm" in locations
    assert "agent_access: false" in locations
    assert "locations/${encodeURIComponent(location.id)}" in locations
    assert "'DELETE'" in locations

    shortcut = section(files, "async function addHostLocation()", "function handleWindowClosed")
    assert "await controller.openFileLocationWizard" in shortcut
    assert "./fileLocationController.js" in shortcut
    assert "./settings.js" not in shortcut and "/api/odysseus-files/roots" not in shortcut
    assert "openclank:file-policy-changed" in files and "openclank:file-policy-changed" in settings
    # Access is first in DOM order (headings above), not visually reordered
    # away from keyboard order. Long policy names/controls remain wrappable.
    row_styles = section(css, '[data-settings-panel="file-access"] .file-policy-row {', "}")
    assert "flex-wrap: wrap" in row_styles
    title_styles = section(css, '[data-settings-panel="file-access"] .file-policy-title {', "}")
    assert "overflow-wrap: anywhere" in title_styles


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
    # S13: one activation path (activatePanel) serves nav clicks, open(),
    # finder and deep-link — model management loads through it exactly once.
    assert "function activatePanel(" in settings
    assert settings.count("providerControl.load({ view: tab });") == 1
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
    # S13 consolidate: exactly one lazy activation call site (activatePanel).
    assert settings.count("if (tab === 'ai') activateAiSettings();") == 1
    assert "function activatePanel(" in settings
    assert "activatePanel(activeTab);" in settings
    assert "activatePanel(tab);" in settings
    assert "if (!initializedNow) refreshAiModelEndpoints();" in settings


def test_permissions_page_keeps_canonical_lifetimes_and_explicit_scoped_resets():
    settings = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    status = section(settings, "function fileBindingStatus", "function fileBindingDetails")
    assert "binding.expires_unix_ms" in status and "Date.now()" in status
    assert "binding.lifetime === 'once'" in status and "Number(binding.remaining_uses) <= 0" in status
    assert "return 'expired'" in status and "return 'used'" in status
    details = section(settings, "function fileBindingDetails", "function fileLocationCeiling")
    for lifetime in ("always", "workspace", "chat", "once"):
        assert lifetime + ":" in details
    for text in ("Account-wide scope", "Workspace:", "Chat:", "Expires", "remaining", "Workspace archived"):
        assert text in details
    assert "fileBindingStatus(binding)" in details

    approvals = section(settings, "function renderOperationApprovals", "async function resetFileAgentPermissions")
    assert "state.operation_approvals || []" in approvals
    assert "state.is_admin || String(binding.subject_id) === String(state.subject_id)" in approvals
    assert "if (canManage && ['active', 'disabled'].includes(binding.status))" in approvals
    assert "status === 'expired' || status === 'used'" in approvals
    assert "(!workspace || workspace.archived)" in approvals
    assert "disabled: !active && cannotEnable" in approvals
    assert "operation-approvals/${encodeURIComponent(binding.id)}" in approvals
    assert "'PATCH'" in approvals and "'DELETE'" in approvals
    assert "operationExpiryControls(binding, endpoint)" in approvals

    resets = section(settings, "async function resetFileAgentPermissions", "function initFileAccessControls")
    assert "/api/file-policy/resets${preview ? '/preview' : ''}" in resets
    assert resets.index("await request(true)") < resets.index("uiModule.styledConfirm") < resets.index("await request(false)")
    assert "if (!await uiModule.styledConfirm(" in resets and ")) return;" in resets
    assert "body: JSON.stringify({ scope, ...payload })" in resets
    assert "Shared Locations and human access remain unchanged." in resets
    assert "announceFilePolicyChanged" in resets
    assert "people_preserved" in (ROOT / "src/openclank/file_policy.py").read_text(encoding="utf-8")
    controls = section(settings, "function initFileAccessControls", "async function loadFileAccessSettings")
    assert "const payload = context();" in controls and "if (!payload) throw" in controls
    assert "window.sessionModule?.getCurrentSessionId?.()" in controls
    assert "const workspaceId = getWorkspaceId();" in controls
    assert "{ workspace_id: workspaceId }" in controls and "{ chat_id: chatId }" in controls
    assert "'all_agent'" in controls
    assert "localStorage.getItem('currentSessionId')" not in controls
    assert "/api/mimo/permission-grants" not in resets + controls
    assert "canonicalWorkspaceForPath" not in settings
    row = section(settings, "function filePolicyRow", "const FILE_POLICY_CONTROL_ICONS")
    assert "title.textContent = titleText" in row and "meta.textContent = metaText" in row
    assert "innerHTML" not in row + details + approvals + resets


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
    mcp_routes = (ROOT / "routes/mcp/mcp_routes.py").read_text(encoding="utf-8")
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
    expected = {"previewLayout", "templateFolder", "dailyTemplateId", "lineNumbers",
                "readableLineWidth", "ribbon", "completedVisibility",
                "showTutorialFolder", "showCopalInFolderWorkspace"}
    keys = re.findall(r'data-copal-notes-setting="([^"]+)"', appearance)
    assert len(keys) == len(expected) and set(keys) == expected
    assert "key === 'completedVisibility' ? (control.checked ? 'hide' : 'show') : control.checked" in settings
    assert "control.dataset.copalNotesSetting === 'completedVisibility' ? value === 'hide' : value === true" in settings
    assert "_copalModule.updateNotesSettings" in settings
    assert "_copalModule.getNotesSettings" in settings
    assert "export function updateNotesSettings" in copal
    assert "settingsModule.setCopalModule(copalModule)" in app
