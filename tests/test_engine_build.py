import hashlib
import json
import threading
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from src.openclank import engine_build


ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_install(tmp_path: Path) -> tuple[Path, Path]:
    install = tmp_path / "engine"
    target = engine_build.current_target()
    artifact = install / "1.0.2" / target / ("a" * 16)
    artifact.mkdir(parents=True)
    binary = artifact / ("openclank-engine.exe" if target.startswith("windows-") else "openclank-engine")
    binary.write_bytes(b"fixture-managed-engine")
    binary.chmod(0o755)
    provenance = {
        "schema_version": 1,
        "product": "Open Clank",
        "artifact_kind": "managed-model-engine",
        "build_contract_version": 1,
        "open_clank_version": "1.0.2",
        "engine_version": "1.0.2",
        "target": target,
        "support_tier": "tier-1",
        "bun_version": "1.3.14",
        "binary": {
            "name": binary.name,
            "sha256": _sha256(binary),
            "size": binary.stat().st_size,
        },
        "inputs": {
            "vendor_manifest_sha256": "1" * 64,
            "source_sha256": "2" * 64,
            "bun_lock_sha256": "3" * 64,
            "model_catalog_sha256": "4" * 64,
        },
        "protocols": dict(engine_build.SUPPORTED_PROTOCOLS),
        "managed_schema": {
            "id": engine_build.MANAGED_SCHEMA_ID,
            "version": engine_build.MANAGED_SCHEMA_VERSION,
            "sha256": engine_build.MANAGED_SCHEMA_HASH,
        },
        "source": {
            "upstream_repository": "https://github.com/XiaomiMiMo/mimo-code.git",
            "upstream_commit": "5" * 40,
            "open_clank_import_commit": "6" * 40,
            "open_clank_import_tree": "7" * 40,
            "hash_algorithm": "openclank-tree-sha256-v1",
        },
    }
    provenance_path = artifact / "provenance.json"
    provenance_path.write_bytes(engine_build._canonical_json_bytes(provenance))
    pointer = {
        "schema_version": 1,
        "artifact": artifact.relative_to(install).as_posix(),
        "provenance_sha256": _sha256(provenance_path),
    }
    (install / "current.json").write_bytes(engine_build._canonical_json_bytes(pointer))
    return install, binary


def test_vendor_manifest_pins_toolchain_lock_catalog_and_license():
    manifest = engine_build.load_vendor_manifest(ROOT)
    vendor = ROOT / "packages" / "mimo-code"

    assert manifest["toolchain"]["bun"]["version"] == "1.3.14"
    assert set(manifest["build"]["tier_1_targets"]) == {
        "linux-x64",
        "linux-arm64",
        "darwin-arm64",
        "darwin-x64",
        "windows-x64",
    }
    assert _sha256(vendor / manifest["inputs"]["lockfile"]["path"]) == manifest["inputs"]["lockfile"]["sha256"]
    assert _sha256(vendor / manifest["inputs"]["model_catalog"]["path"]) == manifest["inputs"]["model_catalog"]["sha256"]
    assert _sha256(vendor / manifest["license"]["path"]) == manifest["license"]["sha256"]
    assert engine_build.source_fingerprint(vendor) == manifest["build"]["source_sha256"]


def test_linux_musl_is_not_mislabeled_as_a_supported_glibc_target(monkeypatch):
    monkeypatch.setattr(engine_build.sys, "platform", "linux")
    monkeypatch.setattr(engine_build.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(engine_build.platform, "libc_ver", lambda: ("musl", "1.2.5"))

    assert engine_build.current_target() == "linux-x64-musl"


def test_build_generation_is_offline_and_bun_version_is_exact():
    generate = (ROOT / "packages/mimo-code/packages/opencode/script/generate.ts").read_text(encoding="utf-8")
    version = (ROOT / "packages/mimo-code/packages/script/src/index.ts").read_text(encoding="utf-8")
    build = (ROOT / "packages/mimo-code/packages/opencode/script/build.ts").read_text(encoding="utf-8")

    assert "fetch(" not in generate
    assert '"test", "tool", "fixtures", "models-api.json"' in generate
    assert "MODELS_DEV_API_JSON" not in generate
    assert "process.versions.bun !== expectedBunVersion" in version
    assert "bun ci" in build
    assert "bun install" not in build
    assert "splitting: false" in build


def test_engine_smoke_uses_the_generated_managed_protocol_binding():
    response = {
        "_meta": {"openclankManaged": engine_build.client_capability_offer()},
    }
    assert engine_build._managed_capability_error(response) is None

    response["_meta"]["openclankManaged"]["operations"].append("chat.future")
    assert "contract validation failed" in engine_build._managed_capability_error(response)
    assert 'environment["OPEN_CLANK_MANAGED"] = "1"' in Path(engine_build.__file__).read_text(encoding="utf-8")


def test_upstream_engine_update_channels_are_not_reachable():
    source = (ROOT / "packages/mimo-code/packages/opencode/src/cli/upgrade.ts").read_text(encoding="utf-8")
    entrypoint = (ROOT / "packages/mimo-code/packages/opencode/src/index.ts").read_text(encoding="utf-8")
    installation = (ROOT / "packages/mimo-code/packages/opencode/src/installation/index.ts").read_text(encoding="utf-8")

    assert "svc.latest" not in source
    assert "svc.upgrade" not in source
    assert "UpgradeCommand" not in entrypoint
    assert installation.count("managed engine updates are controlled exclusively by Open Clank") == 2


def test_container_builds_and_copies_only_a_verified_engine_payload():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "AS openclank-bun-builder" in dockerfile
    assert "oven/bun" not in dockerfile
    assert "COPY contracts/openclank/managed-provider-v2.schema.json" in dockerfile
    assert "python scripts/openclank_engine.py" in dockerfile
    assert "COPY --from=openclank-bun-builder /engine/ /app/libexec/openclank/engine/" in dockerfile
    assert 'CMD ["python", "scripts/openclank_bootstrap.py", "serve"' in dockerfile
    assert "/cache/" in dockerignore
    assert "/libexec/" in dockerignore


def test_supported_launchers_enter_the_pre_app_bootstrap():
    expected = {
        "start-macos.sh": 'scripts/openclank_bootstrap.py serve --host "$HOST" --port "$PORT"',
        "launch-windows.ps1": "scripts/openclank_bootstrap.py serve --host $BindHost --port $Port",
        "build-macos-app.sh": "scripts/openclank_bootstrap.py serve --host 127.0.0.1",
        "open-clank.service": "scripts/openclank_bootstrap.py serve",
    }
    for filename, command in expected.items():
        source = (ROOT / filename).read_text(encoding="utf-8")
        assert command in source, filename
        assert "uvicorn app:app" not in source, filename


def test_docker_final_payload_contains_the_cutover_bootstrap_dependencies():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    # The final stage copies the repository after dependency installation. Keep
    # these source trees in that context: bootstrap imports both before app.py.
    assert "COPY . ." in dockerfile
    assert "scripts/" not in set(dockerignore.splitlines())
    assert "src/" not in set(dockerignore.splitlines())
    assert (ROOT / "scripts/openclank_bootstrap.py").is_file()
    assert (ROOT / "src/openclank/provider_cutover.py").is_file()


def test_ci_covers_every_tier_one_target_twice_and_signs_release_payloads():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    for target in engine_build.TIER_1_TARGETS:
        assert target in workflow
    assert workflow.count("build --") >= 2
    assert "build --skip-dependencies" in workflow
    assert "openclank-engine-${{ matrix.target }}-signed" in workflow
    assert "cosign sign-blob --yes" in workflow


def test_source_fingerprint_ignores_local_and_generated_state(tmp_path):
    source = tmp_path / "vendor"
    (source / "packages/opencode/src/provider").mkdir(parents=True)
    (source / "packages/opencode/src/code.ts").write_text("export const value = 1\n", encoding="utf-8")
    initial = engine_build.source_fingerprint(source)

    incidental_directories = [
        ".git",
        ".mimocode",
        ".turbo",
        "dist",
        "dist-darwin-arm64",
        "experiment",
        "mimoapi",
        "node_modules",
        "packages/opencode/.mimocode-test-fixtures-one",
        "packages/opencode/research",
        "packages/opencode/src/ext",
        "packages/opencode/tmp",
        "packages/sdk/ts-dist",
        "target",
    ]
    for relative in incidental_directories:
        directory = source / relative
        directory.mkdir(parents=True)
        (directory / "ambient-state").write_bytes(relative.encode("utf-8"))

    incidental_files = {
        ".env": "local environment",
        ".env.local": "local environment variant",
        "README.md": "distribution documentation",
        "app.log": "runtime log",
        "daemon.pid": "123",
        "openclank-vendor.json": "manifest hashes separately",
        "packages/opencode/script/build-darwin.ts": "generated build entrypoint",
        "packages/opencode/src/provider/models-snapshot.js": "generated model catalogue",
        "packages/sdk/tsconfig.tsbuildinfo": "compiler state",
        "scratch.bun-build": "package output",
        "scratch~": "editor backup",
    }
    for relative, content in incidental_files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    assert engine_build.source_fingerprint(source) == initial

    (source / "packages/opencode/src/code.ts").write_text("export const value = 2\n", encoding="utf-8")
    assert engine_build.source_fingerprint(source) != initial


def test_source_fingerprint_distinguishes_admitted_config_and_similar_directory_names(tmp_path):
    source = tmp_path / "vendor"
    (source / "packages/opencode/src").mkdir(parents=True)
    admitted = source / "packages/opencode/src/distribution"
    admitted.mkdir()
    (admitted / "config.ts").write_text("export const value = 1\n", encoding="utf-8")
    (source / ".env.example").write_text("PLACEHOLDER=value\n", encoding="utf-8")
    initial = engine_build.source_fingerprint(source)

    (admitted / "config.ts").write_text("export const value = 2\n", encoding="utf-8")
    assert engine_build.source_fingerprint(source) != initial

    (admitted / "config.ts").write_text("export const value = 1\n", encoding="utf-8")
    (source / ".env.example").write_text("PLACEHOLDER=other\n", encoding="utf-8")
    assert engine_build.source_fingerprint(source) != initial


def test_resolve_and_verify_install_use_current_pointer(tmp_path):
    install, binary = _fixture_install(tmp_path)

    assert engine_build.resolve_installed_binary(install) == binary.resolve()
    result = engine_build.verify_install(install, expected_version="1.0.2", run_smoke=False, acp_smoke=False)
    assert result.ok, result.errors
    assert result.binary == binary.resolve()
    assert result.source_sha256 == "2" * 64
    assert result.to_dict()["binary"] == str(binary.resolve())


def test_verify_rejects_binary_tampering(tmp_path):
    install, binary = _fixture_install(tmp_path)
    binary.write_bytes(b"tampered")

    result = engine_build.verify_install(install, run_smoke=False, acp_smoke=False)
    assert not result.ok
    assert "managed engine binary checksum mismatch" in result.errors


def test_verify_rejects_a_stale_source_fingerprint(tmp_path):
    install, _ = _fixture_install(tmp_path)

    result = engine_build.verify_install(
        install,
        expected_source_sha256="9" * 64,
        run_smoke=False,
        acp_smoke=False,
    )

    assert not result.ok
    assert "installed engine source fingerprint is stale" in result.errors


def test_verify_rejects_a_stale_vendor_manifest(tmp_path):
    install, _ = _fixture_install(tmp_path)

    result = engine_build.verify_install(
        install,
        expected_vendor_manifest_sha256="9" * 64,
        run_smoke=False,
        acp_smoke=False,
    )

    assert not result.ok
    assert "installed engine vendor manifest is stale" in result.errors


def test_resolver_rejects_pointer_traversal(tmp_path):
    install = tmp_path / "engine"
    install.mkdir()
    (install / "current.json").write_text(
        json.dumps({
            "schema_version": 1,
            "artifact": "../outside",
            "provenance_sha256": "0" * 64,
        }),
        encoding="utf-8",
    )

    with pytest.raises(engine_build.EngineBuildError, match="stay below"):
        engine_build.resolve_installed_binary(install)


def test_build_rejects_a_version_that_can_escape_the_install_root(tmp_path):
    with pytest.raises(engine_build.EngineBuildError, match="unsafe"):
        engine_build.build_current(ROOT, tmp_path / "engine", version="../../outside")


def test_generated_provenance_conforms_to_checked_in_schema(tmp_path):
    manifest = engine_build.load_vendor_manifest(ROOT)
    binary = tmp_path / "openclank-engine"
    binary.write_bytes(b"fixture")
    payload = engine_build._provenance_payload(
        manifest=manifest,
        manifest_sha256="8" * 64,
        version="1.0.2",
        target="linux-x64",
        source_sha256="9" * 64,
        binary=binary,
    )
    schema = json.loads(
        (ROOT / "contracts/openclank/engine-provenance-v1.schema.json").read_text(encoding="utf-8")
    )

    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(payload)) == []
    payload["credential"] = "must-never-be-tolerated"
    assert "provenance top-level fields are incompatible" in engine_build._validate_provenance_shape(payload)


def test_failed_pointer_activation_keeps_the_previous_engine_selected(tmp_path, monkeypatch):
    install, original_binary = _fixture_install(tmp_path)
    manifest = engine_build.load_vendor_manifest(ROOT)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"replacement-engine")
    replacement.chmod(0o755)
    provenance = engine_build._provenance_payload(
        manifest=manifest,
        manifest_sha256="8" * 64,
        version="1.0.3",
        target=engine_build.current_target(),
        source_sha256="9" * 64,
        binary=replacement,
    )
    original_atomic_write = engine_build._atomic_write

    def fail_current_pointer(path, payload, mode=0o644):
        if path.name == "current.json":
            raise OSError("injected activation failure")
        return original_atomic_write(path, payload, mode)

    monkeypatch.setattr(engine_build, "_atomic_write", fail_current_pointer)
    with pytest.raises(OSError, match="injected activation"):
        engine_build._install_artifact(
            install_root=install,
            built_binary=replacement,
            provenance=provenance,
        )

    assert engine_build.resolve_installed_binary(install) == original_binary.resolve()


def test_build_lock_serializes_competing_builders(tmp_path):
    lock = tmp_path / "engine" / ".build.lock"
    first_acquired = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_acquired = threading.Event()

    def first():
        with engine_build._build_lock(lock):
            first_acquired.set()
            assert release_first.wait(2)

    def second():
        second_started.set()
        with engine_build._build_lock(lock):
            second_acquired.set()

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    assert first_acquired.wait(2)
    second_thread.start()
    assert second_started.wait(2)
    assert not second_acquired.wait(0.1)
    release_first.set()
    assert second_acquired.wait(2)
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
