import os
import sys
from pathlib import Path
from unittest import mock
import pytest
from src.runtime_paths import (
    RuntimeResolutionError,
    get_app_root,
    get_default_data_dir,
    resolve_browser,
    resolve_python,
    resolve_runtime_bundle,
)


def test_get_app_root_normal_run():
    """Verify that get_app_root returns the repository root parent of src/ when not frozen."""
    with mock.patch.object(sys, "frozen", False, create=True):
        app_root = get_app_root()
        # Verify it is a valid directory path and matches expected parent structure
        assert os.path.isdir(app_root)
        assert os.path.exists(os.path.join(app_root, "src"))


def test_get_app_root_frozen_with_meipass():
    """Verify that get_app_root returns the sys._MEIPASS directory when frozen by PyInstaller."""
    mock_meipass = os.path.abspath("mock_meipass_dir")
    with mock.patch.object(sys, "frozen", True, create=True), \
         mock.patch.object(sys, "_MEIPASS", mock_meipass, create=True):
        app_root = get_app_root()
        assert app_root == mock_meipass


def test_get_app_root_frozen_without_meipass():
    """Verify that get_app_root falls back to the sys.executable parent directory when frozen but _MEIPASS is absent."""
    mock_exe_path = os.path.join(os.path.abspath("mock_exe_dir"), "Odysseus.exe")
    with mock.patch.object(sys, "frozen", True, create=True), \
         mock.patch.object(sys, "executable", mock_exe_path, create=True):
        # Remove sys._MEIPASS if it exists in the test process environment
        if hasattr(sys, "_MEIPASS"):
            delattr(sys, "_MEIPASS")
        app_root = get_app_root()
        assert app_root == os.path.abspath("mock_exe_dir")


def test_get_default_data_dir_normal():
    """Verify that get_default_data_dir resolves to get_app_root() / 'data' when not frozen."""
    with mock.patch.object(sys, "frozen", False, create=True):
        res = get_default_data_dir()
        assert res == os.path.join(get_app_root(), "data")


def test_get_default_data_dir_frozen():
    """Verify that get_default_data_dir resolves to a persistent user path under ~ when frozen."""
    with mock.patch.object(sys, "frozen", True, create=True):
        res = get_default_data_dir()
        expected = os.path.join(os.path.expanduser("~"), ".open-clank", "data")
        assert res == expected


def test_get_default_data_dir_migrates_legacy_frozen_root(tmp_path):
    legacy = tmp_path / ".odysseus"
    (legacy / "data").mkdir(parents=True)
    (legacy / "data" / "proof.txt").write_text("kept")
    with mock.patch.object(sys, "frozen", True, create=True), \
         mock.patch("src.runtime_paths.os.path.expanduser", return_value=str(tmp_path)):
        result = Path(get_default_data_dir())
    assert result == tmp_path / ".open-clank" / "data"
    assert (result / "proof.txt").read_text() == "kept"
    assert not legacy.exists()


def test_resolve_python_uses_bootstrap_probe(monkeypatch):
    monkeypatch.setenv("OPEN_CLANK_RUNTIME_PYTHON", sys.executable)
    identity = resolve_python(required_modules=("json",))
    assert Path(identity.path).resolve() == Path(sys.executable).resolve()
    assert identity.prefix
    assert identity.version


def test_resolve_python_reports_missing_dependency(monkeypatch, tmp_path):
    missing = tmp_path / "missing-python"
    missing.write_text("#!/bin/sh\nexit 0\n")
    missing.chmod(0o755)
    monkeypatch.setenv("OPEN_CLANK_RUNTIME_PYTHON", str(missing))
    monkeypatch.setattr("src.runtime_paths._python_candidate_paths", lambda _root: [missing])
    with pytest.raises(RuntimeResolutionError) as caught:
        resolve_python(required_modules=("module_that_does_not_exist",))
    assert caught.value.code in {"child_import_failed", "bootstrap_dependency_missing"}
    assert caught.value.diagnostics[0]["path"] == str(missing)


@pytest.mark.skipif(os.name == "nt", reason="shell executable fixture is POSIX-only")
def test_resolve_browser_honors_explicit_override(monkeypatch, tmp_path):
    browser = tmp_path / "browser"
    browser.write_text("#!/bin/sh\nprintf 'test-browser 1.0\\n'\n")
    browser.chmod(0o755)
    monkeypatch.setenv("OPEN_CLANK_BROWSER_EXECUTABLE", str(browser))
    identity = resolve_browser()
    assert identity.path == str(browser.resolve())
    assert identity.version == "test-browser 1.0"


def test_resolve_runtime_bundle_has_admitted_components(tmp_path):
    bundle = resolve_runtime_bundle(Path(__file__).resolve().parents[1])
    assert bundle["python"]["path"]
    assert bundle["lifetools"]["path"].endswith("src/openclank/lifetools_server.py")
    assert bundle["fm_mcp"]["path"].endswith(("fm-mcp", "fm-mcp.exe"))
