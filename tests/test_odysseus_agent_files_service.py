"""Agent-lane registry scope and Rust-client boundary contracts."""

import asyncio
import base64
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from src.openclank.filesystem_registry import FilesystemRootRegistry
from src.openclank.files_service_client import (
    FilesServiceError,
    OdysseusFilesClient,
    agent_client_for,
    client_for_owner,
    close_all_clients,
    set_history_binding_provider,
)


def _service_binary() -> str:
    return str(
        Path(__file__).resolve().parents[1]
        / "packages/odysseus-files/target/debug/odysseus-files-service"
    )


def test_active_workspace_scope_narrows_to_the_most_specific_root(tmp_path):
    outer = tmp_path / "repo"
    inner = outer / "packages" / "app"
    inner.mkdir(parents=True)
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    broad = registry.add("owner", str(outer), "recursive_directory", ["read", "write"])
    narrow = registry.add("owner", str(inner), "recursive_directory", ["read"])

    scope = registry.agent_scope("owner", str(inner))

    assert scope["approved_root_ids"] == [narrow["id"]]
    assert scope["active_folder"]["root_id"] == narrow["id"]
    assert scope["active_folder"]["canonical_path"] == str(inner.resolve())
    assert scope["active_folder"]["capabilities"] == ["read"]
    assert broad["id"] not in scope["approved_root_ids"]


def test_workspace_outside_registry_fails_closed(tmp_path):
    approved = tmp_path / "approved"
    approved.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    registry.add("owner", str(approved), "recursive_directory", ["read"])

    scope = registry.agent_scope("owner", str(outside))

    assert scope == {"approved_root_ids": [], "root_capabilities": {}, "active_folder": None}


def test_rust_scope_snapshot_is_wire_compatible(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    record = registry.add("owner", str(root), "recursive_directory", ["read", "write"])
    snapshot = json.loads(registry.path.read_text())

    assert snapshot["version"] == 1
    rust_record = snapshot["roots"][record["id"]]
    assert rust_record["kind"] == "recursive_directory"
    assert rust_record["capabilities"] == ["read", "write"]
    assert rust_record["platform_identity"]["device"] is not None


def test_app_scope_is_explicitly_sent_to_rust_service(tmp_path, monkeypatch):
    registry_path = tmp_path / "roots.json"
    registry_path.write_text(json.dumps({"version": 1, "roots": {}}), encoding="utf-8")
    monkeypatch.setenv("ODYSSEUS_FILES_REGISTRY", str(registry_path))
    client = OdysseusFilesClient(
        "non-admin",
        app_scope={
            "host": False,
            "visible_root_ids": ["root-assigned"],
            "capabilities": ["read"],
            "root_capabilities": {"root-assigned": ["read"]},
            "generation": 9,
            "active_folder": None,
        },
    )
    environment = client._service_environment()
    scope = json.loads(environment["ODYSSEUS_FILES_APP_SCOPE"])
    assert environment["ODYSSEUS_FILES_REGISTRY"] == str(registry_path)
    assert scope == {
        "active_folder": None,
        "capabilities": ["read"],
        "generation": 9,
        "host": False,
        "root_capabilities": {"root-assigned": ["read"]},
        "visible_root_ids": ["root-assigned"],
    }


def test_files_child_receives_supervisor_scoped_history_binding(tmp_path, monkeypatch):
    registry_path = tmp_path / "roots.json"
    registry_path.write_text(json.dumps({"version": 1, "roots": {}}), encoding="utf-8")
    monkeypatch.setenv("ODYSSEUS_FILES_REGISTRY", str(registry_path))
    set_history_binding_provider(
        lambda owner, lane: {
            "socket": "/private/history.sock",
            "account_id": "immutable-account-a",
            "actor_id": f"{lane}:{owner}",
            "token": "supervisor-issued-token",
            "workspace_id": "workspace-a",
            "workspace_root": str(tmp_path),
        }
    )
    try:
        environment = OdysseusFilesClient("alice")._service_environment()
    finally:
        set_history_binding_provider(None)
    assert environment["OPENCLANK_HISTORY_ACCOUNT_ID"] == "immutable-account-a"
    assert environment["OPENCLANK_HISTORY_ACTOR_ID"] == "human:alice"
    assert environment["OPENCLANK_HISTORY_TOKEN"] == "supervisor-issued-token"
    assert environment["OPENCLANK_HISTORY_WORKSPACE_ROOT"] == str(tmp_path)


def test_non_admin_app_scope_reads_only_assigned_root_with_real_service(tmp_path, monkeypatch):
    visible = tmp_path / "visible"
    outside = tmp_path / "outside"
    visible.mkdir()
    outside.mkdir()
    (visible / "ok.txt").write_text("ok", encoding="utf-8")
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    root = registry.add("administrator", str(visible), "recursive_directory", ["read"])
    registry.assign_visibility("administrator", "bob", root["id"], ["read"])
    monkeypatch.setenv("ODYSSEUS_FILES_REGISTRY", str(registry.path))
    client = OdysseusFilesClient(
        "bob",
        binary=_service_binary(),
        app_scope=registry.app_scope("bob", is_admin=False),
    )
    try:
        allowed = asyncio.run(client.request("read_lines", str(visible / "ok.txt")))
        assert allowed["data"]["text"] == "ok"
        opened = asyncio.run(client.open_handle(str(visible / "ok.txt")))

        async def collect_handle():
            return b"".join([
                chunk
                async for chunk in client.stream_read_handle(
                    opened["handle"],
                    length=2,
                )
            ])

        assert asyncio.run(collect_handle()) == b"ok"
        try:
            asyncio.run(client.request("read_lines", str(outside / "secret.txt")))
        except Exception as error:
            assert getattr(error, "code", None) in {"denied", "outside_root"}
        else:
            raise AssertionError("unassigned file unexpectedly readable")
    finally:
        client.close()


def test_non_admin_app_scope_does_not_bleed_capabilities_between_roots(tmp_path, monkeypatch):
    read_only = tmp_path / "read-only"
    write_only = tmp_path / "write-only"
    read_only.mkdir()
    write_only.mkdir()
    readable = read_only / "readable.txt"
    readable.write_text("read me", encoding="utf-8")
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    # The physical roots support both operations; the user-visible assignments
    # deliberately narrow them differently. This reproduces the old aggregate-
    # union bug where write on the second root widened the first root too.
    read_root = registry.add(
        "administrator", str(read_only), "recursive_directory", ["read", "write"]
    )
    write_root = registry.add(
        "administrator", str(write_only), "recursive_directory", ["read", "write"]
    )
    registry.assign_visibility("administrator", "bob", read_root["id"], ["read"])
    registry.assign_visibility("administrator", "bob", write_root["id"], ["write"])
    monkeypatch.setenv("ODYSSEUS_FILES_REGISTRY", str(registry.path))
    client = OdysseusFilesClient(
        "bob",
        binary=_service_binary(),
        app_scope=registry.app_scope("bob", is_admin=False),
    )
    try:
        assert asyncio.run(client.request("read_lines", str(readable)))["data"]["text"] == "read me"

        forbidden = read_only / "must-not-exist.txt"
        try:
            asyncio.run(client.request("create", str(forbidden), {"text": "forbidden"}))
        except FilesServiceError as error:
            assert error.code == "denied"
        else:
            raise AssertionError("write capability from another root widened a read-only assignment")
        assert not forbidden.exists()

        allowed = write_only / "allowed.txt"
        asyncio.run(client.request("create", str(allowed), {"text": "allowed"}))
        assert allowed.read_text(encoding="utf-8") == "allowed"

        try:
            asyncio.run(client.request("read_lines", str(allowed)))
        except FilesServiceError as error:
            assert error.code == "denied"
        else:
            raise AssertionError("read capability from another root widened a write-only assignment")
    finally:
        client.close()


def test_scoped_app_client_rejects_registry_generation_change(tmp_path, monkeypatch):
    visible = tmp_path / "visible"
    visible.mkdir()
    target = visible / "ok.txt"
    target.write_text("ok", encoding="utf-8")
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    root = registry.add("administrator", str(visible), "recursive_directory", ["read"])
    registry.assign_visibility("administrator", "bob", root["id"], ["read"])
    monkeypatch.setenv("ODYSSEUS_FILES_REGISTRY", str(registry.path))
    client = OdysseusFilesClient(
        "bob",
        binary=_service_binary(),
        app_scope=registry.app_scope("bob", is_admin=False),
    )
    try:
        assert asyncio.run(client.request("read_lines", str(target)))["data"]["text"] == "ok"
        registry.update("administrator", root["id"], enabled=False)
        try:
            asyncio.run(client.request("read_lines", str(target)))
        except FilesServiceError as error:
            assert error.code == "policy_generation_changed"
        else:
            raise AssertionError("stale scoped client unexpectedly remained usable")
    finally:
        client.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="first production Tonic lane is macOS-only")
def test_tonic_private_uds_has_unary_parity_streaming_and_session_rejection(tmp_path):
    grpc = pytest.importorskip("grpc")
    from src.openclank import files_transport_pb2 as wire
    from src.openclank import files_transport_pb2_grpc as wire_grpc

    target = tmp_path / "stream.bin"
    expected = bytes(range(251)) * 10_000
    target.write_bytes(expected)
    framed = OdysseusFilesClient("owner", binary=_service_binary(), transport="framed")
    tonic = OdysseusFilesClient("owner", binary=_service_binary(), transport="grpc")

    async def exercise():
        framed_stat = await framed.request("stat", str(target), {"include_fingerprint": False})
        tonic_stat = await tonic.request("stat", str(target), {"include_fingerprint": False})
        assert tonic_stat["data"] == framed_stat["data"]
        assert tonic.stable_object_handles is True

        chunks = [
            chunk
            async for chunk in tonic.stream_read(
                str(target),
                length=len(expected),
                chunk_bytes=256 * 1024,
            )
        ]
        assert b"".join(chunks) == expected
        assert all(len(chunk) <= 256 * 1024 for chunk in chunks)

        opened = await tonic.open_handle(str(target))
        handle = opened["handle"]
        assert handle["token"]
        assert opened["size"] == len(expected)
        handle_chunks = [
            chunk
            async for chunk in tonic.stream_read_handle(
                handle,
                offset=17,
                length=700_000,
                chunk_bytes=64 * 1024,
            )
        ]
        assert b"".join(handle_chunks) == expected[17:700_017]

        png_target = tmp_path / "thumbnail.png"
        png_target.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        ))
        png_handle = await tonic.open_handle(str(png_target))
        thumbnail = await tonic.thumbnail_handle(
            png_handle["handle"],
            width=96,
            height=96,
            scale=1.0,
        )
        assert thumbnail.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(thumbnail) <= 4 * 1024 * 1024

        watched = tonic.watch_path(str(tmp_path))
        first_event = asyncio.create_task(anext(watched))
        await asyncio.sleep(0.1)
        (tmp_path / "watch-created.txt").write_text("changed", encoding="utf-8")
        event = await asyncio.wait_for(first_event, timeout=5.0)
        assert event["kind"] in {"created", "modified", "rescan_required"}
        assert set(event) == {
            "sequence", "kind", "rescan_required", "observed_unix_ms",
        }
        assert not any("path" in key for key in event)
        await watched.aclose()
        assert (await tonic.request("health", "."))["ready"] is True

        socket_path = tonic._grpc_socket_path
        assert socket_path is not None
        assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(socket_path.lstat().st_mode) == 0o600
        if shutil.which("lsof") and tonic.process is not None:
            listeners = await asyncio.to_thread(
                subprocess.run,
                [
                    "lsof",
                    "-nP",
                    "-a",
                    "-p",
                    str(tonic.process.pid),
                    "-iTCP",
                    "-sTCP:LISTEN",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert not listeners.stdout.strip()

        forged_channel = grpc.aio.insecure_channel(
            f"unix:{socket_path}",
            options=(("grpc.default_authority", "openclank.local"),),
        )
        try:
            forged = wire_grpc.FilesTransportStub(forged_channel)
            with pytest.raises(grpc.aio.AioRpcError) as rejected:
                await forged.Health(
                    wire.HealthRequest(),
                    metadata=(("x-open-clank-session", "0" * 64),),
                    timeout=2.0,
                )
            assert rejected.value.code() == grpc.StatusCode.UNAUTHENTICATED
        finally:
            await forged_channel.close()

        cancelled_stream = tonic.stream_read(
            str(target),
            length=len(expected),
            chunk_bytes=64 * 1024,
        )
        assert await anext(cancelled_stream)
        await cancelled_stream.aclose()
        # Cancellation must release the data-plane call without poisoning
        # independent unary control traffic.
        assert (await tonic.request("health", "."))["ready"] is True

        replace_target = tmp_path / "replace.bin"
        replace_target.write_bytes(b"old object")
        replaced = await tonic.open_handle(str(replace_target))
        old_path = tmp_path / "old-object.bin"
        replace_target.rename(old_path)
        replace_target.write_bytes(b"new object")
        stale_stream = tonic.stream_read_handle(
            replaced["handle"],
            length=len(b"old object"),
            chunk_bytes=64 * 1024,
        )
        with pytest.raises(FilesServiceError) as stale:
            await anext(stale_stream)
        assert stale.value.code == "invalid_path"
        await stale_stream.aclose()

    try:
        asyncio.run(exercise())
    finally:
        framed.close()
        tonic.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="first production Tonic lane is macOS-only")
def test_tonic_stream_cancels_on_non_admin_policy_generation_change(tmp_path, monkeypatch):
    pytest.importorskip("grpc")
    visible = tmp_path / "visible"
    visible.mkdir()
    target = visible / "revoked.bin"
    target.write_bytes(b"x" * (3 * 64 * 1024))
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    root = registry.add("administrator", str(visible), "recursive_directory", ["read"])
    registry.assign_visibility("administrator", "bob", root["id"], ["read"])
    monkeypatch.setenv("ODYSSEUS_FILES_REGISTRY", str(registry.path))
    client = OdysseusFilesClient(
        "bob",
        binary=_service_binary(),
        app_scope=registry.app_scope("bob", is_admin=False),
        transport="grpc",
    )

    async def exercise():
        stream = client.stream_read(
            str(target),
            length=target.stat().st_size,
            chunk_bytes=64 * 1024,
        )
        assert await anext(stream) == b"x" * (64 * 1024)
        registry.update("administrator", root["id"], enabled=False)
        with pytest.raises(FilesServiceError) as revoked:
            await anext(stream)
        assert revoked.value.code == "policy_generation_changed"
        await stream.aclose()

    try:
        asyncio.run(exercise())
    finally:
        client.close()


def test_agent_client_cache_key_tracks_policy_generation(tmp_path, monkeypatch):
    visible = tmp_path / "visible"
    visible.mkdir()
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    root = registry.add("bob", str(visible), "recursive_directory", ["read"])
    monkeypatch.setenv("ODYSSEUS_FILES_REGISTRY", str(registry.path))
    close_all_clients()
    first = agent_client_for("bob", str(visible))
    registry.update("bob", root["id"], enabled=False)
    second = agent_client_for("bob", str(visible))
    try:
        assert second is not first
    finally:
        close_all_clients()


def test_cached_clients_do_not_cross_transport_selection(monkeypatch):
    close_all_clients()
    monkeypatch.setenv("ODYSSEUS_FILES_TRANSPORT", "framed")
    framed = client_for_owner("owner")
    monkeypatch.setenv("ODYSSEUS_FILES_TRANSPORT", "grpc")
    tonic = client_for_owner("owner")
    try:
        assert tonic is not framed
        assert framed.transport == "framed"
        assert tonic.transport == "grpc"
    finally:
        close_all_clients()


def test_manage_files_uses_rust_move_trash_restore_for_scoped_agent(tmp_path, monkeypatch):
    from src.agent_tools.filesystem_tools import ManageFilesTool, resolve_file_approval

    visible = tmp_path / "visible"
    visible.mkdir()
    source = visible / "source.txt"
    moved = visible / "moved.txt"
    source.write_text("agent lifecycle", encoding="utf-8")
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    app_root = registry.add("administrator", str(visible), "recursive_directory", ["read", "write"])
    registry.assign_visibility("administrator", "bob", app_root["id"], ["read", "write"])
    registry.add("bob", str(visible), "recursive_directory", ["read", "write"])
    monkeypatch.setenv("ODYSSEUS_FILES_REGISTRY", str(registry.path))
    monkeypatch.setenv("ODYSSEUS_FILES_SERVICE_BIN", _service_binary())
    monkeypatch.setenv("ODYSSEUS_FILES_AGENT_RUNTIME", "1")

    from src import tool_execution
    token = tool_execution._active_workspace.set(str(visible))
    try:
        tool = ManageFilesTool()
        async def approve(event):
            assert event["type"] == "permission_request"
            assert resolve_file_approval(
                event["data"]["request_id"],
                "once",
                owner="bob",
                session_id="chat-bob",
            )

        context = {
            "owner": "bob",
            "session_id": "chat-bob",
            "workspace": str(visible),
            "progress_cb": approve,
        }
        moved_result = asyncio.run(tool.execute(json.dumps({"action": "move", "path": str(source), "destination": str(moved)}), context))
        assert moved_result["exit_code"] == 0
        assert moved.exists() and not source.exists()
        trash_result = asyncio.run(tool.execute(json.dumps({"action": "trash", "path": str(moved)}), context))
        assert trash_result["exit_code"] == 0
        assert not moved.exists()
        entry = {
            "id": trash_result["trash_id"],
            "root_id": trash_result["root_id"],
            "original_path": trash_result["original_path"],
            "trashed_path": trash_result["trashed_path"],
            "fingerprint": trash_result["fingerprint"],
        }
        restored_result = asyncio.run(tool.execute(json.dumps({"action": "restore", "entry": entry}), context))
        assert restored_result["exit_code"] == 0
        assert moved.read_text(encoding="utf-8") == "agent lifecycle"
    finally:
        tool_execution._active_workspace.reset(token)
        close_all_clients()


def test_rust_manage_files_rejects_source_changed_during_approval(tmp_path, monkeypatch):
    from src.agent_tools.filesystem_tools import ManageFilesTool, resolve_file_approval
    from src import tool_execution

    visible = tmp_path / "visible"
    visible.mkdir()
    source = visible / "source.txt"
    destination = visible / "moved.txt"
    source.write_text("before approval", encoding="utf-8")
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    root = registry.add("administrator", str(visible), "recursive_directory", ["read", "write"])
    registry.assign_visibility("administrator", "bob", root["id"], ["read", "write"])
    registry.add("bob", str(visible), "recursive_directory", ["read", "write"])
    monkeypatch.setenv("ODYSSEUS_FILES_REGISTRY", str(registry.path))
    monkeypatch.setenv("ODYSSEUS_FILES_SERVICE_BIN", _service_binary())
    monkeypatch.setenv("ODYSSEUS_FILES_AGENT_RUNTIME", "1")

    token = tool_execution._active_workspace.set(str(visible))
    try:
        async def approve_after_change(event):
            source.write_text("changed while deciding", encoding="utf-8")
            assert resolve_file_approval(
                event["data"]["request_id"],
                "once",
                owner="bob",
                session_id="chat-bob",
            )

        result = asyncio.run(ManageFilesTool().execute(json.dumps({
            "action": "move",
            "path": str(source),
            "destination": str(destination),
        }), {
            "owner": "bob",
            "session_id": "chat-bob",
            "workspace": str(visible),
            "progress_cb": approve_after_change,
        }))

        assert result["exit_code"] == 1
        assert result["code"] == "conflict"
        assert source.read_text(encoding="utf-8") == "changed while deciding"
        assert not destination.exists()
    finally:
        tool_execution._active_workspace.reset(token)
        close_all_clients()


def test_brokered_read_uses_rust_scope_without_legacy_path_ceiling(tmp_path, monkeypatch):
    from src.agent_tools.filesystem_tools import ReadFileTool
    from src import tool_execution

    visible = tmp_path / "visible"
    visible.mkdir()
    target = visible / "outside-legacy-roots.txt"
    target.write_text("brokered", encoding="utf-8")
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    ceiling = registry.add("administrator", str(visible), "recursive_directory", ["read"])
    registry.assign_visibility("administrator", "bob", ceiling["id"], ["read"])
    registry.add("bob", str(visible), "recursive_directory", ["read"])
    monkeypatch.setenv("ODYSSEUS_FILES_REGISTRY", str(registry.path))
    monkeypatch.setenv("ODYSSEUS_FILES_SERVICE_BIN", _service_binary())
    monkeypatch.setenv("ODYSSEUS_FILES_AGENT_RUNTIME", "1")
    # The Rust lane must not ask the legacy resolver for a second, unrelated
    # allowlist decision before the service sees the request.
    monkeypatch.setattr(
        tool_execution,
        "_resolve_tool_path",
        lambda _path: (_ for _ in ()).throw(AssertionError("legacy path ceiling was consulted")),
    )

    token = tool_execution._active_workspace.set(str(visible))
    try:
        result = asyncio.run(
            ReadFileTool().execute(json.dumps({"path": str(target)}), {"owner": "bob"})
        )
        assert result["exit_code"] == 0
        assert result["output"] == "brokered"
    finally:
        tool_execution._active_workspace.reset(token)
        close_all_clients()
