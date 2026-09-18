"""Recovery handles for interrupted account sagas stay discoverable and silent."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routes.auth_routes import setup_auth_routes
from src.openclank.account_lifecycle import AccountOwnerLifecycle


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_args):
        return self

    def order_by(self, *_args):
        return self

    def all(self):
        return list(self.rows)


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False

    def query(self, *_args):
        return _Query(self.rows)

    def close(self):
        self.closed = True


def _route(router, name):
    return next(route.endpoint for route in router.routes if route.name == name)


def test_active_operation_listing_excludes_manifest_receipt_and_subject(tmp_path):
    now = datetime(2026, 8, 31, 12, 0, 0)
    row = SimpleNamespace(
        id="account-delete-1",
        kind="delete",
        source_owner="alice",
        target_owner=None,
        tombstone_owner="deleted:stable-id",
        state="partial",
        revision=9,
        created_at=now,
        updated_at=now,
        manifest={"secret_path": "/private/alice.txt", "memory": "secret"},
        receipt={"error": "contained private data"},
        account_id="stable-subject",
        actor_account_id="admin-subject",
    )
    session = _Session([row])
    lifecycle = object.__new__(AccountOwnerLifecycle)
    lifecycle.operation_path = tmp_path / "operations.json"
    lifecycle._session_factory = lambda: session

    result = lifecycle.list_active_operations()

    assert result == [
        {
            "operation_id": "account-delete-1",
            "kind": "delete",
            "source_owner": "alice",
            "target_owner": None,
            "tombstone_owner": "deleted:stable-id",
            "state": "partial",
            "revision": 9,
            "created_at": "2026-08-31T12:00:00",
            "updated_at": "2026-08-31T12:00:00",
        }
    ]
    assert session.closed is True
    assert "secret" not in str(result)
    assert "subject" not in str(result)


def test_admin_can_discover_interrupted_operation_without_known_id():
    expected = [{"operation_id": "account-rename-1", "state": "partial"}]
    lifecycle = SimpleNamespace(list_active_operations=lambda: expected)
    auth = SimpleNamespace(
        get_username_for_token=lambda _token: "admin",
        is_admin=lambda username: username == "admin",
    )
    endpoint = _route(
        setup_auth_routes(auth, account_lifecycle=lifecycle),
        "list_account_operations",
    )
    request = SimpleNamespace(cookies={"session": "token"})

    assert asyncio.run(endpoint(request)) == {"operations": expected}


def test_non_admin_cannot_list_account_operations():
    lifecycle = SimpleNamespace(list_active_operations=lambda: [])
    auth = SimpleNamespace(
        get_username_for_token=lambda _token: "alice",
        is_admin=lambda _username: False,
    )
    endpoint = _route(
        setup_auth_routes(auth, account_lifecycle=lifecycle),
        "list_account_operations",
    )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(endpoint(SimpleNamespace(cookies={"session": "token"})))

    assert caught.value.status_code == 403


def test_generic_operation_routes_accept_rename_authority():
    operation = {"operation_id": "rename-1", "kind": "rename", "state": "complete"}
    lifecycle = SimpleNamespace(get_operation=lambda operation_id: operation)
    auth = SimpleNamespace(
        get_username_for_token=lambda token: "admin", is_admin=lambda username: True,
    )
    router = setup_auth_routes(auth, account_lifecycle=lifecycle)
    request = SimpleNamespace(cookies={"session": "token"})
    assert asyncio.run(_route(router, "get_account_operation")("rename-1", request)) == operation
    result = asyncio.run(_route(router, "resume_account_operation")("rename-1", request))
    assert result["ok"] is True
