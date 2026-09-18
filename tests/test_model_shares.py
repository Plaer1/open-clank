"""Tombstone contract for the retired endpoint-row sharing authority."""

from pathlib import Path

from src.openclank.retired_provider_routes import RETIRED_PROVIDER_ROUTE_PREFIXES


_ROOT = Path(__file__).resolve().parent.parent
_APP = (_ROOT / "app.py").read_text(encoding="utf-8")
_STORE = (_ROOT / "src" / "openclank" / "provider_store.py").read_text(
    encoding="utf-8"
)
_ROUTES = (_ROOT / "routes" / "provider_v1_routes.py").read_text(
    encoding="utf-8"
)
_LEGACY_ROUTES = (_ROOT / "routes" / "model_routes.py").read_text(
    encoding="utf-8"
)


def test_app_does_not_mount_legacy_model_share_routes():
    assert "setup_model_share_routes" not in _APP
    assert "src.model_shares" not in _APP
    assert "/api/model-shares" in RETIRED_PROVIDER_ROUTE_PREFIXES


def test_normalized_store_owns_granular_share_lifecycle():
    for operation in (
        "create_share_grant",
        "replace_share_selectors",
        "set_model_share",
        "resolve_share_scope",
        "assert_share_selection",
    ):
        assert f"def {operation}" in _STORE


def test_normalized_api_owns_share_mutations():
    for path in (
        '"/share-recipients"',
        '"/models/{model_route_id}/shares/{recipient}"',
        '"/shares"',
        '"/shares/received"',
    ):
        assert path in _ROUTES


def test_recipient_acceptance_and_preference_mutations_are_tombstoned():
    assert '"Provider shares are active immediately"' in _ROUTES
    assert '"Shared-account preferences are no longer supported"' in _ROUTES
    assert "store.accept_share_grant(" not in _ROUTES
    assert "store.set_share_preferred_account(" not in _ROUTES


def test_share_api_never_serializes_credentials():
    share_routes = _ROUTES.split('"/shares"', 1)[1]
    assert '"api_key"' not in share_routes
    assert '"headers"' not in share_routes


def test_retired_legacy_share_projection_does_not_guess_family_from_model_id():
    provider_helper = _LEGACY_ROUTES.split("def _share_provider_metadata", 1)[1]
    provider_helper = provider_helper.split("def _share_display_name", 1)[0]
    assert 'share.model_id.split' not in provider_helper
    assert 'provider_family_id' in _LEGACY_ROUTES
    assert 'provider_display_name' in _LEGACY_ROUTES
