"""S13 — shell applet routes, direct navigation, /files refresh, settings panels.

Behavioral checks through FastAPI TestClient (auth disabled) plus source and
node registry checks. No skip-to-green: node cases skip only when node is
absent from PATH (same pattern as the other *_js.py suites).
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None


# ── Behavioral: server shell routes (subprocess-isolated) ───────────────────
# Importing the real app with AUTH_ENABLED=false flips a module-level constant
# for the whole pytest process and would break later auth/suite tests. Run the
# TestClient checks in a child interpreter so the parent stays clean.

_CLIENT_SCRIPT = r"""
import json, os, sys
os.environ["AUTH_ENABLED"] = "false"
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
sys.path.insert(0, {root!r})
import app as app_module
from fastapi.testclient import TestClient
c = TestClient(app_module.app)
results = {{}}
checks = json.loads(sys.argv[1])
for key, path, follow in checks:
    r = c.get(path, follow_redirects=False)
    results[key] = {{
        "status": r.status_code,
        "content_type": r.headers.get("content-type", ""),
        "location": r.headers.get("location", ""),
        "is_html": b"<html" in r.content.lower(),
    }}
print(json.dumps(results))
"""


def _client_results():
    import json
    checks = []
    def add(key, path, follow=False):
        checks.append([key, path, follow])
    add("files", "/files")
    for path in [
        "/editor", "/wiki", "/graph", "/treehouse", "/timeline", "/todo",
        "/calendar", "/notes", "/code", "/bases", "/mind", "/galaxy",
        "/settings", "/settings/history", "/settings/file-access",
    ]:
        add(path, path)
    for path, _ in [
        ("/copal", "/editor"), ("/copal/editor", "/editor"), ("/copal/notes", "/editor"),
        ("/copal/wiki", "/wiki"), ("/copal/treehouse", "/treehouse"),
        ("/copal/todo", "/todo"), ("/copal/calendar", "/calendar"),
    ]:
        add(path, path)
    add("/copal/graph?doc=abc&mode=mind", "/copal/graph?doc=abc&mode=mind")
    add("/copal/mind", "/copal/mind")
    add("/copal/bases?doc=d1", "/copal/bases?doc=d1")
    add("/copal/bases", "/copal/bases")
    add("/no-such-applet", "/no-such-applet")
    add("/api/no-such-route-s13", "/api/no-such-route-s13")

    import subprocess
    proc = subprocess.run(
        [sys.executable, "-c", _CLIENT_SCRIPT.format(root=str(ROOT)), json.dumps(checks)],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120,
        env={**os.environ, "DEBUG": "false", "OPENCLANK_DEBUG": "false"},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


@pytest.fixture(scope="module")
def client_results():
    return _client_results()


def test_files_refresh_serves_shell_html(client_results):
    """The /files refresh failure is the S13 headline bug: GET /files must
    serve the SPA shell, not 404."""
    result = client_results["files"]
    assert result["status"] == 200
    assert "text/html" in result["content_type"]
    assert result["is_html"]


@pytest.mark.parametrize(
    "path",
    [
        "/editor", "/wiki", "/graph", "/treehouse", "/timeline", "/todo",
        "/calendar", "/notes", "/code", "/bases", "/mind", "/galaxy",
        "/settings", "/settings/history", "/settings/file-access",
    ],
)
def test_direct_applet_paths_serve_shell(client_results, path):
    result = client_results[path]
    assert result["status"] == 200
    assert "text/html" in result["content_type"]


@pytest.mark.parametrize(
    "path,location",
    [
        ("/copal", "/editor"),
        ("/copal/editor", "/editor"),
        ("/copal/notes", "/editor"),
        ("/copal/wiki", "/wiki"),
        ("/copal/treehouse", "/treehouse"),
        ("/copal/todo", "/todo"),
        ("/copal/calendar", "/calendar"),
    ],
)
def test_legacy_copal_alias_redirects_to_direct_path(client_results, path, location):
    result = client_results[path]
    assert result["status"] == 302
    assert result["location"] == location


def test_copal_alias_preserves_query_state(client_results):
    result = client_results["/copal/graph?doc=abc&mode=mind"]
    assert result["status"] == 302
    assert result["location"].startswith("/graph?")
    assert "doc=abc" in result["location"]
    assert "mode=mind" in result["location"]


def test_copal_mind_alias_supplies_graph_mode(client_results):
    result = client_results["/copal/mind"]
    assert result["status"] == 302
    assert result["location"] == "/graph?mode=mind"


def test_copal_bases_alias_preserves_open_bases(client_results):
    """`/bases` keeps the Bases leaf intent; the legacy `/copal/bases` alias
    must be symmetric and not drop it."""
    focused = client_results["/copal/bases?doc=d1"]
    assert focused["status"] == 302
    assert focused["location"].startswith("/editor?")
    assert "open=bases" in focused["location"]
    assert "doc=d1" in focused["location"]
    bare = client_results["/copal/bases"]
    assert bare["status"] == 302
    assert bare["location"] == "/editor?open=bases"


def test_unknown_url_is_not_blanket_shell_catchall(client_results):
    result = client_results["/no-such-applet"]
    assert result["status"] == 404
    assert "text/html" not in result["content_type"]


def test_unknown_api_url_returns_json_error_not_html(client_results):
    result = client_results["/api/no-such-route-s13"]
    assert result["status"] in (401, 404)
    assert "text/html" not in result["content_type"]


# ── Behavioral: applet registry (node) ──────────────────────────────────────

def _node_eval(expr):
    js = (
        "import { appletPath, resolveAppletLocation, isShellNavigation } "
        f"from '{(ROOT / 'static' / 'js' / 'appletRoutes.js').as_posix()}';"
        f"console.log(JSON.stringify({expr}));"
    )
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=js, capture_output=True, text=True, cwd=str(ROOT), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    import json
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_applet_path_is_direct_never_copal():
    assert _node_eval("appletPath('editor')") == "/editor"
    assert _node_eval("appletPath('notes')") == "/editor"
    assert _node_eval("appletPath('files')") == "/files"
    assert _node_eval("appletPath('wiki')") == "/editor"
    assert _node_eval("appletPath('graph', { mode: 'mind' })") == "/graph?mode=mind"
    assert _node_eval("appletPath('treehouse')") == "/treehouse"
    assert _node_eval("appletPath('editor', { doc: 'x1' })") == "/editor?doc=x1"


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_resolve_accepts_direct_and_legacy_aliases():
    assert _node_eval(
        "resolveAppletLocation('/copal/editor', '')"
    ) == {
        "target": "editor", "view": "notes", "mode": None, "doc": None,
        "panel": None, "openBases": False, "legacy": True, "canonicalPath": "/editor",
    }
    assert _node_eval(
        "resolveAppletLocation('/files', '')"
    )["canonicalPath"] == "/files"
    gallery = _node_eval("resolveAppletLocation('/gallery', '')")
    assert gallery["target"] == "files"
    assert gallery["canonicalPath"] == "/files"
    wiki = _node_eval("resolveAppletLocation('/wiki', '')")
    assert wiki["target"] == "editor"
    assert wiki["canonicalPath"] == "/editor"
    resolved = _node_eval("resolveAppletLocation('/mind', '')")
    assert resolved["target"] == "editor"
    assert resolved["mode"] == "mind"
    assert resolved["canonicalPath"] == "/graph?mode=mind"
    resolved = _node_eval("resolveAppletLocation('/settings/history', '')")
    assert resolved["target"] == "settings"
    assert resolved["panel"] == "history"
    assert resolved["canonicalPath"] == "/settings/history"


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_resolve_rejects_unknown_and_root():
    assert _node_eval("resolveAppletLocation('/', '')") is None
    assert _node_eval("resolveAppletLocation('/no-such-applet', '')") is None


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_treehouse_share_token_is_stripped_from_new_addresses():
    """appletPath builds fresh destinations; a one-shot share token never
    rides along to an unrelated navigation."""
    path = _node_eval("appletPath('treehouse', { search: 'treehouseShare=tok&doc=d' })")
    assert path == "/treehouse?doc=d"
    assert "treehouseShare" not in _node_eval("appletPath('editor', { search: 'treehouseShare=tok' })")


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_treehouse_share_token_survives_resolve_until_consumed():
    """Consume-then-strip: shell normalization must keep the live token so
    copal/treehouse.js can accept it and only then rewrite the URL. Covers the
    new-address and legacy-alias deep links."""
    new_address = _node_eval("resolveAppletLocation('/treehouse', '?treehouseShare=TOK&doc=d')")
    assert new_address["canonicalPath"].startswith("/treehouse?")
    assert "treehouseShare=TOK" in new_address["canonicalPath"]
    assert "doc=d" in new_address["canonicalPath"]
    legacy_alias = _node_eval("resolveAppletLocation('/copal/treehouse', '?treehouseShare=TOK')")
    assert legacy_alias["legacy"] is True
    assert "treehouseShare=TOK" in legacy_alias["canonicalPath"]


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_treehouse_share_consumer_strips_only_its_token():
    """Consume-then-strip is proven on the real open path, not by source grep.

    Drives the production deep-link sequence (`resolveAppletLocation` ->
    `updateAppletRoute` -> TreeHouse `load()`) for both the direct
    `/treehouse?treehouseShare=…` address and the legacy
    `/copal/treehouse?treehouseShare=…` alias, and asserts the token is
    delivered to the consumer and stripped only after acceptance.
    """
    suite = ROOT / "tests" / "js" / "test_treehouse_share_open_path.mjs"
    proc = subprocess.run(
        ["node", "--test", str(suite)],
        capture_output=True, text=True, cwd=str(ROOT), timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_bases_open_intent_is_symmetric_across_aliases():
    """`/bases` and `/copal/bases` both carry the Bases leaf intent; a refresh
    of the canonical `/editor?open=bases` form must honor it too."""
    assert _node_eval("appletPath('bases')") == "/editor?open=bases"
    assert _node_eval("appletPath('bases', { doc: 'd1' })") == "/editor?doc=d1&open=bases"
    from_bases = _node_eval("resolveAppletLocation('/bases', '')")
    assert from_bases["openBases"] is True
    assert "open=bases" in from_bases["canonicalPath"]
    from_alias = _node_eval("resolveAppletLocation('/copal/bases', '?doc=d1')")
    assert from_alias["openBases"] is True
    assert "open=bases" in from_alias["canonicalPath"]
    assert "doc=d1" in from_alias["canonicalPath"]
    from_canonical = _node_eval("resolveAppletLocation('/editor', '?open=bases&doc=d1')")
    assert from_canonical["openBases"] is True
    assert "open=bases" in from_canonical["canonicalPath"]


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_is_shell_navigation_matches_registry():
    for path in ("/files", "/editor", "/settings/history", "/copal/wiki", "/"):
        assert _node_eval(f"isShellNavigation({path!r})") is True
    assert _node_eval("isShellNavigation('/static/js/app.js')") is False
    assert _node_eval("isShellNavigation('/no-such-applet')") is False


# ── Source contracts ────────────────────────────────────────────────────────

def test_sw_navigation_list_mirrors_applet_registry():
    """The service worker cannot import the ES module; pin the mirror."""
    registry = (ROOT / "static" / "js" / "appletRoutes.js").read_text(encoding="utf-8")
    sw = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
    shell_block = re.search(
        r"export const SHELL_PATHS = Object\.freeze\(\[(.*?)\]\);",
        registry, re.S,
    ).group(1)
    paths = set(re.findall(r"'(/[^']*)'", shell_block))
    sw_block = re.search(r"const SHELL_NAV_PATHS = \[(.*?)\];", sw, re.S).group(1)
    sw_paths = set(re.findall(r"'(/[^']*)'", sw_block))
    assert paths == sw_paths
    prefixes = set(re.findall(r"'(/[^']*)'", re.search(
        r"export const SHELL_PATH_PREFIXES = Object\.freeze\(\[(.*?)\]\);",
        registry, re.S,
    ).group(1)))
    sw_prefixes = set(re.findall(r"'(/[^']*)'", re.search(
        r"const SHELL_NAV_PREFIXES = \[(.*?)\];", sw, re.S,
    ).group(1)))
    assert prefixes == sw_prefixes


def test_app_py_registers_files_and_direct_applets():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    block = re.search(
        r"_SHELL_APPLET_PATHS = \((.*?)\n\)",
        source, re.S,
    ).group(1)
    paths = set(re.findall(r'"(/[^"]*)"', block))
    assert "/files" in paths
    for required in ("/editor", "/graph", "/treehouse", "/timeline", "/todo", "/calendar"):
        assert required in paths
    assert 'async def serve_wiki_alias' in source
    assert 'async def serve_gallery_alias' in source
    assert "/settings" in source
    assert "_COPAL_VIEW_TO_PATH" in source


def test_settings_panels_include_history_and_file_access():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    settings = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    tabs = set(re.findall(r'data-settings-tab="([^"]+)"', html))
    panels = set(re.findall(r'data-settings-panel="([^"]+)"', html))
    assert "history" in tabs and "history" in panels
    assert "file-access" in tabs and "file-access" in panels
    assert tabs == panels
    # Finder + one activation path.
    assert 'id="settings-finder"' in html
    assert "function initSettingsFinder" in settings
    assert "function activatePanel" in settings
    assert "SETTINGS_PANEL_IDS" in settings
    # Theme is retained inside Settings -> Appearance.
    assert 'data-settings-panel="appearance"' in html
    assert "settings-theme-controls" in html


def test_theme_sidebar_entry_removed_but_handle_kept():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    # Not a visible sidebar navigation entry.
    assert 'data-ui-key="tool-theme"' not in html
    # Hidden compatibility handle remains for rail/shortcut wiring.
    assert 'id="tool-theme-btn"' in html
    assert "data-theme-settings-alias" in html
    app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "settingsModule.open('appearance')" in app


def test_interaction_policy_dead_field_is_documented():
    """S12 NIT-1: stored column is historical; runtime agent turns pause."""
    database = (ROOT / "core" / "database.py").read_text(encoding="utf-8")
    scheduler = (ROOT / "src" / "task_scheduler.py").read_text(encoding="utf-8")
    assert "historical/API-facing" in database
    assert "pause_on_interaction" in scheduler
    assert "deliberately not consulted" in scheduler


def test_mcp_oauth_callback_honors_configured_host_port():
    """Native boot: default 7777, configured host/port, trusted explicit
    public base only — never a forwarded origin header."""
    source = (ROOT / "src" / "mcp_oauth.py").read_text(encoding="utf-8")
    assert "OAUTH_REDIRECT_BASE_URL" in source
    assert "APP_PUBLIC_URL" in source
    assert "APP_PORT" in source
    assert "APP_BIND" in source
    assert "_configured_redirect_base" in source
    # Behavioral: reload helper under controlled env.
    import importlib
    import sys
    import src.mcp_oauth as mcp_oauth

    def base(env):
        saved = dict(os.environ)
        for key in ("OAUTH_REDIRECT_BASE_URL", "APP_PUBLIC_URL", "APP_PORT", "APP_BIND"):
            os.environ.pop(key, None)
        os.environ.update(env)
        try:
            return mcp_oauth._configured_redirect_base()
        finally:
            os.environ.clear()
            os.environ.update(saved)

    assert base({}) == "http://localhost:7777"
    assert base({"APP_PORT": "8123"}) == "http://localhost:8123"
    assert base({"APP_BIND": "127.0.0.1", "APP_PORT": "9000"}) == "http://127.0.0.1:9000"
    assert base({"APP_BIND": "0.0.0.0", "APP_PORT": "9000"}) == "http://localhost:9000"
    assert base({"OAUTH_REDIRECT_BASE_URL": "https://clank.example/"}) == "https://clank.example"
    assert base({"APP_PUBLIC_URL": "https://pub.example/base"}) == "https://pub.example/base"


def test_copal_js_addresses_use_registry_not_copal_prefix():
    copal = (ROOT / "static" / "js" / "copal.js").read_text(encoding="utf-8")
    # Browser addresses go through appletPath; API /api/copal/* stays.
    assert "appletPath(" in copal
    assert "resolveAppletLocation" in copal
    # No remaining literal browser history writes of /copal/ addresses.
    assert not re.search(r"""history\.(?:push|replace)State\([^)]*'/copal/""", copal)


def test_stray_copal_emitters_go_through_registry():
    """Registry uniqueness: every browser history write and link generator in
    the shell modules resolves through appletRoutes, never a literal /copal/
    destination. Legacy /copal/* stays redirect-only (server 302 + resolve)."""
    # Match a quoted /copal/ path literal — not './copal/...' module paths
    # and not comments that merely name the alias space.
    literal_copal = re.compile(r"""['"`]/copal/""")
    code_editor = (ROOT / "static" / "js" / "codeEditor.js").read_text(encoding="utf-8")
    assert "appletPath(" in code_editor
    assert "from './appletRoutes.js'" in code_editor
    assert not re.search(r"""history\.(?:push|replace)State\([^)]*['"`]/copal/""", code_editor)
    assert not literal_copal.search(code_editor)
    help_module = (ROOT / "static" / "js" / "contextualHelp.js").read_text(encoding="utf-8")
    assert "appletPath(" in help_module
    assert "from './appletRoutes.js'" in help_module
    assert not literal_copal.search(help_module)
    # Sweep: no shell module emits a /copal/ history write or href.
    for js in (ROOT / "static" / "js").rglob("*.js"):
        source = js.read_text(encoding="utf-8")
        if js.name == "appletRoutes.js":
            continue  # owns alias input handling
        assert not re.search(r"""history\.(?:push|replace)State\([^)]*['"`]/copal/""", source), js
        assert not re.search(r"""href\s*[:=]\s*['"`]/copal/""", source), js


def test_python_url_emitters_use_canonical_not_copal():
    """Tool envelopes and generated links never emit /copal/* destinations."""
    literal_copal = re.compile(r"""['"`]/copal/""")
    manage = (ROOT / "src" / "openclank" / "copal_manage.py").read_text(encoding="utf-8")
    tools = (ROOT / "src" / "openclank" / "copal_tools.py").read_text(encoding="utf-8")
    field_guide = (ROOT / "src" / "openclank" / "treehouse_field_guide.py").read_text(encoding="utf-8")
    projection = (ROOT / "src" / "openclank" / "copal_calendar_projection.py").read_text(encoding="utf-8")
    assert "canonical_open_url(" in manage
    assert "canonical_open_url(" in tools
    assert not literal_copal.search(manage)
    assert not literal_copal.search(tools)
    assert not literal_copal.search(field_guide)
    assert not literal_copal.search(projection)
    urls = (ROOT / "src" / "openclank" / "copal_urls.py").read_text(encoding="utf-8")
    assert "canonical_open_url" in urls
    assert '"/editor?open=bases"' in urls


def test_mcp_pin_records_plan_work_7_slice():
    """Cheap dependency slice of plan work 7: MCP stays under `<2`."""
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^mcp<2\s*$", requirements, re.M)
    # PostgreSQL is an advertised DATABASE_URL option. A native setup must
    # install its dialect rather than failing only when create_engine() runs.
    assert re.search(r"^psycopg2-binary\s*$", requirements, re.M)
