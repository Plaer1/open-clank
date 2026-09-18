import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def test_unified_provider_ui_contract():
    result = subprocess.run(
        ["node", str(ROOT / "tests/unified_provider_ui_contract.mjs")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "Unified provider UI contract checks passed" in result.stdout


def test_setup_command_never_accepts_provider_credentials():
    source = (ROOT / "static" / "js" / "slashCommands.js").read_text(encoding="utf-8")
    setup_registry = source[source.index("  setup: {") : source.index("  demo: {", source.index("  setup: {"))]
    assert "usage: '/setup'" in setup_registry
    assert "noUserBubble: true" in setup_registry
    assert "subs:" not in setup_registry
    assert "/setup openai" not in setup_registry
    assert "/setup groq" not in setup_registry


def test_active_settings_provider_surface_uses_only_v1_control_plane():
    source = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    assert "import providerControl from './providerControl.js'" in source
    assert "import mimoProviders" not in source
    assert "import modelSharing" not in source
    assert "'/api/model-endpoints'" not in source
    assert "'/api/v1/providers'" in source


def test_provider_ui_uses_single_model_routing_and_direct_model_shares():
    control = (ROOT / "static" / "js" / "providerControl.js").read_text(encoding="utf-8")
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert "'/share-recipients'" in control
    assert "/models/${encodeURIComponent(model.id)}/shares/${encodeURIComponent(username)}" in control
    assert "body: { enabled: wanted }" in control
    assert "body: { routes: [{ model_route_id: nextModelRouteId, enabled: true }] }" in control
    assert "Use next account" not in control
    assert "Accept share" not in control
    assert "/preferred-account" not in control
    assert "Primary, then ordered fallbacks" not in control
    assert "provider-control-new-share" not in html
    assert "provider-control-share-form" not in html
    assert "Choose one model for each purpose." in html


def test_legacy_model_fallback_controls_and_saves_are_absent():
    settings = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    for legacy in (
        "default_model_fallbacks",
        "utility_model_fallbacks",
        "vision_model_fallbacks",
        "set-defaultFallbacks",
        "set-utilityFallbacks",
        "set-visionFallbacks",
    ):
        assert legacy not in settings
        assert legacy not in html
