"""Owner-scope regression for /api/research/start endpoint resolution.

`research_start()` resolves a CALLER-SUPPLIED `endpoint_id` (and, with nothing
configured, a bare first-enabled fallback) to a `ModelEndpoint` whose *decrypted*
api_key + base_url then drive the research LLM calls
(`start_research(llm_endpoint=, llm_headers=)`). Both lookups must be
exact-owner scoped, with legacy null-owner rows reserved for unauthenticated
single-user mode, so a research-privileged user can't bind a research run
to ANOTHER user's PRIVATE endpoint and silently spend that owner's API key /
reach whatever internal base_url they configured. Mirrors the
webhook `_first_enabled_endpoint` (#1045) and session `_owned_endpoint` fixes.
"""

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

# The helper resolves `from src.database import ModelEndpoint` at call time.
# Stub the module so we can hand it a fake declarative class whose column
# comparisons return inspectable predicates (the real one is a SQLAlchemy
# class, MagicMock'd to oblivion by conftest). owner_filter stays REAL.
_sd = types.ModuleType("src.database")
_sd.ModelEndpoint = MagicMock()
sys.modules.setdefault("src.database", _sd)

from routes.research_routes import _owned_enabled_endpoint, _resolve_endpoint_runtime  # noqa: E402


class _Predicate:
    def __init__(self, check):
        self._check = check

    def __call__(self, row):
        return self._check(row)

    def __or__(self, other):
        return _Predicate(lambda row: self(row) or other(row))


class _Column:
    def __init__(self, name):
        self.name = name

    def __eq__(self, value):
        return _Predicate(lambda row: getattr(row, self.name) == value)


class _ModelEndpoint:
    id = _Column("id")
    is_enabled = _Column("is_enabled")
    owner = _Column("owner")


class _Query:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *predicates):
        self._rows = [r for r in self._rows if all(p(r) for p in predicates)]
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _DB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, model):
        assert model is _ModelEndpoint
        return _Query(self._rows)


def _ep(eid, owner, *, is_enabled=True):
    return SimpleNamespace(id=eid, owner=owner, is_enabled=is_enabled, api_key="sk-secret")


def _resolve(rows, owner, endpoint_id=None):
    sys.modules["src.database"].ModelEndpoint = _ModelEndpoint
    return _owned_enabled_endpoint(_DB(rows), owner, endpoint_id)


# --- explicit endpoint_id (POST /api/research/start, body.endpoint_id) --------

def test_endpoint_id_rejects_another_owners_private_endpoint():
    # bob's private endpoint exists, but alice asking for it by id resolves None
    # → the route raises 404 ("Endpoint not found or disabled"), never builds
    #   headers from bob's key.
    rows = [_ep("ep-bob", "bob"), _ep("ep-alice", "alice")]
    assert _resolve(rows, "alice", "ep-bob") is None


def test_endpoint_id_returns_callers_own_endpoint():
    rows = [_ep("ep-bob", "bob"), _ep("ep-alice", "alice")]
    ep = _resolve(rows, "alice", "ep-alice")
    assert ep is not None and ep.id == "ep-alice"


def test_endpoint_id_rejects_legacy_null_owner_row_for_authenticated_user():
    rows = [_ep("ep-shared", None)]
    assert _resolve(rows, "alice", "ep-shared") is None


def test_endpoint_id_skips_disabled_even_when_owned():
    rows = [_ep("ep-alice", "alice", is_enabled=False)]
    assert _resolve(rows, "alice", "ep-alice") is None


# --- bare first-enabled fallback (no endpoint_id, nothing configured) ---------

def test_fallback_never_picks_another_owners_endpoint():
    # Alice must borrow neither Bob's endpoint nor the legacy ownerless row.
    rows = [_ep("ep-bob", "bob"), _ep("ep-shared", None)]
    assert _resolve(rows, "alice") is None


def test_fallback_returns_none_when_only_others_endpoints():
    rows = [_ep("ep-bob", "bob"), _ep("ep-carol", "carol")]
    assert _resolve(rows, "alice") is None


# --- legacy single-user / unresolved owner: null-owner rows only -------------

def test_null_owner_only_resolves_legacy_null_owner_row():
    rows = [_ep("ep-x", "bob"), _ep("ep-y", None)]
    assert _resolve(rows, None, "ep-x") is None
    ep = _resolve(rows, None, "ep-y")
    assert ep is not None and ep.id == "ep-y"


def test_runtime_resolution_uses_provider_auth_for_chatgpt_subscription(monkeypatch):
    ep = SimpleNamespace(
        id="ep-chatgpt",
        owner="alice",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key=None,
        provider_auth_id="auth-1",
        cached_models='["gpt-5.5"]',
        hidden_models=None,
    )

    monkeypatch.setattr(
        "src.chatgpt_subscription.resolve_runtime_credentials",
        lambda auth_id, owner=None: {
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "fresh-access-token",
        },
    )

    url, model, headers = _resolve_endpoint_runtime(ep, owner="alice", model="")

    assert url == "https://chatgpt.com/backend-api/codex/responses"
    assert model == "gpt-5.5"
    assert headers["Authorization"] == "Bearer fresh-access-token"
