import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_copal_browser_state_is_owner_scoped():
    entrypoint_path = ROOT / "static/js/copal.js"
    sources = [entrypoint_path, *(ROOT / "static/js/copal").glob("*.js")]
    for path in sources:
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"localStorage\.(?:getItem|setItem)\(\s*[`'\"]openclank", source), path
        assert not re.search(r"sizeKey\s*:\s*[`'\"]openclank-(?:copal|treehouse)", source), path

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


def test_account_switch_wipes_global_state_but_preserves_scoped_owner_state():
    for relative in ("static/login.html", "static/js/init.js", "static/js/settings.js"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "includes(':scope:')" in source
        assert "!scopedOwnerState" in source
