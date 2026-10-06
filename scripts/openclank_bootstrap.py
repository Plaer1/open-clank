#!/usr/bin/env python3
"""Service engine verification and read-only provider store admission."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import MutableMapping
from pathlib import Path, PurePosixPath


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.openclank.engine_build import EngineBuildError, ensure_engine_ready  # noqa: E402
from src.runtime_paths import get_default_data_dir  # noqa: E402


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CHECKSUM_LINE_RE = re.compile(r"^([0-9a-f]{64})  ([^\r\n]+)$")
_PORTABLE_EXTRA_SUFFIXES = (".sigstore.json", ".spdx.json")
_BOOLEAN_ENV_TRUE = frozenset({"1", "on", "t", "true", "y", "yes"})
_BOOLEAN_ENV_FALSE = frozenset({"0", "f", "false", "n", "no", "off"})


class BootstrapError(RuntimeError):
    """A pre-app invariant prevented a safe service launch."""


def _normalize_application_environment(
    environment: MutableMapping[str, str] | None = None,
) -> None:
    """Prevent an ambient build ``DEBUG`` value from breaking app import.

    ``AppConfig`` retains the generic ``DEBUG`` name for legacy launchers, while
    the supported application switch is namespaced as ``OPEN_CLANK_DEBUG``.
    Build shells commonly export unrelated values such as ``DEBUG=release``;
    Pydantic rejects those as booleans when Uvicorn imports :mod:`app`.  Preserve
    recognized legacy booleans in canonical form and otherwise restore the
    application's false-by-default behavior.
    """

    target = os.environ if environment is None else environment
    raw = target.get("DEBUG")
    if raw is None:
        return
    normalized = str(raw).strip().lower()
    if normalized in _BOOLEAN_ENV_TRUE:
        target["DEBUG"] = "true"
    elif normalized in _BOOLEAN_ENV_FALSE:
        target["DEBUG"] = "false"
    else:
        target.pop("DEBUG", None)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _portable_child(root: Path, relative: str, label: str) -> Path:
    candidate = PurePosixPath(str(relative or ""))
    if (
        not relative
        or candidate.is_absolute()
        or ".." in candidate.parts
        or "\\" in str(relative)
        or any(part in {"", "."} for part in candidate.parts)
    ):
        raise EngineBuildError(f"portable {label} path is unsafe")
    resolved = root.joinpath(*candidate.parts).resolve()
    if not resolved.is_relative_to(root):
        raise EngineBuildError(f"portable {label} path escapes the payload")
    return resolved


def verify_portable_payload() -> dict | None:
    """Verify a frozen portable payload before service state or ``app`` import.

    The signed ``SHA256SUMS`` file is the release-level integrity root.  The
    embedded engine still performs its own provenance, binary, version, schema,
    and ACP checks afterward; this layer binds that engine to the exact Python
    server/TUI payload that shipped beside it.
    """

    if not getattr(sys, "frozen", False):
        return None
    bundle_root = Path(sys.executable).resolve().parent
    manifest_path = bundle_root / "portable-provenance.json"
    checksums_path = bundle_root / "SHA256SUMS"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        checksum_lines = checksums_path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EngineBuildError(f"portable payload metadata is invalid: {exc}") from exc
    expected_manifest_fields = {
        "schema_version",
        "product",
        "artifact_kind",
        "version",
        "target",
        "public_entrypoint",
        "contents_directory",
        "engine",
        "helpers",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected_manifest_fields:
        raise EngineBuildError("portable provenance fields are incompatible")
    expected_values = {
        "schema_version": 2,
        "product": "Open Clank",
        "public_entrypoint": "openclank.exe",
        "contents_directory": "_internal",
    }
    for field, expected in expected_values.items():
        if manifest.get(field) != expected:
            raise EngineBuildError(f"portable provenance {field} is incompatible")
    target_name = manifest.get("target")
    if target_name not in {"windows-x64", "windows-arm64"} or manifest.get("artifact_kind") != f"{target_name}-portable":
        raise EngineBuildError("portable target/artifact kind is incompatible")
    from src.openclank.engine_build import _verify_pe_target
    _verify_pe_target(Path(sys.executable), target_name)
    if Path(sys.executable).name.casefold() != "openclank.exe":
        raise EngineBuildError("portable public entrypoint has an unexpected name")
    internal_root = (bundle_root / "_internal").resolve()
    runtime_root = Path(getattr(sys, "_MEIPASS", "")).resolve()
    if runtime_root != internal_root:
        raise EngineBuildError("portable runtime content root is incompatible")

    checksums: dict[str, str] = {}
    for line in checksum_lines:
        match = _CHECKSUM_LINE_RE.fullmatch(line)
        if match is None:
            raise EngineBuildError("portable checksum manifest contains a malformed line")
        digest, relative = match.groups()
        if relative in checksums:
            raise EngineBuildError("portable checksum manifest contains a duplicate path")
        target = _portable_child(bundle_root, relative, "checksum")
        if target.is_symlink() or not target.is_file():
            raise EngineBuildError(f"portable payload file is missing: {relative}")
        if _sha256_file(target) != digest:
            raise EngineBuildError(f"portable payload checksum mismatch: {relative}")
        checksums[relative] = digest

    helpers = manifest.get("helpers")
    if not isinstance(helpers, dict) or set(helpers) != {"inventory_path", "inventory_sha256"}:
        raise EngineBuildError("portable helper inventory fields are incompatible")
    inventory_relative = "_internal/bin/helper-artifacts.json"
    if helpers.get("inventory_path") != inventory_relative or checksums.get(inventory_relative) != helpers.get("inventory_sha256"):
        raise EngineBuildError("portable helper inventory is missing or stale")
    try:
        inventory = json.loads(_portable_child(bundle_root, inventory_relative, "helper inventory").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EngineBuildError("portable helper inventory is invalid") from exc
    if not isinstance(inventory, dict) or inventory.get("schema_version") != 1 or inventory.get("target") != target_name:
        raise EngineBuildError("portable helper inventory target/schema disagrees")
    required_helpers = {"odysseus-files-service.exe", "odysseus-shell-thumbnail-helper.exe", "openclank-history-service.exe", "fm-mcp.exe", "openclank-windows-host-apps.exe", "openclank-windows-desktop-capture.exe"}
    admitted_helpers = set()
    artifacts = inventory.get("artifacts")
    if not isinstance(artifacts, list):
        raise EngineBuildError("portable helper artifacts are malformed")
    for helper in artifacts:
        if not isinstance(helper, dict):
            raise EngineBuildError("portable helper metadata is malformed")
        name = helper.get("name")
        if not isinstance(name, str):
            raise EngineBuildError("portable helper name is malformed")
        if name not in required_helpers or name in admitted_helpers:
            raise EngineBuildError("portable helper name is invalid/duplicated")
        admitted_helpers.add(name)
        relative = "_internal/bin/" + name
        binary = _portable_child(bundle_root, relative, "helper")
        if helper.get("target") != target_name or checksums.get(relative) != helper.get("sha256") or binary.stat().st_size != helper.get("size"):
            raise EngineBuildError("portable helper target/hash/size disagrees")
        inputs = helper.get("source_inputs")
        if not isinstance(inputs, list) or not inputs:
            raise EngineBuildError("portable helper source provenance is missing")
        for item in inputs:
            if not isinstance(item, dict) or set(item) != {"path", "sha256"} or not _SHA256_RE.fullmatch(str(item.get("sha256", ""))):
                raise EngineBuildError("portable helper source provenance is malformed")
            _portable_child(bundle_root, str(item["path"]), "helper source identity")
        source_hash = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if source_hash != helper.get("source_sha256"):
            raise EngineBuildError("portable helper source fingerprint disagrees")
        _verify_pe_target(binary, target_name)
    if not required_helpers.issubset(admitted_helpers):
        raise EngineBuildError("portable payload omits required helpers")

    engine = manifest.get("engine")
    expected_engine_fields = {
        "current_path",
        "provenance_path",
        "provenance_sha256",
        "binary_path",
        "binary_sha256",
        "source_sha256",
        "vendor_manifest_sha256",
        "managed_schema_sha256",
        "protocols",
    }
    if not isinstance(engine, dict) or set(engine) != expected_engine_fields:
        raise EngineBuildError("portable engine provenance fields are incompatible")
    critical_paths = {
        "openclank.exe",
        "portable-provenance.json",
        str(engine.get("current_path") or ""),
        str(engine.get("provenance_path") or ""),
        str(engine.get("binary_path") or ""),
    }
    if not critical_paths.issubset(checksums):
        raise EngineBuildError("portable checksum manifest omits a critical payload file")
    for field in (
        "provenance_sha256",
        "binary_sha256",
        "source_sha256",
        "vendor_manifest_sha256",
        "managed_schema_sha256",
    ):
        if not _SHA256_RE.fullmatch(str(engine.get(field) or "")):
            raise EngineBuildError(f"portable engine {field} is invalid")

    provenance_path = _portable_child(
        bundle_root, str(engine["provenance_path"]), "engine provenance"
    )
    binary_path = _portable_child(bundle_root, str(engine["binary_path"]), "engine binary")
    if _sha256_file(provenance_path) != engine["provenance_sha256"]:
        raise EngineBuildError("portable engine provenance checksum is stale")
    if _sha256_file(binary_path) != engine["binary_sha256"]:
        raise EngineBuildError("portable engine binary checksum is stale")
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        current = json.loads(
            _portable_child(bundle_root, str(engine["current_path"]), "engine pointer").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EngineBuildError(f"portable engine metadata is invalid: {exc}") from exc
    artifact = str(current.get("artifact") or "") if isinstance(current, dict) else ""
    expected_provenance = str(
        PurePosixPath(str(engine["current_path"])).parent / artifact / "provenance.json"
    )
    if expected_provenance != engine["provenance_path"]:
        raise EngineBuildError("portable engine pointer and provenance path disagree")
    if not isinstance(provenance, dict):
        raise EngineBuildError("portable engine provenance is malformed")
    if provenance.get("target") != manifest["target"]:
        raise EngineBuildError("portable engine target disagrees with the package")
    if provenance.get("open_clank_version") != manifest["version"]:
        raise EngineBuildError("portable engine version disagrees with the package")
    inputs = provenance.get("inputs") if isinstance(provenance.get("inputs"), dict) else {}
    managed_schema = (
        provenance.get("managed_schema")
        if isinstance(provenance.get("managed_schema"), dict)
        else {}
    )
    comparisons = {
        "source_sha256": inputs.get("source_sha256"),
        "vendor_manifest_sha256": inputs.get("vendor_manifest_sha256"),
        "managed_schema_sha256": managed_schema.get("sha256"),
        "protocols": provenance.get("protocols"),
    }
    for field, actual in comparisons.items():
        if engine.get(field) != actual:
            raise EngineBuildError(f"portable engine {field} disagrees with provenance")

    listed = set(checksums)
    actual: set[str] = set()
    for path in bundle_root.rglob("*"):
        if path.is_symlink():
            raise EngineBuildError("portable payload must not contain symbolic links")
        if not path.is_file():
            continue
        relative = path.relative_to(bundle_root).as_posix()
        if relative == "SHA256SUMS" or relative.endswith(_PORTABLE_EXTRA_SUFFIXES):
            continue
        actual.add(relative)
    unexpected = sorted(actual.difference(listed))
    if unexpected:
        raise EngineBuildError(f"portable payload contains an unchecksummed file: {unexpected[0]}")
    return manifest


def _data_dir(value: Path | None) -> Path:
    configured = value or Path(
        os.environ.get("OPEN_CLANK_DATA_DIR")
        or os.environ.get("ODYSSEUS_DATA_DIR")
        or get_default_data_dir()
    )
    return configured.expanduser().resolve()


def _configured_provider_environment() -> list[str]:
    from src.openclank.provider_startup import PROVIDER_ENV_AUTHORITIES

    return sorted(
        name
        for name in PROVIDER_ENV_AUTHORITIES
        if str(os.environ.get(name) or "").strip()
    )


def _reject_provider_environment() -> None:
    names = _configured_provider_environment()
    if names:
        raise BootstrapError(
            "retired provider credential environment variables are configured: "
            + ", ".join(names)
            + "; import them through /api/v1/providers and remove them from the service environment"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openclank bootstrap",
        description="Verify the private engine and validate the current store before app import",
    )
    parser.add_argument("--data-dir", type=Path)
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="validate the current store and start the complete service")
    serve.add_argument("--host", default=os.environ.get("APP_BIND", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("APP_PORT", "7777")))
    serve.add_argument("uvicorn_args", nargs=argparse.REMAINDER)

    check = sub.add_parser("check", help="verify engine and current provider store")

    runtime = sub.add_parser(
        "runtime",
        help="resolve admitted Python, LifeTools, fm-mcp, and optional browser runtimes",
    )
    runtime.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    runtime.add_argument("--browser", action="store_true")

    return parser


def _report(result, engine) -> dict:
    return {"ok": result["complete"], "engine": {"version": engine.version, "target": engine.target, "source_sha256": engine.source_sha256}, "provider_store": result}


def prepare_service(
    data_dir: Path | None = None,
):
    """Verify the engine and read-only provider state before importing app."""

    _normalize_application_environment()
    verify_portable_payload()
    from src.constants import APP_VERSION

    data = _data_dir(data_dir)
    database_url = str(os.environ.get("DATABASE_URL") or "").strip()
    expected_url = f"sqlite:///{data / 'app.db'}"
    if database_url and database_url != expected_url:
        raise BootstrapError(
            "service startup validation requires the installation SQLite database"
        )
    engine = ensure_engine_ready(version=APP_VERSION).require()
    from src.openclank.provider_startup import validate_provider_store
    try:
        result = validate_provider_store(data)
    except RuntimeError as exc:
        raise BootstrapError(str(exc)) from exc
    _reject_provider_environment()
    os.environ["OPEN_CLANK_DATA_DIR"] = str(data)
    os.environ["OPEN_CLANK_ENGINE_BIN"] = str(engine.binary)
    return data, result, engine


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = args.command or "serve"
    if args.command is None:
        args.host = os.environ.get("APP_BIND", "127.0.0.1")
        args.port = int(os.environ.get("APP_PORT", "7777"))
        args.uvicorn_args = []
    try:
        verify_portable_payload()
        if command == "runtime":
            from src.runtime_paths import RuntimeResolutionError, resolve_runtime_bundle

            try:
                report = resolve_runtime_bundle(args.repo_root, include_browser=args.browser)
            except RuntimeResolutionError as exc:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "code": exc.code,
                            "error": str(exc),
                            "diagnostics": list(exc.diagnostics),
                        },
                        sort_keys=True,
                    )
                )
                return 1
            print(json.dumps({"ok": True, **report}, sort_keys=True))
            return 0
        data = _data_dir(args.data_dir)
        data, result, engine = prepare_service(data)
        report = _report(result, engine)

        if command == "check":
            print(json.dumps(report, sort_keys=True))
            return 0

        if not 1 <= int(args.port) <= 65535:
            raise BootstrapError("server port must be between 1 and 65535")
        environment = os.environ.copy()
        # The launchers pass an explicit --port.  Preserve that selected port
        # for the application as well: callback and internal URL builders use
        # APP_PORT rather than inspecting uvicorn's command line.
        environment["APP_PORT"] = str(args.port)
        environment["OPEN_CLANK_DATA_DIR"] = os.environ["OPEN_CLANK_DATA_DIR"]
        environment["OPEN_CLANK_ENGINE_BIN"] = os.environ["OPEN_CLANK_ENGINE_BIN"]
        command_line = [
            sys.executable,
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            str(args.host),
            "--port",
            str(args.port),
            "--timeout-graceful-shutdown",
            "10",
            *list(args.uvicorn_args or []),
        ]
        os.execvpe(command_line[0], command_line, environment)
        return 0
    except (
        BootstrapError,
        EngineBuildError,
        OSError,
    ) as exc:
        print(f"openclank bootstrap: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
