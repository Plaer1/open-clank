import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "tests" / "provider_add_models_browser_acceptance.mjs"


def _chrome() -> str | None:
    configured = os.environ.get("OPENCLANK_CHROME_BIN") or os.environ.get("CHROME_BIN")
    candidates = (
        configured,
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    )
    return next((str(candidate) for candidate in candidates if candidate and Path(candidate).is_file()), None)


def test_normalized_add_models_real_browser_flow():
    chrome = _chrome()
    if not chrome:
        pytest.skip("Google Chrome or Chromium is required for the real-browser acceptance test")
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the real-browser acceptance test")

    environment = os.environ.copy()
    environment["OPENCLANK_CHROME_BIN"] = chrome
    result = subprocess.run(
        [node, str(SCRIPT)],
        cwd=REPOSITORY,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    timing_keys = {
        "providerRequestCount",
        "firstUsablePaintMs",
        "corePaintMs",
        "highCardinalityRefreshMs",
    }
    assert {key: value for key, value in report.items() if key not in timing_keys} == {
        "addedModelsVisible": True,
        "progressiveCoreLoad": True,
        "forceReplacementAbort": True,
        "singleInvalidationReload": True,
        "apiAdd": True,
        "localAdd": True,
        "directModelShare": True,
        "highCardinalityRefresh": True,
        "legacyRequests": 0,
        "eligibilityRequests": 3,
        "addOpenCoreReads": {"connections": 0, "models": 0},
        "lazyAiCatalogReads": 1,
    }
    assert report["providerRequestCount"] > 0
    assert 0 <= report["firstUsablePaintMs"] <= 500
    assert 0 <= report["corePaintMs"] <= 1000
    assert 0 <= report["highCardinalityRefreshMs"] <= 1000
