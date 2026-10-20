"""The chat helper layer never handles provider credentials after cutover."""

import inspect

import routes.chat_helpers as chat_helpers


def test_legacy_provider_auth_and_fallback_helpers_are_absent():
    source = inspect.getsource(chat_helpers)

    assert "def resolve_session_auth(" not in source
    assert "def try_fallback_endpoint(" not in source
    assert "resolve_endpoint_runtime" not in source
    assert "ModelEndpoint" not in source


def test_auto_name_runs_through_the_managed_utility_lane():
    source = inspect.getsource(chat_helpers.auto_name_session)

    assert "complete_text(" in source
    assert 'purpose="utility"' in source
    assert "api_key" not in source
    assert "endpoint_url" not in source
