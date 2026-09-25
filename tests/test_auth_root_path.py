"""Mounted ASGI path normalization for auth policy (O01).

Auth middleware must evaluate the same application-relative path Starlette
routes on, so a deployment prefix cannot flip a request between exempt and
authenticated. Redirect targets carry the ASGI root path. Prefix matching is
segment-aware: ``/static-extra`` must not ride on ``/static``.
"""

import pytest

from core.middleware import (
    get_application_route_path,
    path_is_route_or_child,
    with_asgi_root_path,
)


@pytest.mark.parametrize(
    ("root_path", "path", "expected"),
    [
        ("", "/api/models", "/api/models"),
        ("/odysseus", "/odysseus/api/models", "/api/models"),
        ("/odysseus/", "/odysseus//api/models", "/api/models"),
        ("/", "//api/models", "/api/models"),
        ("/odysseus", "/odyssey/api/models", "/odyssey/api/models"),
        ("/app", "/application/api/models", "/application/api/models"),
        ("/odysseus", "/odysseus", ""),
    ],
)
def test_application_route_path_matches_starlette_semantics(root_path, path, expected):
    assert get_application_route_path({"root_path": root_path, "path": path}) == expected


@pytest.mark.parametrize(
    ("root_path", "expected"),
    [
        ("", "/login"),
        ("/odysseus", "/odysseus/login"),
        ("/odysseus/", "/odysseus/login"),
        ("/", "/login"),
    ],
)
def test_client_redirect_path_includes_asgi_root_path(root_path, expected):
    assert with_asgi_root_path({"root_path": root_path}, "/login") == expected


def test_route_prefix_matching_is_segment_aware():
    assert path_is_route_or_child("/static", "/static") is True
    assert path_is_route_or_child("/static/app.js", "/static") is True
    assert path_is_route_or_child("/static-extra/app.js", "/static") is False
    assert path_is_route_or_child("/staticfoo", "/static") is False


def test_app_wires_application_relative_path_for_auth(monkeypatch):
    """AuthMiddleware must consume get_application_route_path, not raw url.path."""
    import app as app_module
    import inspect

    src = inspect.getsource(app_module)
    assert "get_application_route_path(request.scope)" in src
    assert "with_asgi_root_path(request.scope" in src
    assert "path_is_route_or_child(path, p)" in src


def test_auth_exempt_prefixes_are_segment_checked():
    """`/static` exemption must not leak to `/static-extra` or `/staticfoo`."""
    from core.middleware import path_is_route_or_child as check

    exempt_prefixes = ["/static"]
    assert any(check("/static/js/app.js", p) for p in exempt_prefixes)
    assert not any(check("/static-secret/keys", p) for p in exempt_prefixes)
    assert not any(check("/staticfoo", p) for p in exempt_prefixes)
