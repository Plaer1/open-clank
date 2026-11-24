"""IPC contract tests for the Python mutation-owner adapter."""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
import tempfile

from src.openclank.history_client import (
    HistoryClient,
    HistoryClientError,
    HistoryServiceSupervisor,
    ScopedHistoryCredential,
)


def _envelope() -> dict:
    return {
        "schema_version": 1,
        "action_id": "python-action",
        "actor_account_id": "account",
        "resource_key": {
            "account_id": "account",
            "workspace_id": "workspace",
            "provider": "files",
            "resource_id": "resource",
        },
        "guard_resource_ids": [],
        "modified_resource_ids": [],
        "operation": "replace",
        "expected_revision": None,
        "actor_id": "actor",
        "actor_kind": "agent",
        "session_id": None,
        "run_id": None,
        "task_id": None,
        "tool_id": None,
        "before_revision": None,
        "expected_after_revision": None,
        "original_locator": None,
        "destination_locator": None,
        "timestamp_millis": None,
        "coverage": None,
        "per_resource_outcomes": None,
    }


def test_prepare_uses_server_bound_digest_when_client_has_no_digest() -> None:
    client = HistoryClient("/unused", actor_id="actor", account_id="account")
    calls = []
    client._call = lambda payload: calls.append(payload) or {"Action": {"action_id": "python-action"}}

    result = client.prepare(_envelope(), content=b"before", fingerprint="fp")

    assert result["Action"]["action_id"] == "python-action"
    assert calls[0]["Prepare"]["envelope"]["claimed_digest"] == ""
    assert "physical_lease_keys" not in calls[0]["Prepare"]["envelope"]["request"]
    assert calls[0]["Prepare"]["content"] == base64.b64encode(b"before").decode("ascii")


def test_client_preserves_optional_digest_as_an_integrity_hint() -> None:
    client = HistoryClient("/unused", actor_id="actor", account_id="account")
    calls = []
    client._call = lambda payload: calls.append(payload) or {"Accepted": None}
    envelope = _envelope()
    envelope["claimed_digest"] = "tested-rust-digest"

    client.prepare(envelope, content=None, fingerprint="empty")

    assert calls[0]["Prepare"]["envelope"]["claimed_digest"] == "tested-rust-digest"


def test_large_content_uses_bounded_chunk_transport_without_file_size_limit() -> None:
    client = HistoryClient("/unused", actor_id="actor", account_id="account")
    calls: list[dict] = []
    client._call = lambda payload: calls.append(payload) or {"Action": {"action_id": "python-action"}}

    result = client.prepare(_envelope(), content=b"x" * 800_000, fingerprint="fp")

    assert result["Action"]["action_id"] == "python-action"
    assert [next(iter(call)) for call in calls] == [
        "StageBegin",
        "StageChunk",
        "StageChunk",
        "StageFinish",
        "PrepareStaged",
    ]
    assert all(len((__import__("json").dumps(call, separators=(",", ":")) + "\n").encode()) < 1024 * 1024 for call in calls)


def test_supervisor_launches_real_history_service_and_reuses_scoped_client_after_restart() -> None:
    # A developer target may be stale or built with a different protocol.  The
    # real service qualification harness supplies the exact isolated binary;
    # without it this test remains a unit-suite no-op rather than launching an
    # arbitrary target/debug artifact.
    configured_binary = os.environ.get("OPENCLANK_HISTORY_TEST_BIN", "").strip()
    if not configured_binary:
        return
    binary = Path(configured_binary)
    if not binary.is_file():
        return

    async def exercise() -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            admin = ScopedHistoryCredential(
                actor_id="app-admin",
                account_id="account-a",
                capabilities=frozenset({"admin", "capture", "read"}),
            )
            supervisor = HistoryServiceSupervisor(
                binary,
                socket_path=root / "history.sock",
                catalog_path=root / "history.redb",
                lore_root=root / "lore",
                credential_file=root / "credentials.json",
                credentials=[admin],
            )
            await supervisor.start()
            client = supervisor.client_for("app-admin", "account-a")
            envelope = _envelope()
            envelope["actor_id"] = "app-admin"
            envelope["actor_account_id"] = "account-a"
            envelope["resource_key"]["account_id"] = "account-a"
            envelope["action_id"] = "supervisor-restart-action"
            assert client.prepare(envelope, content=b"before", fingerprint="before")["Action"]
            await supervisor.stop()
            await supervisor.start()
            retry = supervisor.client_for("app-admin", "account-a")
            assert retry.prepare(envelope, content=b"before", fingerprint="before")["Action"]
            await supervisor.stop()
            assert (root / "credentials.json").stat().st_mode & 0o077 == 0

    asyncio.run(exercise())


def test_supervisor_rotation_revokes_deleted_account_and_preserves_rename_partition() -> None:
    configured_binary = os.environ.get("OPENCLANK_HISTORY_TEST_BIN", "").strip()
    if not configured_binary or not Path(configured_binary).is_file():
        return

    async def exercise() -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            alice = ScopedHistoryCredential(
                actor_id="*", account_id="account-a", capabilities=frozenset({"admin", "capture", "read"})
            )
            bob = ScopedHistoryCredential(
                actor_id="*", account_id="account-b", capabilities=frozenset({"capture", "read"})
            )
            supervisor = HistoryServiceSupervisor(
                configured_binary,
                socket_path=root / "history.sock",
                catalog_path=root / "history.redb",
                lore_root=root / "lore",
                credential_file=root / "credentials.json",
                credentials=[alice, bob],
            )
            await supervisor.start()
            renamed = supervisor.client_for("alice-renamed", "account-a")
            old_bob = supervisor.client_for("bob", "account-b")
            envelope = _envelope()
            envelope["actor_id"] = "alice-renamed"
            envelope["actor_account_id"] = "account-a"
            envelope["resource_key"]["account_id"] = "account-a"
            assert renamed.prepare(envelope, content=b"before", fingerprint="before")["Action"]
            bob_envelope = _envelope()
            bob_envelope["actor_id"] = "bob"
            bob_envelope["actor_account_id"] = "account-b"
            bob_envelope["resource_key"]["account_id"] = "account-b"
            bob_envelope["action_id"] = "bob-action"
            assert old_bob.prepare(bob_envelope, content=b"before", fingerprint="before")["Action"]
            renamed_token = renamed.token
            await supervisor.sync_accounts({"alice-renamed": {"account_id": "account-a", "is_admin": True}})
            assert supervisor.client_for("alice-renamed", "account-a").token == renamed_token
            try:
                old_bob.prepare(bob_envelope, content=b"before", fingerprint="before")
            except HistoryClientError:
                pass
            else:
                raise AssertionError("deleted account credential remained valid after rotation")
            await supervisor.sync_accounts({"recreated": {"account_id": "account-b", "is_admin": False}})
            recreated = supervisor.client_for("recreated", "account-b")
            assert recreated.token != old_bob.token
            assert "restore" in next(
                credential.capabilities
                for credential in supervisor.credentials
                if credential.account_id == "account-b"
            )
            envelope["actor_id"] = "recreated"
            envelope["actor_account_id"] = "account-b"
            envelope["resource_key"]["account_id"] = "account-b"
            envelope["action_id"] = "recreated-action"
            assert recreated.prepare(envelope, content=b"before", fingerprint="before")["Action"]
            await supervisor.stop()

    asyncio.run(exercise())
