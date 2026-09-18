import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

import src.openclank.copal_bridge as copal_bridge


def write_protocol_binary(path: Path, *, protocol=1, schema=3, capabilities=None, source_identity="sha256:test"):
    capabilities = capabilities or sorted(copal_bridge._REQUIRED_CAPABILITIES)
    status = json.dumps({
        "protocol_version": protocol,
        "source_identity": source_identity,
        "schema_version": schema,
        "capabilities": capabilities,
        "documents": 0,
        "integrity_ok": True,
    }, separators=(",", ":"))
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{json.dumps({'id': 1, 'ok': True, 'result': json.loads(status)})}'\n", encoding="utf-8")
    path.chmod(0o755)
    metadata_path = copal_bridge._artifact_metadata_path(path)
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["artifact_sha256"] = copal_bridge._artifact_sha256(path)
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")


def write_metadata(path: Path, identity: str):
    copal_bridge._artifact_metadata_path(path).write_text(json.dumps({
        "schema_version": 1,
        "build_identity": identity,
        "artifact_sha256": copal_bridge._artifact_sha256(path),
        "protocol_version": 1,
        "storage_schema_version": 3,
        "capabilities": sorted(copal_bridge._REQUIRED_CAPABILITIES),
        "target": "test",
    }), encoding="utf-8")


def test_build_identity_is_deterministic_and_ignores_unrelated_checkout_files(tmp_path):
    crate = tmp_path / "crate"
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text("[package]\nname='fixture'\n", encoding="utf-8")
    (crate / "Cargo.lock").write_text("version = 3\n", encoding="utf-8")
    source = crate / "src" / "main.rs"
    source.write_text("fn main() {}\n", encoding="utf-8")
    toolchain = {"rustc": "rustc 1.0", "cargo": "cargo 1.0"}
    first = copal_bridge.copal_build_identity(crate, target="aarch64-apple-darwin", toolchain=toolchain)
    second = copal_bridge.copal_build_identity(crate, target="aarch64-apple-darwin", toolchain=toolchain)
    assert first == second
    (crate / "README.md").write_text("unrelated\n", encoding="utf-8")
    assert copal_bridge.copal_build_identity(crate, target="aarch64-apple-darwin", toolchain=toolchain) == first
    source.write_text("fn main() { println!(\"changed\"); }\n", encoding="utf-8")
    assert copal_bridge.copal_build_identity(crate, target="aarch64-apple-darwin", toolchain=toolchain) != first


def test_artifact_probe_distinguishes_protocol_schema_and_capabilities(tmp_path):
    binary = tmp_path / "copal-bridge"
    write_protocol_binary(binary)
    write_metadata(binary, "sha256:test")
    verified = copal_bridge.verify_copal_artifact(binary, expected_identity="sha256:test", data_dir=tmp_path / "probe")
    assert verified["status"]["protocol_version"] == 1
    assert verified["status"]["schema_version"] == 3
    assert set(verified["status"]["capabilities"]) == copal_bridge._REQUIRED_CAPABILITIES

    write_protocol_binary(binary, protocol=2)
    with pytest.raises(copal_bridge.CopalBridgeError, match="protocol mismatch"):
        copal_bridge.verify_copal_artifact(binary, expected_identity="sha256:test", data_dir=tmp_path / "probe-protocol")
    write_protocol_binary(binary, schema=2)
    with pytest.raises(copal_bridge.CopalBridgeError, match="storage schema mismatch"):
        copal_bridge.verify_copal_artifact(binary, expected_identity="sha256:test", data_dir=tmp_path / "probe-schema")
    write_protocol_binary(binary, capabilities=["scoped-storage"])
    with pytest.raises(copal_bridge.CopalBridgeError, match="capabilities are incomplete"):
        copal_bridge.verify_copal_artifact(binary, expected_identity="sha256:test", data_dir=tmp_path / "probe-capabilities")

    write_protocol_binary(binary, source_identity="sha256:other")
    with pytest.raises(copal_bridge.CopalBridgeError, match="running source identity mismatch"):
        copal_bridge.verify_copal_artifact(binary, expected_identity="sha256:test", data_dir=tmp_path / "probe-source")

    binary.write_text(binary.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")
    with pytest.raises(copal_bridge.CopalBridgeError, match="packaging digest"):
        copal_bridge.verify_copal_artifact(binary, expected_identity="sha256:test", data_dir=tmp_path / "probe-digest")


def test_explicit_custom_binary_fails_closed_with_actionable_diagnostic(tmp_path):
    binary = tmp_path / "custom-bridge"
    write_protocol_binary(binary, protocol=2)
    write_metadata(binary, "sha256:custom")
    bridge = copal_bridge.CopalBridge(command=binary, data_dir=tmp_path / "data")
    with pytest.raises(copal_bridge.CopalBridgeError, match="protocol mismatch"):
        asyncio.run(bridge._build())


def test_stale_checkout_build_is_serialized_and_keeps_prior_artifact(tmp_path, monkeypatch):
    command = tmp_path / "release" / "copal-bridge"
    command.parent.mkdir()
    command.write_text("prior-artifact", encoding="utf-8")
    command.chmod(0o755)
    write_metadata(command, "sha256:old")
    monkeypatch.setattr(copal_bridge, "_DEFAULT_COMMAND", command)
    monkeypatch.setattr(copal_bridge, "copal_build_identity", lambda: "sha256:new")
    real_run = copal_bridge.subprocess.run
    builds = 0

    def run(args, **kwargs):
        nonlocal builds
        if args[:2] == ["cargo", "build"]:
            builds += 1
            target = Path(kwargs["env"]["CARGO_TARGET_DIR"]) / "release" / "copal-bridge"
            target.parent.mkdir(parents=True)
            write_protocol_binary(target, source_identity="sha256:new")
            return __import__("subprocess").CompletedProcess(args, 0, stdout="", stderr="")
        return real_run(args, **kwargs)

    monkeypatch.setattr(copal_bridge.subprocess, "run", run)

    async def build_twice():
        await asyncio.gather(copal_bridge.CopalBridge(data_dir=tmp_path / "one")._build(), copal_bridge.CopalBridge(data_dir=tmp_path / "two")._build())

    asyncio.run(build_twice())
    assert builds == 1
    assert command.read_text(encoding="utf-8").startswith("#!/bin/sh")
    assert command.with_name(command.name + ".previous").read_text(encoding="utf-8") == "prior-artifact"
    assert json.loads(copal_bridge._artifact_metadata_path(command).read_text())["build_identity"] == "sha256:new"


def test_failed_candidate_does_not_replace_prior_artifact(tmp_path, monkeypatch):
    command = tmp_path / "release" / "copal-bridge"
    command.parent.mkdir()
    command.write_text("prior-artifact", encoding="utf-8")
    command.chmod(0o755)
    write_metadata(command, "sha256:old")
    monkeypatch.setattr(copal_bridge, "_DEFAULT_COMMAND", command)
    monkeypatch.setattr(copal_bridge, "copal_build_identity", lambda: "sha256:new")
    real_run = copal_bridge.subprocess.run
    real_mkdtemp = copal_bridge.tempfile.mkdtemp
    staging = []

    def mkdtemp(*args, **kwargs):
        path = Path(real_mkdtemp(*args, **kwargs))
        if kwargs.get("prefix", "").startswith("copal-build-"):
            staging.append(path)
        return str(path)

    monkeypatch.setattr(copal_bridge.tempfile, "mkdtemp", mkdtemp)

    def run(args, **kwargs):
        if args[:2] == ["cargo", "build"]:
            target = Path(kwargs["env"]["CARGO_TARGET_DIR"]) / "release" / "copal-bridge"
            target.parent.mkdir(parents=True)
            write_protocol_binary(target, protocol=9, source_identity="sha256:new")
            return __import__("subprocess").CompletedProcess(args, 0, stdout="", stderr="")
        return real_run(args, **kwargs)

    monkeypatch.setattr(copal_bridge.subprocess, "run", run)
    with pytest.raises(copal_bridge.CopalBridgeError, match="protocol mismatch"):
        asyncio.run(copal_bridge.CopalBridge(data_dir=tmp_path / "data")._build())
    assert command.read_text(encoding="utf-8") == "prior-artifact"
    assert staging and all(not path.exists() for path in staging)


def test_interrupted_promotion_leaves_prior_valid_and_selection_fail_closed(tmp_path, monkeypatch):
    command = tmp_path / "release" / "copal-bridge"
    command.parent.mkdir()
    command.write_text("prior-artifact", encoding="utf-8")
    command.chmod(0o755)
    copal_bridge._artifact_metadata_path(command).write_text(json.dumps({
        "schema_version": 1,
        "build_identity": "sha256:old",
        "artifact_sha256": copal_bridge._artifact_sha256(command),
        "protocol_version": 1,
        "storage_schema_version": 3,
        "capabilities": sorted(copal_bridge._REQUIRED_CAPABILITIES),
    }), encoding="utf-8")
    monkeypatch.setattr(copal_bridge, "_DEFAULT_COMMAND", command)
    monkeypatch.setattr(copal_bridge, "copal_build_identity", lambda: "sha256:new")
    real_run = copal_bridge.subprocess.run

    def run(args, **kwargs):
        if args[:2] == ["cargo", "build"]:
            target = Path(kwargs["env"]["CARGO_TARGET_DIR"]) / "release" / "copal-bridge"
            target.parent.mkdir(parents=True)
            write_protocol_binary(target, source_identity="sha256:new")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return real_run(args, **kwargs)

    monkeypatch.setattr(copal_bridge.subprocess, "run", run)
    real_replace = copal_bridge.os.replace
    replacements = 0

    def replace(source, destination):
        nonlocal replacements
        real_replace(source, destination)
        replacements += 1
        if replacements == 1:
            raise OSError("injected promotion interruption")

    monkeypatch.setattr(copal_bridge.os, "replace", replace)
    with pytest.raises(OSError, match="injected promotion interruption"):
        asyncio.run(copal_bridge.CopalBridge(data_dir=tmp_path / "data")._build())

    prior = command.with_name(command.name + ".previous")
    assert copal_bridge._validate_artifact_metadata(prior, expected_identity="sha256:old")["build_identity"] == "sha256:old"
    with pytest.raises(copal_bridge.CopalBridgeError, match="packaging digest"):
        copal_bridge._validate_artifact_metadata(command)


def test_windows_lock_fallback_uses_os_locking_api(tmp_path, monkeypatch):
    class FakeMsvcrt:
        LK_NBLCK = 11
        LK_UNLCK = 12

        def __init__(self):
            self.calls = []

        def locking(self, fd, mode, size):
            self.calls.append((fd, mode, size))

    fake = FakeMsvcrt()
    monkeypatch.setattr(copal_bridge, "fcntl", None)
    monkeypatch.setattr(copal_bridge, "msvcrt", fake)
    handle = copal_bridge._acquire_process_lock(tmp_path / "windows.build.lock")
    copal_bridge._release_process_lock(handle)
    assert [call[1:] for call in fake.calls] == [(fake.LK_NBLCK, 1), (fake.LK_UNLCK, 1)]


def test_missing_checkout_build_is_serialized_by_real_process_lock(tmp_path):
    lock_path = tmp_path / "release" / "copal-bridge.build.lock"
    marker = tmp_path / "first-held"
    script = """
import sys
import time
from pathlib import Path
from src.openclank.copal_bridge import _acquire_process_lock, _release_process_lock
handle = _acquire_process_lock(sys.argv[1])
Path(sys.argv[2]).write_text('held')
time.sleep(0.5)
_release_process_lock(handle)
"""
    first = subprocess.Popen(
        [sys.executable, "-c", script, str(lock_path), str(marker)],
        cwd=Path(__file__).parents[1],
    )
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        started = time.monotonic()
        second = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from src.openclank.copal_bridge import _acquire_process_lock, _release_process_lock; h = _acquire_process_lock(sys.argv[1]); _release_process_lock(h)",
                str(lock_path),
            ],
            cwd=Path(__file__).parents[1],
            check=False,
        )
        assert second.returncode == 0
        assert time.monotonic() - started >= 0.35
    finally:
        first.wait(timeout=5)
