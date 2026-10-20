from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_windows_portable_build_has_one_real_public_command_and_verified_engine():
    script = (ROOT / "build-windows-portable.ps1").read_text(encoding="utf-8")

    assert "--name openclank" in script
    assert "--console" in script
    assert "openclank_entry.py" in script
    assert "launcher.py" not in script
    assert "scripts/openclank_engine.py build --json" in script
    assert "$publicExe engine verify --json" in script
    assert '((Join-Path $stageRoot "libexec") + ";libexec")' in script
    assert "Copy-Item $sourceArtifact $stageArtifact -Recurse -Force" in script
    assert '"--add-data", ".env.example;.env.example"' not in script
    assert "pyinstaller==6.16.0" in script
    assert "--contents-directory _internal" in script
    assert "--noupx" in script
    assert "portable-provenance.json" in script
    assert "SHA256SUMS" in script
    assert "Odysseus" not in script


def test_frozen_tui_auto_start_uses_hidden_in_process_server_entrypoint():
    cli = (ROOT / "src/openclank/cli.py").read_text(encoding="utf-8")
    manager = (ROOT / "src/openclank/server_manager.py").read_text(encoding="utf-8")
    bootstrap = (ROOT / "scripts/openclank_bootstrap.py").read_text(encoding="utf-8")

    assert 'values[0] == "__managed-server"' in cli
    assert '"__managed-server"' in manager
    assert "prepare_service()" in cli
    assert "from app import app" in cli
    assert cli.index("prepare_service()") < cli.index("from app import app")
    assert "def prepare_service(" in bootstrap


def test_windows_ci_builds_and_smokes_the_complete_portable_bundle():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "windows-portable:" in workflow
    assert "build-windows-portable.ps1 -UseExistingVerifiedEngine" in workflow
    assert "dist/openclank/openclank.exe" in workflow
    assert "Portable fail-closed startup smoke" in workflow
    assert "openclank-windows-x64-portable" in workflow


def test_native_windows_launcher_uses_current_product_name():
    script = (ROOT / "launch-windows.ps1").read_text(encoding="utf-8")
    assert "Starting Open Clank" in script
    assert "Odysseus" not in script


def _bootstrap_module():
    script = ROOT / "scripts" / "openclank_bootstrap.py"
    spec = importlib.util.spec_from_file_location("windows_portable_bootstrap_test", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_portable_fixture(root: Path) -> tuple[Path, Path]:
    internal = root / "_internal"
    engine_root = internal / "libexec/openclank/engine"
    artifact = engine_root / "1.0.2/windows-x64/fixture"
    artifact.mkdir(parents=True)
    binary = artifact / "openclank-engine.exe"
    binary.write_bytes(b"managed-engine")
    provenance = {
        "open_clank_version": "1.0.2",
        "target": "windows-x64",
        "binary": {"name": binary.name},
        "inputs": {
            "source_sha256": "1" * 64,
            "vendor_manifest_sha256": "2" * 64,
        },
        "managed_schema": {"sha256": "3" * 64},
        "protocols": {"managed_acp": 1, "provider_store": 1, "operation_router": 1},
    }
    provenance_path = artifact / "provenance.json"
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
    current_path = engine_root / "current.json"
    current_path.write_text(
        json.dumps({"artifact": "1.0.2/windows-x64/fixture"}), encoding="utf-8"
    )
    executable = root / "openclank.exe"
    executable.write_bytes(b"public-command")
    relative_provenance = provenance_path.relative_to(root).as_posix()
    relative_binary = binary.relative_to(root).as_posix()
    manifest = {
        "schema_version": 1,
        "product": "Open Clank",
        "artifact_kind": "windows-x64-portable",
        "version": "1.0.2",
        "target": "windows-x64",
        "public_entrypoint": "openclank.exe",
        "contents_directory": "_internal",
        "engine": {
            "current_path": current_path.relative_to(root).as_posix(),
            "provenance_path": relative_provenance,
            "provenance_sha256": hashlib.sha256(provenance_path.read_bytes()).hexdigest(),
            "binary_path": relative_binary,
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "source_sha256": "1" * 64,
            "vendor_manifest_sha256": "2" * 64,
            "managed_schema_sha256": "3" * 64,
            "protocols": provenance["protocols"],
        },
    }
    manifest_path = root / "portable-provenance.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    lines = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}")
    (root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="ascii")
    return executable, internal


def test_frozen_portable_verifier_binds_every_payload_file(tmp_path, monkeypatch):
    module = _bootstrap_module()
    executable, internal = _write_portable_fixture(tmp_path)
    monkeypatch.setattr(module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(module.sys, "executable", str(executable))
    monkeypatch.setattr(module.sys, "_MEIPASS", str(internal), raising=False)

    report = module.verify_portable_payload()
    assert report["target"] == "windows-x64"

    (internal / "unexpected.dll").write_bytes(b"not checksummed")
    with pytest.raises(module.EngineBuildError, match="unchecksummed file"):
        module.verify_portable_payload()


def test_frozen_portable_verifier_rejects_tampering(tmp_path, monkeypatch):
    module = _bootstrap_module()
    executable, internal = _write_portable_fixture(tmp_path)
    monkeypatch.setattr(module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(module.sys, "executable", str(executable))
    monkeypatch.setattr(module.sys, "_MEIPASS", str(internal), raising=False)
    (internal / "libexec/openclank/engine/current.json").write_text("{}", encoding="utf-8")

    with pytest.raises(module.EngineBuildError, match="checksum mismatch"):
        module.verify_portable_payload()
