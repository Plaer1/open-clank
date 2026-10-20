import asyncio
import io
from pathlib import Path

import pytest

import src.desktop_capture as capture
from src.desktop_ocr import corrected_revision, vision_box_to_original


def _png():
    from PIL import Image
    output = io.BytesIO()
    Image.new("RGBA", (1, 1), (0, 0, 0, 255)).save(output, format="PNG")
    return output.getvalue()


def _authorized_importer(monkeypatch, tmp_path):
    from tests.test_files_facade_routes import HostClient
    from src.openclank.file_policy import FilePolicyRepository
    from src.openclank.files_service_client import FilesServiceError
    from src.openclank.filesystem_registry import FilesystemRootRegistry
    import src.openclank.files_service_client as service_client
    root = tmp_path / "workspace"; root.mkdir()
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    record = registry.add("alice", str(root), "recursive_directory", ["read", "write"])
    registry.assign_visibility("alice", "alice", record["id"], ["read", "write"])
    monkeypatch.setenv("ODYSSEUS_FILES_REGISTRY", str(tmp_path / "roots.json"))
    class Client(HostClient):
        async def request(self, operation, path, payload=None):
            if operation == "stat" and not Path(path).exists():
                raise FilesServiceError("missing", code="denied")
            if operation == "mkdir":
                Path(path).mkdir(parents=True, exist_ok=True); return {"data": {"path": path}}
            if operation == "create" and payload and payload.get("content") is not None:
                Path(path).parent.mkdir(parents=True, exist_ok=True); Path(path).write_bytes(payload["content"])
            return await super().request(operation, path, payload)
    monkeypatch.setattr(service_client, "client_for_owner", lambda owner, **kwargs: Client(owner, kwargs.get("app_scope")))
    repo = FilePolicyRepository(tmp_path / "policy.db")
    monkeypatch.setenv("OPEN_CLANK_AUTHORITY_DB_PATH", str(tmp_path / "policy.db"))
    return root, repo, capture.host_files_importer(owner="alice", account_id="acct-alice", workspace=str(root), repository=repo)


def test_capture_requires_owner_opt_in_before_runner(monkeypatch):
    calls = []
    monkeypatch.setattr(capture, "capture_enabled", lambda owner: False)

    def runner(*args, **kwargs):
        calls.append(args)

    with pytest.raises(capture.DesktopCaptureError) as error:
        asyncio.run(capture.capture_desktop({"target": "display", "display": 1}, owner="alice", runner=runner))
    assert error.value.code == "opt_in_required"
    assert calls == []


def test_capture_uses_bounded_argv_and_files_identity(monkeypatch):
    monkeypatch.setattr(capture, "capture_enabled", lambda owner: owner == "alice")
    seen = {}

    def runner(argv, *, timeout):
        seen["argv"] = argv
        seen["timeout"] = timeout
        Path(argv[-1]).write_bytes(_png())
        return {"returncode": 0}

    async def importer(path, *, owner, metadata):
        seen["path"] = str(path)
        return {"resource_key": "files://alice/capture-1", "owner": owner, "metadata": metadata}

    result = asyncio.run(capture.capture_desktop(
        {"target": "region", "region": [0, 1, 20, 30]}, owner="alice", runner=runner, importer=importer,
    ))
    assert result["resource"]["resource_key"].startswith("files://")
    assert seen["argv"][:5] == ["/usr/sbin/screencapture", "-x", "-t", "png", "-R"]
    assert seen["argv"][-2] == "0,1,20,30"
    assert seen["timeout"] == capture.CAPTURE_TIMEOUT_SECONDS
    assert "capture.png" in seen["path"]


def test_capture_owner_isolation_and_cancellation(monkeypatch):
    monkeypatch.setattr(capture, "capture_enabled", lambda owner: owner == "alice")
    event = asyncio.Event()
    event.set()
    with pytest.raises(capture.DesktopCaptureError, match="cancelled"):
        asyncio.run(capture.capture_desktop({"target": "display", "display": 1}, owner="alice", cancel_event=event))
    with pytest.raises(capture.DesktopCaptureError) as error:
        asyncio.run(capture.capture_desktop({"target": "display", "display": 1}, owner="bob"))
    assert error.value.code == "opt_in_required"


@pytest.mark.asyncio
async def test_capture_runner_timeout_is_bounded_and_reaped():
    with pytest.raises(asyncio.TimeoutError):
        await capture._run_capture(["/bin/sleep", "2"], timeout=0.01)


def test_invalid_png_is_rejected_before_import(monkeypatch):
    monkeypatch.setattr(capture, "capture_enabled", lambda owner: True)
    imported = []

    def runner(argv, *, timeout):
        Path(argv[-1]).write_bytes(b"not-png")
        return {"returncode": 0}

    with pytest.raises(capture.DesktopCaptureError) as error:
        asyncio.run(capture.capture_desktop(
            {"target": "display", "display": 1}, owner="alice", runner=runner,
            importer=lambda *args, **kwargs: imported.append(True),
        ))
    assert error.value.code == "size_limit"
    assert imported == []


@pytest.mark.asyncio
async def test_capture_runs_ocr_before_files_import_and_persists_correction_revision(monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "capture_enabled", lambda owner: True)
    monkeypatch.setenv("OPEN_CLANK_AUTHORITY_DB_PATH", str(tmp_path / "authority.db"))
    seen = []

    def runner(argv, *, timeout):
        Path(argv[-1]).write_bytes(_png())
        return {"returncode": 0}

    async def importer(path, *, owner, metadata):
        seen.append((path, metadata))
        return {"resource_key": "files://alice/capture-2" if metadata["kind"] == "desktop_capture" else "files://alice/ocr-rev-1"}

    result = await capture.capture_desktop(
        {"target": "display", "display": 1}, owner="alice", runner=runner,
        importer=importer, ocr_runner=lambda path: {"boxes": [{"x": 1}]},
    )
    assert result["ocr"]["boxes"]
    correction = await capture.correct_capture(
        result["resource"], owner="alice", text="fixed", boxes=[],
    )
    assert correction["original_id"] == result["resource"]["resource_key"]
    assert correction["metadata_revision"]["revision_id"].startswith("desktop-ocr:")
    assert len(seen) == 1


def test_capture_rejects_invisible_target_before_runner(monkeypatch):
    monkeypatch.setattr(capture, "capture_enabled", lambda owner: True)
    called = []

    def runner(*args, **kwargs):
        called.append(True)

    with pytest.raises(capture.DesktopCaptureError) as error:
        asyncio.run(capture.capture_desktop(
            {"target": "window", "window_id": 7}, owner="alice", runner=runner,
            metadata_reader=lambda request: {"visible": False, "bounds": [0, 0, 100, 100]},
        ))
    assert error.value.code == "target_unavailable"
    assert called == []


@pytest.mark.asyncio
async def test_capture_tool_correction_uses_authoritative_event(monkeypatch, tmp_path):
    root, repo, importer = _authorized_importer(monkeypatch, tmp_path)
    source = tmp_path / "capture.png"; source.write_bytes(_png())
    original = await importer(source, owner="alice", metadata={"owner": "alice", "ocr": {"text": "recognized"}})
    result = await capture.capture_tool('{"action":"correct","original":{"resource_key":"%s","owner":"alice","revision":"forged","ocr":{"text":"forged"}},"text":"fixed","boxes":[]}' % original["resource_key"], {"owner":"alice", "files_importer": importer})
    assert result["state"] == "corrected"
    with __import__("sqlite3").connect(repo.db_path) as db:
        row = db.execute("SELECT recognized, source_revision FROM desktop_capture_revisions").fetchone()
    assert "recognized" in row[0] and "forged" not in row[0]
    assert "forged" not in row[1]


@pytest.mark.asyncio
async def test_capture_tool_rejections_leave_no_revision_rows(monkeypatch, tmp_path):
    root, repo, importer = _authorized_importer(monkeypatch, tmp_path)
    cases = [("bob", "rr"), ("alice", "wrong-ref"), ("alice", "host:v1:directory")]
    for owner, ref in cases:
        with pytest.raises(capture.DesktopCaptureError):
            await capture.capture_tool('{"action":"correct","original":{"resource_key":"%s","owner":"%s"},"text":"x","boxes":[]}' % (ref, owner), {"owner":"alice", "files_importer": importer})
    with __import__("sqlite3").connect(repo.db_path) as db:
        try:
            count = db.execute("SELECT COUNT(*) FROM desktop_capture_revisions").fetchone()[0]
        except __import__("sqlite3").OperationalError:
            count = 0
        assert count == 0


@pytest.mark.asyncio
async def test_capture_metadata_event_cardinality_and_repair_retry(monkeypatch, tmp_path):
    root, repo, importer = _authorized_importer(monkeypatch, tmp_path)
    source = tmp_path / "capture.png"; source.write_bytes(_png())
    await importer(source, owner="alice", metadata={"owner":"alice", "ocr":{"text":"one"}})
    await importer(source, owner="alice", metadata={"owner":"alice", "ocr":{"text":"one"}})
    await importer(source, owner="alice", metadata={"owner":"alice", "ocr":{"text":"two"}})
    with __import__("sqlite3").connect(repo.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM desktop_capture_metadata_events").fetchone()[0] == 2
    assert len(list((root / "Captures").glob("*.png"))) == 1
    original = capture._persist_capture_metadata_event
    failed = {"once": True}
    def fail_once(*args, **kwargs):
        if failed["once"]:
            failed["once"] = False; raise RuntimeError("metadata store unavailable")
        return original(*args, **kwargs)
    monkeypatch.setattr(capture, "_persist_capture_metadata_event", fail_once)
    with pytest.raises(RuntimeError):
        await importer(source, owner="alice", metadata={"owner":"alice", "ocr":{"text":"three"}})
    monkeypatch.setattr(capture, "_persist_capture_metadata_event", original)
    await importer(source, owner="alice", metadata={"owner":"alice", "ocr":{"text":"three"}})
    with __import__("sqlite3").connect(repo.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM desktop_capture_metadata_events").fetchone()[0] == 3
    assert len(list((root / "Captures").glob("*.png"))) == 1


@pytest.mark.asyncio
async def test_host_importer_rejects_request_owner_mismatch_before_files(monkeypatch, tmp_path):
    importer = capture.host_files_importer(owner="alice", account_id="acct-alice", workspace=str(tmp_path), repository=None)
    with pytest.raises(capture.DesktopCaptureError) as error:
        await importer(tmp_path / "missing.png", owner="bob", metadata={"owner": "bob"})
    assert error.value.code == "files_unavailable"


@pytest.mark.asyncio
async def test_capture_metadata_event_replay_and_authority_validation(monkeypatch, tmp_path):
    # Exercise the production importer validator contract with an actual
    # resource-shaped capture and reject forged workspace/owner identities.
    importer = capture.host_files_importer(owner="alice", account_id="acct", workspace=str(tmp_path), repository=None)
    assert callable(getattr(importer, "validate_resource"))
    assert await importer.validate_resource({"resource_key": "forged", "owner": "alice"}, "alice") is None
    assert await importer.validate_resource({"resource_key": "forged", "owner": "bob"}, "alice") is None
    with pytest.raises(capture.DesktopCaptureError):
        await capture.correct_capture({"resource_key": "forged", "owner": "bob"}, owner="alice", text="x", boxes=[])


@pytest.mark.asyncio
async def test_host_importer_facade_replay_with_authorized_temp_root(monkeypatch, tmp_path):
    from tests.test_files_facade_routes import HostClient
    from src.openclank.file_policy import FilePolicyRepository
    from src.openclank.files_service_client import FilesServiceError
    from src.openclank.filesystem_registry import FilesystemRootRegistry
    import src.openclank.files_service_client as service_client
    root = tmp_path / "workspace"; root.mkdir()
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    record = registry.add("alice", str(root), "recursive_directory", ["read", "write"])
    registry.assign_visibility("alice", "alice", record["id"], ["read", "write"])
    monkeypatch.setenv("ODYSSEUS_FILES_REGISTRY", str(tmp_path / "roots.json"))
    class Client(HostClient):
        async def request(self, operation, path, payload=None):
            if operation == "stat" and not Path(path).exists():
                raise FilesServiceError("missing", code="denied")
            if operation == "mkdir":
                Path(path).mkdir(parents=True, exist_ok=True)
                return {"data": {"path": path}}
            if operation == "create" and payload and payload.get("content") is not None:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_bytes(payload["content"])
            return await super().request(operation, path, payload)
    monkeypatch.setattr(service_client, "client_for_owner", lambda owner, **kwargs: Client(owner, kwargs.get("app_scope")))
    importer = capture.host_files_importer(owner="alice", account_id="acct-alice", workspace=str(root), repository=FilePolicyRepository(tmp_path / "policy.db"))
    source = tmp_path / "capture.png"; source.write_bytes(_png())
    first = await importer(source, owner="alice", metadata={"owner": "alice"})
    second = await importer(source, owner="alice", metadata={"owner": "alice"})
    assert first["resource_key"] == second["resource_key"]






def test_ocr_coordinate_transform_and_correction_revision():
    box = vision_box_to_original({"x": 0.25, "y": 0.5, "width": 0.25, "height": 0.25}, width=800, height=600, crop=(10, 20, 400, 300), scale=2)
    assert box == {"x": 110.0, "y": 95.0, "width": 100.0, "height": 75.0}
    revision = corrected_revision("capture-1", 1, "corrected", [box])
    assert revision["original_id"] == "capture-1"
    assert revision["source"] == "user"


@pytest.mark.asyncio
async def test_correction_request_identity_replays_concurrently(monkeypatch, tmp_path):
    monkeypatch.setenv("OPEN_CLANK_AUTHORITY_DB_PATH", str(tmp_path / "authority.db"))
    original = {"resource_key": "rr-owner-bound", "owner": "alice", "revision": {"kind": "hostFingerprint", "value": "abc"}, "ocr": {"text": "recognized", "boxes": []}}
    results = await asyncio.gather(*[capture.correct_capture(original, owner="alice", text="fixed", boxes=[]) for _ in range(2)])
    assert {item["revision"] for item in results} == {1}
    assert results[0]["metadata_revision"]["revision_id"] == results[1]["metadata_revision"]["revision_id"]
    import sqlite3
    with sqlite3.connect(tmp_path / "authority.db") as db:
        row = db.execute("SELECT recognized, source_revision FROM desktop_capture_revisions").fetchone()
    assert "recognized" in row[0]
    assert "hostFingerprint" in row[1]


@pytest.mark.asyncio
async def test_execute_tool_block_threads_owner_and_files_importer(monkeypatch):
    from src.tool_blocks import ToolBlock
    from src.tool_execution import execute_tool_block

    monkeypatch.setattr(capture, "capture_enabled", lambda owner: owner == "alice")

    def runner(argv, *, timeout):
        Path(argv[-1]).write_bytes(_png())
        return {"returncode": 0}

    async def importer(path, *, owner, metadata):
        return {"resource_key": f"files://{owner}/capture-1", "metadata": metadata}

    monkeypatch.setattr(capture, "_run_capture", runner)
    block = ToolBlock("capture_desktop", '{"target":"display","display":1}')
    _desc, result = await execute_tool_block(block, owner="alice", files_importer=importer)
    assert result["resource"]["resource_key"] == "files://alice/capture-1"


@pytest.mark.asyncio
async def test_execute_tool_block_final_door_builds_owner_workspace_importer(monkeypatch):
    from src.tool_blocks import ToolBlock
    from src.tool_execution import execute_tool_block

    monkeypatch.setattr(capture, "capture_enabled", lambda owner: True)
    import src.tool_execution as execution
    monkeypatch.setattr(execution, "_copal_account_id", lambda owner: (owner, "acct-alice", None))
    monkeypatch.setattr(execution, "_trusted_workspace_from_id", lambda *args, **kwargs: "/authorized")

    def runner(argv, *, timeout):
        Path(argv[-1]).write_bytes(_png())
        return {"returncode": 0}

    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)

        async def importer(path, *, owner, metadata):
            return {"resource_key": "rr2.owner-bound"}

        return importer

    monkeypatch.setattr(capture, "host_files_importer", factory)
    class _Helpers:
        def metadata(self, request):
            return {"id": 123, "index": request["display"], "visible": True, "bounds": [0, 0, 100, 100], "scale": 2}
        def ocr(self, path):
            return {"boxes": [], "width": 1, "height": 1}
    monkeypatch.setattr(capture, "_NativeDesktopHelpers", _Helpers)
    monkeypatch.setattr(capture, "_run_capture", runner)
    block = ToolBlock("capture_desktop", '{"target":"display","display":1}')
    _desc, result = await execute_tool_block(
        block, owner="alice", workspace="/authorized", authority_workspace_id="ws-1",
    )
    assert result["resource"]["resource_key"] == "rr2.owner-bound"
    assert captured == {"owner": "alice", "account_id": "acct-alice", "workspace": "/authorized"}
