import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_copal_browser_state_is_owner_scoped():
    entrypoint_path = ROOT / "static/js/copal.js"
    sources = [entrypoint_path, *(ROOT / "static/js/copal").glob("*.js")]
    for path in sources:
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"localStorage\.(?:getItem|setItem)\(\s*[`'\"]odysseus", source), path
        assert not re.search(r"sizeKey\s*:\s*[`'\"]odysseus-(?:copal|treehouse)", source), path

    entrypoint = entrypoint_path.read_text(encoding="utf-8")
    configured = entrypoint.index("configureCopalStorage(status.storage_namespace)")
    initialized = entrypoint.index("buildWorkspace(); bindSidebar(); connectEvents();")
    assert configured < initialized


def test_copal_storage_namespace_matrix():
    subprocess.run(
        ["node", str(ROOT / "tests/copal_storage_scope.mjs")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_account_switch_wipes_global_state_but_preserves_scoped_copal_state():
    for relative in ("static/login.html", "static/js/init.js", "static/js/settings.js"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "key.includes(':scope:')" in source or "k.includes(':scope:')" in source
        assert "!scopedCopalState" in source


def test_copal_status_bootstrap_retries_only_transient_failures():
    source = (ROOT / "static/js/copal.js").read_text(encoding="utf-8")
    assert "attempt < 3" in source
    assert "!error.status || error.status >= 500" in source
    assert source.index("status = await api('/status')") < source.index(
        "configureCopalStorage(status.storage_namespace)"
    )


def test_notes_explorer_hides_dot_folders_by_default_but_search_keeps_them_reachable():
    workspace = (ROOT / "static/js/copal/notesWorkspace.js").read_text(encoding="utf-8")
    feature = (ROOT / "static/js/copal/notesFeature.js").read_text(encoding="utf-8")

    assert "showDotFolders:raw?.showDotFolders === true" in workspace
    assert "if (workspace?.left?.showDotFolders) return docs" in feature
    assert "parts.some((part) => part.startsWith('.'))" in feature
    assert "const allDocs = documents()" in feature
