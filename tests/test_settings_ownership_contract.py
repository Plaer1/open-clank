import re
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


def test_settings_tabs_have_matching_panels():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    tabs = re.findall(r'data-settings-tab="([\w-]+)"', html)
    panels = re.findall(r'data-settings-panel="([\w-]+)"', html)
    assert tabs and len(tabs) == len(set(tabs))
    assert sorted(tabs) == sorted(panels)


def test_file_location_controls_have_expected_admin_visibility():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    panel = section(html, 'data-settings-panel="file-access"', 'data-settings-panel="tools"')
    controls = ControlAncestors(section(panel, "data-settings-markup>", "</script>"))
    for control in ("odysseus-file-root-add", "odysseus-file-visibility-subject",
                    "odysseus-file-visibility-root", "odysseus-file-visibility-add"):
        assert controls.admin_only(control)
    assert not controls.admin_only("odysseus-file-visible-to-me-list")


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


def test_mcp_list_exposes_environment_names_only():
    routes = (ROOT / "routes/mcp/mcp_routes.py").read_text(encoding="utf-8")
    listing = section(routes, "def list_servers(", "async def add_server(")
    assert '"env": json.loads(srv.env)' not in listing
    assert '"env_keys":' in listing
    assert '"args": json.loads(srv.args)' not in listing
