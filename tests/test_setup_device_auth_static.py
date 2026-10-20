"""Static regressions for `/setup` account sign-in providers."""

from pathlib import Path


_REPO = Path(__file__).resolve().parent.parent
_SLASH = (_REPO / "static" / "js" / "slashCommands.js").read_text(encoding="utf-8")


def _between(src: str, start: str, end: str) -> str:
    start_idx = src.index(start)
    end_idx = src.index(end, start_idx)
    return src[start_idx:end_idx]


def test_setup_guide_lists_account_sign_in_providers():
    guide_block = _between(_SLASH, "function _showSetupEndpointChoices", "async function _hasConfiguredModels")

    assert 'data-setup-provider="' in _SLASH
    assert "provider.key" in _SLASH
    assert "'copilot'" in _SLASH
    assert "'chatgpt-subscription'" in _SLASH
    assert "/setup copilot" in _SLASH
    assert "/setup chatgpt-subscription" in _SLASH


def test_clicking_account_sign_in_provider_prefills_setup_command_not_api_key():
    click_block = _between(_SLASH, "const providerEl = e.target.closest('.setup-clickable-provider')", "// 3. Check")

    assert "providerEl.dataset.setupProvider" in click_block
    assert "providerEl.dataset.setupKind === 'device-auth'" in click_block
    assert "'/setup ' + providerKey" in click_block


def test_setup_command_opens_the_normalized_provider_control_plane():
    setup_block = _between(_SLASH, "async function _cmdSetup", "// ── Shortcuts")

    assert "settingsModule.open('services')" in setup_block
    assert "Connections, account pools, rotation, model routes, and shares" in setup_block
    assert "_setupProviderDeviceFlow" not in _SLASH
    assert "/api/copilot/device/start" not in _SLASH
    assert "/api/chatgpt-subscription/device/start" not in _SLASH
