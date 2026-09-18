"""Skill importer SSRF hardening: redirects must be re-validated per hop.

The importer follows redirects manually (`_get_checked`) and re-runs the SSRF
guard on every hop with ``block_private=True``, matching the hardened web-fetch
path in ``services/search/content.py:_get_public_url``. Previously it used
``httpx``'s ``follow_redirects=True`` with the lenient guard on the *initial*
URL only, so a ``3xx`` to an internal/metadata address was still connected to.

These tests are hermetic: DNS and the HTTP layer are faked, so no real request
is made.
"""
import ipaddress
from types import SimpleNamespace

import httpx
import pytest

from services.memory import skill_importer
from services.memory.skill_importer import (
    SkillImportError,
    _PinnedBackend,
    _PinnedTransport,
    _check_fetch_url,
    _fetch_bytes,
    _get_checked,
    parse_skill_source,
)
# Clearly-public, non-reserved IP literals for the initial (allowed) hop.
PUBLIC_A = "https://1.1.1.1/skill"
PUBLIC_B = "https://8.8.8.8/skill"
GITHUB_A = "https://raw.githubusercontent.com/o/r/" + ("a" * 40) + "/SKILL.md"
# Internal redirect targets that must be refused before connection.
LOOPBACK = "http://127.0.0.1/latest"
METADATA = "http://169.254.169.254/latest/meta-data/"


@pytest.fixture(autouse=True)
def _hermetic_dns(monkeypatch):
    def resolve(host):
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            return ["1.1.1.1"]

    monkeypatch.setattr(skill_importer, "_resolve_host_ips", resolve)


def _install_fake_client(monkeypatch, *, redirect_from, redirect_to):
    """Replace httpx.Client so `redirect_from` 302s to `redirect_to`, and any
    other URL returns 200. No real socket is opened."""

    class _Resp:
        def __init__(self, url, status, location):
            self.url = url
            self.status_code = status
            self.headers = {"location": location} if location else {}
            self.content = b"ok"
            self.text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return {}

    class _Client:
        def __init__(self, *args, **kwargs):
            # Safety invariant: the importer follows redirects by hand and
            # re-runs the SSRF guard per hop, so it MUST disable httpx's own
            # redirect following. Asserting ``follow_redirects is False`` here
            # (not merely accepting the kwarg) makes any regression to
            # ``follow_redirects=True`` fail these tests instead of passing
            # silently — httpx being faked would otherwise hide the change.
            assert kwargs.get("follow_redirects") is False, (
                "skill importer must construct httpx.Client with "
                "follow_redirects=False; got "
                f"{kwargs.get('follow_redirects')!r}"
            )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            if url == redirect_from:
                return _Resp(url, 302, redirect_to)
            return _Resp(url, 200, None)

    monkeypatch.setattr(skill_importer.httpx, "Client", _Client)


# --- Guard unit: block_private=True refuses internal, allows public ----------

@pytest.mark.parametrize("url", [LOOPBACK, METADATA, "http://10.0.0.5/", "http://[::1]/"])
def test_check_fetch_url_blocks_internal(url):
    with pytest.raises(SkillImportError):
        _check_fetch_url(url)


@pytest.mark.parametrize("url", [PUBLIC_A, PUBLIC_B])
def test_check_fetch_url_allows_public(url):
    # Should not raise for a public IP literal.
    _check_fetch_url(url)


def test_check_fetch_url_rejects_mixed_public_private_resolution(monkeypatch):
    monkeypatch.setattr(
        skill_importer,
        "_resolve_host_ips",
        lambda _host: ["1.1.1.1", "127.0.0.1"],
    )
    with pytest.raises(SkillImportError, match="loopback"):
        _check_fetch_url("https://github.com/o/r")


# --- Redirect revalidation: the core regression ------------------------------

@pytest.mark.parametrize("internal", [LOOPBACK, METADATA])
def test_get_checked_blocks_redirect_to_internal(monkeypatch, internal):
    _install_fake_client(monkeypatch, redirect_from=PUBLIC_A, redirect_to=internal)
    with pytest.raises(SkillImportError, match="blocked"):
        _get_checked(PUBLIC_A)


@pytest.mark.parametrize("internal", [LOOPBACK, METADATA])
def test_fetch_bytes_blocks_redirect_to_internal(monkeypatch, internal):
    # Higher-level: the public fetch helpers inherit the per-hop guard.
    _install_fake_client(monkeypatch, redirect_from=GITHUB_A, redirect_to=internal)
    monkeypatch.setattr(
        skill_importer,
        "check_outbound_url",
        lambda url, **kwargs: (
            (False, "blocked internal URL")
            if url in (LOOPBACK, METADATA)
            else (True, "")
        ),
    )
    with pytest.raises(SkillImportError, match="HTTPS|blocked"):
        _fetch_bytes(GITHUB_A)


def test_skills_sh_entry_blocks_redirect_to_metadata(monkeypatch):
    # The skills.sh unwrap path (user-supplied host) must also revalidate hops.
    raw = "https://skills.sh/example"
    _install_fake_client(monkeypatch, redirect_from=raw, redirect_to=METADATA)
    monkeypatch.setattr(
        skill_importer,
        "check_outbound_url",
        lambda url, **kwargs: (
            (False, "blocked internal URL")
            if url in (LOOPBACK, METADATA)
            else (True, "")
        ),
    )
    with pytest.raises(SkillImportError, match="HTTPS|blocked"):
        parse_skill_source(raw)


# --- Positive: a legitimate public->public redirect is still followed --------

def test_get_checked_follows_public_redirect(monkeypatch):
    _install_fake_client(monkeypatch, redirect_from=PUBLIC_A, redirect_to=PUBLIC_B)
    resp = _get_checked(PUBLIC_A)
    assert resp.status_code == 200
    assert str(resp.url) == PUBLIC_B


def test_get_checked_pins_every_redirect_hop(monkeypatch):
    pins = []

    class _MarkerTransport:
        def __init__(self, ip):
            pins.append(ip)

    monkeypatch.setattr(skill_importer, "_PinnedTransport", _MarkerTransport)
    _install_fake_client(monkeypatch, redirect_from=PUBLIC_A, redirect_to=PUBLIC_B)

    assert _get_checked(PUBLIC_A).status_code == 200
    assert pins == ["1.1.1.1", "8.8.8.8"]


def test_pinned_backend_connects_to_validated_ip_not_rebound_host(monkeypatch):
    connects = []

    class _Backend:
        def connect_tcp(self, host, port, timeout, local_address, socket_options):
            connects.append((host, port))
            return object()

    monkeypatch.setattr(skill_importer.httpcore, "SyncBackend", _Backend)
    backend = _PinnedBackend("1.1.1.1")

    backend.connect_tcp("raw.githubusercontent.com", 443)

    assert connects == [("1.1.1.1", 443)]


def test_pinned_transport_keeps_original_host_for_tls_sni_and_streams():
    seen = {}

    class _Pool:
        def handle_request(self, request):
            seen["host"] = request.url.host
            seen["target"] = request.url.target
            return SimpleNamespace(
                status=200,
                headers=[],
                stream=iter([b"safe"]),
                extensions={},
            )

    transport = _PinnedTransport.__new__(_PinnedTransport)
    transport._pool = _Pool()
    response = transport.handle_request(
        httpx.Request(
            "GET",
            "https://raw.githubusercontent.com/o/r/SKILL.md?ref=pinned",
        )
    )

    assert seen == {
        "host": b"raw.githubusercontent.com",
        "target": b"/o/r/SKILL.md?ref=pinned",
    }
    assert response.read() == b"safe"


def test_private_rebinding_answer_is_rejected_before_client_creation(monkeypatch):
    monkeypatch.setattr(
        skill_importer,
        "_resolve_host_ips",
        lambda _host: ["1.1.1.1", "169.254.169.254"],
    )
    monkeypatch.setattr(
        skill_importer.httpx,
        "Client",
        lambda *args, **kwargs: pytest.fail("unsafe peer reached HTTP client"),
    )

    with pytest.raises(SkillImportError, match="link-local"):
        _get_checked("https://github.com/o/r")


@pytest.mark.parametrize(
    "target, message",
    [
        ("https://evil.example/skill", "allowed origin"),
        ("http://raw.githubusercontent.com/o/r/SKILL.md", "HTTPS"),
    ],
)
def test_github_redirect_policy_rejects_before_outsider_connection(
    monkeypatch,
    target,
    message,
):
    calls = []

    class _Resp:
        def __init__(self, url, status, location=None):
            self.url = url
            self.status_code = status
            self.headers = {"location": location} if location else {}
            self.content = b""

    class _Client:
        def __init__(self, *args, **kwargs):
            assert kwargs.get("follow_redirects") is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            calls.append(url)
            if url == GITHUB_A:
                return _Resp(url, 302, target)
            raise AssertionError(f"redirect target was connected: {url}")

    monkeypatch.setattr(skill_importer.httpx, "Client", _Client)
    monkeypatch.setattr(
        skill_importer,
        "check_outbound_url",
        lambda url, **kwargs: (True, ""),
    )
    with pytest.raises(SkillImportError, match=message):
        _get_checked(
            GITHUB_A,
            allowed_hosts=skill_importer._GITHUB_HOSTS,
            require_https=True,
        )
    assert calls == [GITHUB_A]
