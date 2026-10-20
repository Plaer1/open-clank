from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_startup_shell_keeps_html_and_modules_in_sync():
    worker = (ROOT / "static/sw.js").read_text(encoding="utf-8")
    index = (ROOT / "static/index.html").read_text(encoding="utf-8")

    assert "HTML (navigation): network-first" in worker
    cache_names = re.findall(r"open-clank-v[0-9A-Za-z-]+", worker)
    assert cache_names, "service worker must declare a versioned Open Clank cache"
    assert len(set(cache_names)) == 1, "service worker cache revision must be unambiguous"
    # Keep the contract tied to the current shell revision. The service worker
    # is network-first for HTML/modules, so a stale historical query string is
    # a test failure rather than a reason to roll the frontend back.
    assert "/static/app.js?v=20260816qol2s01" in index
    assert "/static/js/init.js?v=20260728syncshell1" in index
    assert 'name="viewport" content="width=device-width' in index
    assert "./js/permission-mode.js" in (ROOT / "static/app.js").read_text(encoding="utf-8")


def test_authenticated_startup_owns_username_and_sidebar_model_visibility():
    init = (ROOT / "static/js/init.js").read_text(encoding="utf-8")
    app = (ROOT / "static/app.js").read_text(encoding="utf-8")

    assert "userBarName.textContent = liveUser" in init
    assert "'models-section':      '#models-section'" in app
    assert "new Set(['models-section'," in app


def test_merged_frontend_modules_are_syntax_valid():
    sessions = (ROOT / "static/js/sessions.js").read_text(encoding="utf-8")
    chat = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")

    assert sessions.count("const _isFirstLoad =") == 1
    assert chat.count("function _setStoredPlan(") == 1
    assert "_readSessionState('plan'" in chat
    assert "localStorage.setItem(PLAN_STORAGE_KEY" not in chat
