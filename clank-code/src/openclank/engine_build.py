"""Deterministic build, install, and verification contract for Open Clank's engine.

The managed model engine is vendored source, not an opaque optional download.
This module deliberately derives provenance from file content and the tracked
vendor manifest; it never reads ``packages/mimo-code/.git``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from src.openclank.managed_protocol import (
    MANAGED_METHODS,
    MODEL_OPERATIONS as MANAGED_OPERATIONS,
    OPERATION_ROUTER_VERSION,
    PROTOCOL_VERSION,
    PROVIDER_STORE_VERSION,
    SCHEMA_ID as MANAGED_SCHEMA_ID,
    SCHEMA_SHA256 as MANAGED_SCHEMA_HASH,
    SCHEMA_VERSION as MANAGED_SCHEMA_VERSION,
    ManagedProtocolError,
    client_capability_offer,
    validate_initialize_result,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_RELATIVE_PATH = Path("packages/mimo-code")
VENDOR_MANIFEST_NAME = "openclank-vendor.json"
PROVENANCE_SCHEMA_VERSION = 1
CURRENT_POINTER_SCHEMA_VERSION = 1
SUPPORTED_PROTOCOLS = {
    "managed_acp": PROTOCOL_VERSION,
    "provider_store": PROVIDER_STORE_VERSION,
    "operation_router": OPERATION_ROUTER_VERSION,
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,127}$")
_TARGET_RE = re.compile(r"^(linux|darwin|windows)-(arm64|x64)(-(baseline|musl))?$")
TIER_1_TARGETS = frozenset({
    "linux-x64",
    "linux-arm64",
    "darwin-arm64",
    "darwin-x64",
    "windows-x64",
})
BUN_TOOLCHAIN_TARGETS = TIER_1_TARGETS | {"windows-arm64"}
EXPERIMENTAL_TARGETS = frozenset({
    "linux-x64-baseline",
    "linux-x64-musl",
    "linux-arm64-musl",
    "windows-arm64",
})
_SOURCE_EXCLUDED_DIRS = {
    # Repository/editor state.
    ".claude",
    ".codex",
    ".dev-home",
    ".direnv",
    ".artifacts",
    ".cache",
    ".git",
    ".idea",
    ".openclank-worktrees",
    ".openclank",
    ".mimocode",
    ".mimo-worktrees",
    ".playwright-cli",
    ".scripts",
    ".serena",
    ".sst",
    ".turbo",
    ".vscode",
    ".worktrees",
    # Dependency, build, test, and runtime outputs.
    "__pycache__",
    "coverage",
    "dist",
    "gen",
    "logs",
    "node_modules",
    "playground",
    "refs",
    "result",
    "target",
    "temp",
    "tmp",
    "ts-dist",
}
_SOURCE_EXCLUDED_DIR_PREFIXES = (
    ".openclank-test-fixtures-",
    ".mimocode-test-fixtures-",
    "dist-",
)
_SOURCE_EXCLUDED_RELATIVE_DIRS = {
    # These paths are explicitly generated/local in the vendored project's
    # ignore contract. Restrict them by relative path so similarly named real
    # source directories elsewhere remain admitted inputs.
    # Retired pre-upgrade source; private/excluded runtime data stays untouched.
    "packages/app",
    "packages/console",
    "packages/desktop",
    "packages/enterprise",
    "packages/function",
    "packages/opencode",
    "packages/script",
    "packages/slack",
    "packages/storybook",
    "packages/ui",
    "packages/web",
    "experiment",
    "mimoapi",
    "packages/cli/research",
    "packages/cli/src/ext",
}
_SOURCE_EXCLUDED_FILES = {
    # The manifest pins this tree digest, so it must not hash itself. Its own
    # bytes are hashed separately into artifact provenance.
    VENDOR_MANIFEST_NAME,
    "packages/cli/src/ext/_manifest.ts",
    "packages/cli/src/provider/models-snapshot.d.ts",
    "packages/cli/src/provider/models-snapshot.js",
    "packages/sdk/js/openapi.json",
}


def _source_directory_excluded(relative: Path) -> bool:
    name = relative.name
    return (
        name in _SOURCE_EXCLUDED_DIRS
        or any(name.startswith(prefix) for prefix in _SOURCE_EXCLUDED_DIR_PREFIXES)
        or relative.as_posix() in _SOURCE_EXCLUDED_RELATIVE_DIRS
    )


def _source_file_excluded(relative: Path) -> bool:
    name = relative.name
    relative_text = relative.as_posix()
    if relative_text in _SOURCE_EXCLUDED_FILES:
        return True
    # Both runtime skill bundle macros embed Markdown.
    # Other Markdown stays documentation.
    if relative.suffix.lower() == ".md":
        return not (
            relative_text.startswith(("packages/cli/src/skill/builtin/.bundle/",
                                      "packages/cli/src/skill/compose/.bundle/"))
            and len(relative.parts) >= 8
        )
    # Compiler, package, log, or process outputs are not build inputs.
    if relative.suffix.lower() in {".tsbuildinfo", ".log", ".pid", ".tgz"}:
        return True
    if name in {".DS_Store", "Thumbs.db"} or name.endswith(("~", ".bun-build")):
        return True
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    # opencode's build-* scripts are generated per target; build.ts is the
    # admitted source entrypoint and must remain fingerprinted.
    return (
        relative.parent.as_posix() == "packages/cli/script"
        and name.startswith("build-")
        and relative.suffix == ".ts"
    )


class EngineBuildError(RuntimeError):
    """The engine cannot be built or trusted under the pinned contract."""


@dataclass(frozen=True)
class EngineVerification:
    ok: bool
    binary: Path | None
    provenance: Path | None
    version: str | None
    target: str | None
    source_sha256: str | None
    errors: tuple[str, ...] = ()
    addon: Mapping[str, Any] | None = None
    declared_addon: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "binary": str(self.binary) if self.binary else None,
            "provenance": str(self.provenance) if self.provenance else None,
            "version": self.version,
            "target": self.target,
            "source_sha256": self.source_sha256,
            "errors": list(self.errors),
        }

    def require(self) -> "EngineVerification":
        if not self.ok:
            raise EngineBuildError("; ".join(self.errors) or "engine verification failed")
        return self


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EngineBuildError(f"invalid {label} at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EngineBuildError(f"invalid {label} at {path}: expected an object")
    return value


def _safe_child(root: Path, relative: str, label: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or not relative or ".." in candidate.parts:
        raise EngineBuildError(f"invalid {label}: path must stay below its contract root")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise EngineBuildError(f"invalid {label}: path escapes its contract root")
    return resolved


def current_target() -> str:
    os_name = {
        "darwin": "darwin",
        "linux": "linux",
        "win32": "windows",
    }.get(sys.platform)
    machine = platform.machine().lower()
    architecture = {
        "aarch64": "arm64",
        "amd64": "x64",
        "arm64": "arm64",
        "x86_64": "x64",
    }.get(machine)
    if os_name is None or architecture is None:
        raise EngineBuildError(f"unsupported engine build target: {sys.platform}/{machine}")
    suffix = "-musl" if os_name == "linux" and platform.libc_ver()[0].lower() == "musl" else ""
    return f"{os_name}-{architecture}{suffix}"


def _native_windows_target() -> str:
    """Query the OS architecture, separately from an emulated Python process."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    query = kernel.IsWow64Process2
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.USHORT), ctypes.POINTER(wintypes.USHORT)]
    query.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    process_machine, native_machine = wintypes.USHORT(), wintypes.USHORT()
    if not query(kernel.GetCurrentProcess(), ctypes.byref(process_machine), ctypes.byref(native_machine)):
        raise EngineBuildError("cannot determine native Windows engine architecture")
    architecture = {0x8664: "x64", 0xAA64: "arm64"}.get(native_machine.value)
    if architecture is None:
        raise EngineBuildError("unsupported native Windows engine architecture")
    return "windows-" + architecture


def _compatible_host_target(target: str) -> bool:
    if target == current_target():
        return True
    # Windows ARM runs native ARM64 engines beside an emulated x64 Python host.
    # Explicit expected_target verification remains exact; this is runtime admission only.
    return sys.platform == "win32" and _native_windows_target() == "windows-arm64" and target in {"windows-arm64", "windows-x64"}


def _verify_pe_target(binary: Path, target: str) -> None:
    if not target.startswith("windows-"):
        return
    with binary.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise EngineBuildError("Windows engine is not a PE executable")
        offset = struct.unpack_from("<I", header, 60)[0]
        if offset > binary.stat().st_size - 6:
            raise EngineBuildError("Windows engine has an invalid PE header")
        stream.seek(offset)
        pe = stream.read(6)
    expected = {"windows-x64": 0x8664, "windows-arm64": 0xAA64}.get(target)
    if pe[:4] != b"PE\0\0" or expected is None or struct.unpack_from("<H", pe, 4)[0] != expected:
        raise EngineBuildError("Windows engine PE architecture disagrees with its target")


def default_install_root(repo_root: Path | str | None = None) -> Path:
    return Path(repo_root or REPO_ROOT).resolve() / "libexec" / "openclank" / "engine"


def _vendor_root(repo_root: Path) -> Path:
    return repo_root / VENDOR_RELATIVE_PATH


def load_vendor_manifest(repo_root: Path | str | None = None) -> dict[str, Any]:
    root = Path(repo_root or REPO_ROOT).resolve()
    vendor_root = _vendor_root(root)
    manifest_path = vendor_root / VENDOR_MANIFEST_NAME
    manifest = _load_json(manifest_path, "Open Clank engine vendor manifest")

    required_top = {
        "schema_version",
        "product",
        "component",
        "vendor",
        "license",
        "toolchain",
        "inputs",
        "protocols",
        "managed_schema",
        "build",
    }
    if set(manifest) != required_top:
        missing = required_top.difference(manifest)
        unexpected = set(manifest).difference(required_top)
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unexpected:
            details.append("unexpected " + ", ".join(sorted(unexpected)))
        raise EngineBuildError("vendor manifest fields are incompatible: " + "; ".join(details))
    if manifest["schema_version"] != 1 or manifest["product"] != "Open Clank":
        raise EngineBuildError("unsupported Open Clank engine vendor manifest")
    if manifest["component"] != "managed-model-engine":
        raise EngineBuildError("vendor manifest names the wrong component")

    vendor = manifest["vendor"]
    if not isinstance(vendor, dict) or set(vendor) != {
        "name",
        "upstream_repository",
        "upstream_commit",
        "open_clank_import_commit",
        "open_clank_import_tree",
    }:
        raise EngineBuildError("vendor manifest vendor must be an object")
    if vendor.get("name") != "MiMo Code":
        raise EngineBuildError("vendor manifest names the wrong upstream engine")
    if vendor.get("upstream_repository") != "https://github.com/XiaomiMiMo/mimo-code.git":
        raise EngineBuildError("vendor manifest has an unexpected upstream repository")
    for field in ("upstream_commit", "open_clank_import_commit", "open_clank_import_tree"):
        if not _GIT_SHA_RE.fullmatch(str(vendor.get(field, ""))):
            raise EngineBuildError(f"vendor manifest has an invalid {field}")

    toolchain = manifest.get("toolchain")
    if not isinstance(toolchain, dict) or set(toolchain) != {"bun"}:
        raise EngineBuildError("vendor manifest toolchain fields are incompatible")
    bun = toolchain.get("bun", {})
    if not isinstance(bun, dict) or set(bun) != {"version", "release_repository", "downloads"}:
        raise EngineBuildError("vendor manifest Bun contract fields are incompatible")
    if bun.get("version") != "1.4.0":
        raise EngineBuildError("the managed engine requires exactly Bun 1.4.0")
    if bun.get("release_repository") != "https://github.com/oven-sh/bun":
        raise EngineBuildError("vendor manifest has an unexpected Bun release repository")
    package_manager = _load_json(vendor_root / "package.json", "vendored package manifest").get("packageManager")
    if package_manager != "bun@1.4.0":
        raise EngineBuildError("packages/mimo-code/package.json must pin bun@1.4.0")
    downloads = bun.get("downloads")
    if not isinstance(downloads, dict) or set(downloads) != BUN_TOOLCHAIN_TARGETS:
        raise EngineBuildError("vendor manifest is missing a pinned Bun toolchain")
    for target in BUN_TOOLCHAIN_TARGETS:
        contract = downloads[target]
        if not isinstance(contract, dict) or set(contract) != {"url", "sha256"}:
            raise EngineBuildError(f"vendor manifest has an invalid Bun toolchain for {target}")
        url = str(contract.get("url", ""))
        if not url.startswith("https://github.com/oven-sh/bun/releases/download/bun-v1.4.0/"):
            raise EngineBuildError(f"vendor manifest has an invalid Bun source for {target}")
        if not _SHA256_RE.fullmatch(str(contract.get("sha256", ""))):
            raise EngineBuildError(f"vendor manifest has an invalid Bun checksum for {target}")

    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != {"lockfile", "model_catalog"}:
        raise EngineBuildError("vendor manifest input fields are incompatible")
    for input_name in ("lockfile", "model_catalog"):
        contract = manifest.get("inputs", {}).get(input_name, {})
        if not isinstance(contract, dict) or set(contract) != {"path", "sha256"}:
            raise EngineBuildError(f"vendor manifest has an invalid {input_name} input")
        path = _safe_child(vendor_root, str(contract.get("path", "")), f"{input_name} input")
        expected = str(contract.get("sha256", ""))
        if not _SHA256_RE.fullmatch(expected) or _sha256_file(path) != expected:
            raise EngineBuildError(f"pinned {input_name} input does not match the vendor manifest")

    license_contract = manifest.get("license", {})
    if not isinstance(license_contract, dict) or set(license_contract) != {
        "spdx",
        "path",
        "sha256",
    }:
        raise EngineBuildError("vendor manifest license fields are incompatible")
    if license_contract.get("spdx") != "MIT":
        raise EngineBuildError("vendored engine license must remain MIT")
    license_path = _safe_child(vendor_root, str(license_contract.get("path", "")), "license")
    if _sha256_file(license_path) != license_contract.get("sha256"):
        raise EngineBuildError("vendored license does not match the vendor manifest")

    if manifest.get("protocols") != SUPPORTED_PROTOCOLS:
        raise EngineBuildError("vendor protocol versions do not match the Open Clank host")
    managed_schema = manifest.get("managed_schema", {})
    if not isinstance(managed_schema, dict) or set(managed_schema) != {
        "id",
        "version",
        "repository_path",
        "sha256",
    }:
        raise EngineBuildError("vendor managed-schema fields are incompatible")
    managed_schema_path = _safe_child(root, str(managed_schema.get("repository_path", "")), "managed schema")
    if (
        managed_schema.get("id") != MANAGED_SCHEMA_ID
        or managed_schema.get("version") != MANAGED_SCHEMA_VERSION
        or managed_schema.get("sha256") != MANAGED_SCHEMA_HASH
        or _sha256_file(managed_schema_path) != MANAGED_SCHEMA_HASH
    ):
        raise EngineBuildError("managed provider schema does not match the pinned protocol contract")
    build = manifest.get("build", {})
    if not isinstance(build, dict) or set(build) != {
        "contract_version",
        "source_date_epoch",
        "source_hash_algorithm",
        "source_sha256",
        "tier_1_targets",
        "experimental_targets",
    }:
        raise EngineBuildError("vendor build-contract fields are incompatible")
    if build.get("contract_version") != 1:
        raise EngineBuildError("unsupported engine build contract version")
    if build.get("source_hash_algorithm") != "openclank-tree-sha256-v1":
        raise EngineBuildError("unsupported engine source hash algorithm")
    if set(build.get("tier_1_targets", [])) != TIER_1_TARGETS:
        raise EngineBuildError("vendor manifest Tier 1 targets do not match the host contract")
    if len(build.get("tier_1_targets", [])) != len(TIER_1_TARGETS):
        raise EngineBuildError("vendor manifest Tier 1 targets contain duplicates")
    if set(build.get("experimental_targets", [])) != EXPERIMENTAL_TARGETS:
        raise EngineBuildError("vendor manifest experimental targets do not match the host contract")
    if len(build.get("experimental_targets", [])) != len(EXPERIMENTAL_TARGETS):
        raise EngineBuildError("vendor manifest experimental targets contain duplicates")
    if not isinstance(build.get("source_date_epoch"), int) or build["source_date_epoch"] < 1:
        raise EngineBuildError("vendor manifest has an invalid reproducible-build epoch")
    pinned_source = str(build.get("source_sha256", ""))
    if not _SHA256_RE.fullmatch(pinned_source):
        raise EngineBuildError("vendor manifest has an invalid source fingerprint")
    if source_fingerprint(vendor_root) != pinned_source:
        raise EngineBuildError("vendored engine source does not match the pinned custom-source fingerprint")
    return manifest


def source_fingerprint(vendor_root: Path | str) -> str:
    """Hash admitted portable source while excluding declared incidental state.

    The contract intentionally does not consult Git: release archives and
    container builds may not contain the nested repository metadata. Directory
    and file exclusions above mirror only explicit generated, dependency,
    editor, test-output, and runtime categories; everything else is admitted.
    """

    root = Path(vendor_root).resolve()
    if not root.is_dir():
        raise EngineBuildError(f"vendored engine source is missing: {root}")
    digest = hashlib.sha256()
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        symlink_directories: list[str] = []
        admitted_directories: list[str] = []
        for name in sorted(directories):
            path = current_path / name
            relative = path.relative_to(root)
            if _source_directory_excluded(relative):
                continue
            if path.is_symlink():
                symlink_directories.append(name)
            else:
                admitted_directories.append(name)
        directories[:] = admitted_directories
        for name in sorted([*filenames, *symlink_directories]):
            path = current_path / name
            relative = path.relative_to(root)
            if _source_file_excluded(relative):
                continue
            relative_text = relative.as_posix()
            digest.update(relative_text.encode("utf-8"))
            digest.update(b"\0")
            if path.is_symlink():
                digest.update(b"symlink\0")
                target = os.readlink(path)
                # Windows reports real link targets with native separators;
                # preserve the portable target spelling without changing type.
                if sys.platform == "win32":
                    target = target.replace("\\", "/")
                digest.update(target.encode("utf-8"))
            elif path.is_file():
                digest.update(b"file\0")
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()


def _read_pointer(install_root: Path) -> tuple[Path, Path, dict[str, Any]]:
    pointer_path = install_root / "current.json"
    pointer = _load_json(pointer_path, "engine current pointer")
    if pointer.get("schema_version") != CURRENT_POINTER_SCHEMA_VERSION:
        raise EngineBuildError("unsupported engine current pointer version")
    artifact = _safe_child(install_root, str(pointer.get("artifact", "")), "engine artifact")
    provenance = artifact / "provenance.json"
    expected = str(pointer.get("provenance_sha256", ""))
    if not _SHA256_RE.fullmatch(expected) or _sha256_file(provenance) != expected:
        raise EngineBuildError("engine provenance does not match current.json")
    return artifact, provenance, pointer


def resolve_installed_binary(install_root: Path | str | None = None) -> Path:
    root = Path(install_root or default_install_root()).resolve()
    artifact, provenance_path, _ = _read_pointer(root)
    provenance = _load_json(provenance_path, "engine provenance")
    name = str(provenance.get("binary", {}).get("name", ""))
    if name not in {"openclank-engine", "openclank-engine.exe"}:
        raise EngineBuildError("engine provenance contains an invalid binary name")
    binary = _safe_child(artifact, name, "engine binary")
    if not binary.is_file():
        raise EngineBuildError(f"managed engine binary is missing: {binary}")
    return binary


def _validate_provenance_shape(provenance: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    v2 = type(provenance.get('schema_version')) is int and provenance.get('schema_version') == 2
    expected_top = {
        "schema_version",
        "product",
        "artifact_kind",
        "build_contract_version",
        "open_clank_version",
        "engine_version",
        "target",
        "support_tier",
        "bun_version",
        "binary",
        "inputs",
        "protocols",
        "managed_schema",
        "source",
    }
    if v2:
        expected_top.add('native_artifacts')
    if set(provenance) != expected_top:
        errors.append("provenance top-level fields are incompatible")
    expected_constants = {
        "schema_version": 2 if v2 else PROVENANCE_SCHEMA_VERSION,
        "product": "Open Clank",
        "artifact_kind": "managed-model-engine",
        "build_contract_version": 1,
        "bun_version": "1.4.0",
    }
    for field, expected in expected_constants.items():
        if provenance.get(field) != expected:
            errors.append(f"provenance {field} must be {expected!r}")
    for field in ("open_clank_version", "engine_version", "target", "support_tier"):
        if not isinstance(provenance.get(field), str) or not provenance[field]:
            errors.append(f"provenance {field} is missing")
    if not _VERSION_RE.fullmatch(str(provenance.get("open_clank_version", ""))):
        errors.append("provenance Open Clank version is invalid")
    if provenance.get("engine_version") != provenance.get("open_clank_version"):
        errors.append("provenance engine version does not match Open Clank")
    target = provenance.get("target")
    if (
        not _TARGET_RE.fullmatch(str(target or ""))
        or target not in TIER_1_TARGETS | EXPERIMENTAL_TARGETS
    ):
        errors.append("provenance target is invalid")
    expected_tier = "tier-1" if target in TIER_1_TARGETS else "experimental"
    if provenance.get("support_tier") != expected_tier:
        errors.append("provenance support tier does not match its target")
    binary = provenance.get("binary")
    if not isinstance(binary, dict):
        errors.append("provenance binary is missing")
    else:
        if set(binary) != {"name", "sha256", "size"}:
            errors.append("provenance binary fields are incompatible")
        if binary.get("name") not in {"openclank-engine", "openclank-engine.exe"}:
            errors.append("provenance binary name is invalid")
        if not _SHA256_RE.fullmatch(str(binary.get("sha256", ""))):
            errors.append("provenance binary checksum is invalid")
        if not isinstance(binary.get("size"), int) or binary.get("size", 0) < 1:
            errors.append("provenance binary size is invalid")
    inputs = provenance.get("inputs")
    if not isinstance(inputs, dict):
        errors.append("provenance inputs are missing")
    else:
        expected_inputs = {
            "vendor_manifest_sha256",
            "source_sha256",
            "bun_lock_sha256",
            "model_catalog_sha256",
        }
        if v2:
            expected_inputs |= {'rust_toolchain_sha256','cargo_lock_sha256','gcf_source_sha256'}
        if set(inputs) != expected_inputs:
            errors.append("provenance input fields are incompatible")
        for field in expected_inputs:
            if not _SHA256_RE.fullmatch(str(inputs.get(field, ""))):
                errors.append(f"provenance input {field} is invalid")
    if provenance.get("protocols") != SUPPORTED_PROTOCOLS:
        errors.append("provenance protocol versions are incompatible")
    managed_schema = provenance.get("managed_schema")
    if not isinstance(managed_schema, dict) or managed_schema != {
        "id": MANAGED_SCHEMA_ID,
        "version": MANAGED_SCHEMA_VERSION,
        "sha256": MANAGED_SCHEMA_HASH,
    }:
        errors.append("provenance managed schema is incompatible")
    source = provenance.get("source")
    if not isinstance(source, dict):
        errors.append("provenance source is missing")
    else:
        expected_source = {
            "upstream_repository",
            "upstream_commit",
            "open_clank_import_commit",
            "open_clank_import_tree",
            "hash_algorithm",
        }
        if set(source) != expected_source:
            errors.append("provenance source fields are incompatible")
        if source.get("upstream_repository") != "https://github.com/XiaomiMiMo/mimo-code.git":
            errors.append("provenance upstream repository is incompatible")
        for field in ("upstream_commit", "open_clank_import_commit", "open_clank_import_tree"):
            if not _GIT_SHA_RE.fullmatch(str(source.get(field, ""))):
                errors.append(f"provenance source {field} is invalid")
        if source.get("hash_algorithm") != "openclank-tree-sha256-v1":
            errors.append("provenance source hash algorithm is incompatible")
    if v2:
        native=provenance.get('native_artifacts')
        if type(native) is not list or len(native)>1:
            errors.append('native artifact list is incompatible')
        elif native:
            leaf=native[0]
            if (type(leaf) is not dict or set(leaf)!={'path','target','napi','abi','codecVersion','sha256','size'}
                or leaf.get('path')!='native/openclank-gcf.node' or leaf.get('target')!=target
                or type(leaf.get('napi')) is not int or leaf.get('napi')!=6
                or type(leaf.get('abi')) is not int or leaf.get('abi')!=1
                or type(leaf.get('size')) is not int or not 0<leaf.get('size',0)<=268435456
                or not _SHA256_RE.fullmatch(str(leaf.get('sha256','')))
                or type(leaf.get('codecVersion')) is not str or not _VERSION_RE.fullmatch(leaf.get('codecVersion',''))):
                errors.append('native artifact metadata is incompatible')
    return errors


def _smoke_environment(temp_root: Path) -> dict[str, str]:
    allowed = {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "NODE_EXTRA_CA_CERTS",
        "PATH",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment.update({
        "HOME": str(temp_root),
        "NO_COLOR": "1",
        "XDG_CACHE_HOME": str(temp_root / "cache"),
        "XDG_CONFIG_HOME": str(temp_root / "config"),
        "XDG_DATA_HOME": str(temp_root / "data"),
        "XDG_STATE_HOME": str(temp_root / "state"),
    })
    return environment


def _run_acp_smoke(binary: Path, version: str, timeout: float) -> str | None:
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": SUPPORTED_PROTOCOLS["managed_acp"],
            "clientCapabilities": {
                "_meta": {"openclankManaged": client_capability_offer()},
            },
            "clientInfo": {"name": "openclank-engine-verifier", "version": version},
        },
    }
    with tempfile.TemporaryDirectory(prefix="openclank-engine-smoke-") as temporary:
        root = Path(temporary)
        environment = _smoke_environment(root)
        # This is a mode switch, not provider state. It forces the private
        # child down the only supported Open Clank handshake and prevents a
        # legacy standalone ACP response from satisfying installation checks.
        environment["OPEN_CLANK_MANAGED"] = "1"
        try:
            result = subprocess.run(
                [str(binary), "acp", "--cwd", str(root)],
                input=json.dumps(request, separators=(",", ":")) + "\n",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=root,
                env=environment,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"ACP initialization smoke test failed: {exc}"
    for line in result.stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") != 1:
            continue
        if "error" in message:
            return "ACP initialization returned an error"
        response = message.get("result", {})
        if response.get("protocolVersion") != SUPPORTED_PROTOCOLS["managed_acp"]:
            return "ACP initialization negotiated an incompatible protocol"
        return _managed_capability_error(response)
    detail = result.stderr.strip().splitlines()[-1][:300] if result.stderr.strip() else "no initialize response"
    return f"ACP initialization smoke test failed: {detail}"


def _managed_capability_error(response: Mapping[str, Any]) -> str | None:
    try:
        validate_initialize_result(dict(response))
    except ManagedProtocolError as exc:
        return f"ACP initialization contract validation failed: {exc}"
    return None


def verify_install(
    install_root: Path | str | None = None,
    *,
    expected_version: str | None = None,
    expected_target: str | None = None,
    expected_source_sha256: str | None = None,
    expected_vendor_manifest_sha256: str | None = None,
    run_smoke: bool = True,
    acp_smoke: bool = True,
    # A verified Bun single-file binary can take longer than 45 seconds to
    # reach main() while the host is saturated by another native build.  Keep
    # the smoke bounded, but avoid turning ordinary scheduler contention into
    # a rebuild/restart loop.
    smoke_timeout: float = 120.0,
) -> EngineVerification:
    root = Path(install_root or default_install_root()).resolve()
    errors: list[str] = []
    binary: Path | None = None
    provenance_path: Path | None = None
    provenance: dict[str, Any] = {}
    try:
        artifact, provenance_path, _ = _read_pointer(root)
        provenance = _load_json(provenance_path, "engine provenance")
        errors.extend(_validate_provenance_shape(provenance))
        name = str(provenance.get("binary", {}).get("name", ""))
        if name in {"openclank-engine", "openclank-engine.exe"}:
            binary = _safe_child(artifact, name, "engine binary")
    except (EngineBuildError, OSError) as exc:
        errors.append(str(exc))

    version = provenance.get("open_clank_version") if provenance else None
    target = provenance.get("target") if provenance else None
    source_sha256 = provenance.get("inputs", {}).get("source_sha256") if provenance else None
    if expected_version is not None and version != expected_version:
        errors.append(f"engine version {version!r} does not match Open Clank {expected_version!r}")
    target_expectation = expected_target
    if target_expectation is None:
        try:
            if not _compatible_host_target(str(target)):
                target_expectation = current_target()
        except EngineBuildError as exc:
            errors.append(str(exc))
    if target_expectation is not None and target != target_expectation:
        errors.append(f"engine target {target!r} does not match this host {target_expectation!r}")
    if expected_source_sha256 is not None and source_sha256 != expected_source_sha256:
        errors.append("installed engine source fingerprint is stale")
    vendor_manifest_sha256 = (
        provenance.get("inputs", {}).get("vendor_manifest_sha256")
        if provenance
        else None
    )
    if (
        expected_vendor_manifest_sha256 is not None
        and vendor_manifest_sha256 != expected_vendor_manifest_sha256
    ):
        errors.append("installed engine vendor manifest is stale")

    if binary is None or not binary.is_file():
        errors.append("managed engine binary is missing")
    elif provenance:
        expected_binary_hash = provenance.get("binary", {}).get("sha256")
        expected_binary_size = provenance.get("binary", {}).get("size")
        if _sha256_file(binary) != expected_binary_hash:
            errors.append("managed engine binary checksum mismatch")
        if binary.stat().st_size != expected_binary_size:
            errors.append("managed engine binary size mismatch")
        try:
            _verify_pe_target(binary, str(target))
        except (EngineBuildError, OSError) as exc:
            errors.append(str(exc))
        if os.name != "nt" and not os.access(binary, os.X_OK):
            errors.append("managed engine binary is not executable")

    addon = None
    if not errors and provenance.get('schema_version') == 2 and provenance.get('native_artifacts'):
        leaf=provenance['native_artifacts'][0]
        try:
            native_dir=artifact / 'native'
            path=native_dir / 'openclank-gcf.node'
            if native_dir.is_symlink() or path.is_symlink() or not path.is_file() or path.resolve().parent != native_dir.resolve():
                raise EngineBuildError('native artifact path is invalid')
            if path.stat().st_size != leaf['size'] or _sha256_file(path) != leaf['sha256']:
                raise EngineBuildError('native artifact integrity mismatch')
            addon={'version':1,'absolutePath':str(path.resolve()),'sha256':leaf['sha256'],'size':leaf['size'],
                   'target':leaf['target'],'napi':6,'abi':1,'codecVersion':leaf['codecVersion']}
        except (EngineBuildError,OSError) as exc:
            errors.append(str(exc))

    if not errors and run_smoke and binary is not None:
        try:
            result = subprocess.run(
                [str(binary), "--version"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=smoke_timeout,
                check=False,
                env=_smoke_environment(Path(tempfile.gettempdir())),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"engine version smoke test failed: {exc}")
        else:
            output = f"{result.stdout}\n{result.stderr}"
            if result.returncode != 0 or output.strip() != str(provenance.get("engine_version", "")):
                errors.append("engine version smoke test returned an unexpected result")
    if not errors and acp_smoke and binary is not None:
        acp_error = _run_acp_smoke(binary, str(version), smoke_timeout)
        if acp_error:
            errors.append(acp_error)

    return EngineVerification(
        ok=not errors,
        binary=binary,
        provenance=provenance_path,
        version=str(version) if version is not None else None,
        target=str(target) if target is not None else None,
        source_sha256=str(source_sha256) if source_sha256 is not None else None,
        errors=tuple(dict.fromkeys(errors)),
        addon=addon if not errors else None,
        declared_addon=provenance.get('schema_version') == 2,
    )


def _bun_version(executable: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _download_file(url: str, destination: Path, expected_sha256: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    request = urllib.request.Request(url, headers={"User-Agent": "Open-Clank-engine-builder/1"})
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 300 * 1024 * 1024:
                    raise EngineBuildError("Bun toolchain archive exceeds the safety limit")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != expected_sha256:
            raise EngineBuildError("downloaded Bun archive checksum mismatch")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _install_bun_from_archive(archive: Path, destination: Path) -> None:
    executable_name = "bun.exe" if os.name == "nt" else "bun"
    with zipfile.ZipFile(archive) as bundle:
        candidates = [
            info
            for info in bundle.infolist()
            if not info.is_dir()
            and Path(info.filename).name == executable_name
            and not Path(info.filename).is_absolute()
            and ".." not in Path(info.filename).parts
        ]
        if len(candidates) != 1:
            raise EngineBuildError("Bun archive does not contain exactly one expected executable")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with bundle.open(candidates[0]) as source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            if os.name != "nt":
                temporary.chmod(temporary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def _ensure_bun(
    repo_root: Path,
    manifest: Mapping[str, Any],
    requested: Path | str,
    *,
    allow_download: bool,
) -> Path:
    requested_path = Path(requested)
    discovered = requested_path if requested_path.parent != Path(".") else shutil.which(str(requested_path))
    if discovered is not None:
        executable = Path(discovered).resolve()
        if _bun_version(executable) == manifest["toolchain"]["bun"]["version"]:
            return executable
        if not allow_download:
            raise EngineBuildError("Bun is present but does not match the pinned version 1.4.0")
    elif not allow_download:
        raise EngineBuildError("Bun 1.4.0 is required but was not found")

    target = current_target()
    contract = manifest["toolchain"]["bun"].get("downloads", {}).get(target)
    if not isinstance(contract, dict):
        raise EngineBuildError(f"no pinned Bun toolchain is available for {target}")
    cache = repo_root / "cache" / "engine-build" / "bun" / manifest["toolchain"]["bun"]["version"] / target
    archive = cache / "bun.zip"
    executable = cache / ("bun.exe" if os.name == "nt" else "bun")
    if not archive.is_file() or _sha256_file(archive) != contract.get("sha256"):
        _download_file(str(contract.get("url", "")), archive, str(contract.get("sha256", "")))
    if _bun_version(executable) != manifest["toolchain"]["bun"]["version"]:
        _install_bun_from_archive(archive, executable)
    if _bun_version(executable) != manifest["toolchain"]["bun"]["version"]:
        raise EngineBuildError("the verified Bun toolchain failed its version check")
    return executable


@contextlib.contextmanager
def _build_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.tell() == handle.seek(0, os.SEEK_END):
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            deadline = time.monotonic() + 30 * 60
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise EngineBuildError("timed out waiting for the engine build lock") from exc
                    time.sleep(0.1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            with contextlib.suppress(OSError):
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _read_app_version(repo_root: Path) -> str:
    constants = (repo_root / "src" / "constants.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', constants, re.MULTILINE)
    if not match:
        raise EngineBuildError("could not determine Open Clank APP_VERSION")
    return match.group(1)


def _sign_darwin_build_binary(binary: Path, target: str) -> None:
    """Finish the native compiled artifact before its provenance is sealed."""
    if not target.startswith("darwin-"):
        return
    if sys.platform != "darwin":
        raise EngineBuildError("Darwin engine signing requires a native macOS build host")
    commands = (
        ("signing", ["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", str(binary)]),
        ("signature verification", ["/usr/bin/codesign", "--verify", "--strict", str(binary)]),
    )
    for stage, command in commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EngineBuildError(f"Darwin engine {stage} failed: {type(exc).__name__}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "no codesign diagnostic").strip()
            detail = detail.replace(str(binary), binary.name)[:1000]
            raise EngineBuildError(f"Darwin engine {stage} failed (code {result.returncode}): {detail}")


def _provenance_payload(
    *,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    version: str,
    target: str,
    source_sha256: str,
    binary: Path,
    addon: Path | None = None,
    gcf_root: Path | None = None,
) -> dict[str, Any]:
    vendor = manifest["vendor"]
    support_tier = "tier-1" if target in manifest["build"]["tier_1_targets"] else "experimental"
    payload = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "product": "Open Clank",
        "artifact_kind": "managed-model-engine",
        "build_contract_version": manifest["build"]["contract_version"],
        "open_clank_version": version,
        "engine_version": version,
        "target": target,
        "support_tier": support_tier,
        "bun_version": manifest["toolchain"]["bun"]["version"],
        "binary": {
            "name": "openclank-engine.exe" if target.startswith("windows-") else "openclank-engine",
            "sha256": _sha256_file(binary),
            "size": binary.stat().st_size,
        },
        "inputs": {
            "vendor_manifest_sha256": manifest_sha256,
            "source_sha256": source_sha256,
            "bun_lock_sha256": manifest["inputs"]["lockfile"]["sha256"],
            "model_catalog_sha256": manifest["inputs"]["model_catalog"]["sha256"],
        },
        "protocols": dict(manifest["protocols"]),
        "managed_schema": {
            "id": manifest["managed_schema"]["id"],
            "version": manifest["managed_schema"]["version"],
            "sha256": manifest["managed_schema"]["sha256"],
        },
        "source": {
            "upstream_repository": vendor["upstream_repository"],
            "upstream_commit": vendor["upstream_commit"],
            "open_clank_import_commit": vendor["open_clank_import_commit"],
            "open_clank_import_tree": vendor["open_clank_import_tree"],
            "hash_algorithm": manifest["build"]["source_hash_algorithm"],
        },
    }


    if addon is not None:
        if gcf_root is None:
            raise EngineBuildError("GCF source root is required")
        try:
            package = tomllib.loads((gcf_root / "Cargo.toml").read_text("utf-8"))
            codec_version = package["package"]["version"]
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise EngineBuildError("invalid GCF package version") from exc
        if not isinstance(codec_version, str) or not _VERSION_RE.fullmatch(codec_version):
            raise EngineBuildError("invalid GCF codec version")
        payload["schema_version"] = 2
        payload["inputs"].update({
            "rust_toolchain_sha256": _sha256_file(gcf_root / "rust-toolchain.toml"),
            "cargo_lock_sha256": _sha256_file(gcf_root / "Cargo.lock"),
            "gcf_source_sha256": source_fingerprint(gcf_root),
        })
        payload["native_artifacts"] = [{
            "path": "native/openclank-gcf.node", "target": target,
            "napi": 6, "abi": 1, "codecVersion": codec_version,
            "sha256": _sha256_file(addon), "size": addon.stat().st_size,
        }]
    return payload


def _build_gcf_addon(gcf_root: Path, target_dir: Path, cargo: Path, rustc: Path, cargo_home: Path) -> Path:
    """Build only from an explicitly supplied pinned toolchain and offline cache."""
    for executable, expected in ((cargo, "cargo 1.99.1"), (rustc, "rustc 1.99.1")):
        if not executable.is_file():
            raise EngineBuildError("explicit GCF toolchain executable is unavailable")
        result = subprocess.run([str(executable), "--version"], capture_output=True, text=True, check=True)
        if not result.stdout.strip().startswith(expected + " "):
            raise EngineBuildError("GCF requires the pinned Rust 1.99.1 toolchain")
    if not cargo_home.is_dir():
        raise EngineBuildError("explicit GCF Cargo cache is unavailable")
    try:
        pin = tomllib.loads((gcf_root / "rust-toolchain.toml").read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise EngineBuildError("invalid GCF toolchain pin") from exc
    if pin.get("toolchain", {}).get("channel") != "1.99.1":
        raise EngineBuildError("GCF source toolchain pin is incompatible")
    env = os.environ.copy()
    for key in list(env):
        if key in {"RUSTFLAGS", "RUSTDOCFLAGS", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTUP_TOOLCHAIN", "CARGO_ENCODED_RUSTFLAGS", "CARGO_BUILD_TARGET", "CARGO_BUILD_RUSTC", "CARGO_BUILD_RUSTC_WRAPPER", "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER", "CARGO_TARGET_DIR"} or key.startswith("CARGO_TARGET_"):
            env.pop(key, None)
    env.update({"CARGO_HOME": str(cargo_home), "RUSTC": str(rustc)})
    subprocess.run([str(cargo), "build", "--locked", "--offline", "--release", "--features", "node-addon", "--manifest-path", str(gcf_root / "Cargo.toml"), "--target-dir", str(target_dir)], cwd=gcf_root, env=env, check=True)
    addon = target_dir / "release" / "libopenclank_gcf.so"
    if addon.is_symlink() or not addon.is_file() or addon.stat().st_size == 0:
        raise EngineBuildError("GCF build did not produce a regular nonempty addon")
    return addon


def _atomic_write(path: Path, payload: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    """Durably publish a rename on filesystems that support directory fsync."""

    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _install_artifact(
    *,
    install_root: Path,
    built_binary: Path,
    provenance: Mapping[str, Any],
    built_addon: Path | None = None,
) -> None:
    version = str(provenance["open_clank_version"])
    target = str(provenance["target"])
    source_prefix = str(provenance["inputs"]["source_sha256"])[:16]
    if _validate_provenance_shape(provenance):
        raise EngineBuildError('invalid artifact provenance')
    leaves=provenance.get('native_artifacts', [])
    if bool(leaves) != (built_addon is not None):
        raise EngineBuildError('native artifact staging mismatch')
    if provenance.get('schema_version') == 2:
        source_prefix += '-' + hashlib.sha256(_canonical_json_bytes(provenance)).hexdigest()[:16]
    final = install_root / version / target / source_prefix
    staging_root = Path(tempfile.mkdtemp(prefix=".staging-", dir=install_root))
    staging = staging_root / "artifact"
    staging.mkdir()
    binary_name = str(provenance["binary"]["name"])
    staged_binary = staging / binary_name
    try:
        shutil.copyfile(built_binary, staged_binary)
        staged_binary.chmod(0o755)
        # Windows fsync uses CRT _commit / FlushFileBuffers, which requires
        # write access. Reopen the private copy without truncating its bytes.
        with staged_binary.open("r+b") as handle:
            os.fsync(handle.fileno())
        if built_addon is not None:
            if built_addon.is_symlink() or not built_addon.is_file():
                raise EngineBuildError('native artifact source is invalid')
            native=staging / 'native'; native.mkdir()
            staged_addon=native / 'openclank-gcf.node'
            shutil.copyfile(built_addon,staged_addon)
            if staged_addon.stat().st_size != leaves[0]['size'] or _sha256_file(staged_addon) != leaves[0]['sha256']:
                raise EngineBuildError('native artifact staging integrity mismatch')
            with staged_addon.open('r+b') as handle: os.fsync(handle.fileno())
            _fsync_directory(native)
        _atomic_write(staging / "provenance.json", _canonical_json_bytes(provenance))
        if final.exists():
            existing = final / "provenance.json"
            if not existing.is_file() or existing.read_bytes() != (staging / "provenance.json").read_bytes():
                raise EngineBuildError("non-reproducible engine artifact for the same source fingerprint")
            existing_binary=final / binary_name
            if (existing_binary.is_symlink() or not existing_binary.is_file()
                    or existing_binary.stat().st_size != provenance['binary']['size']
                    or _sha256_file(existing_binary) != provenance['binary']['sha256']):
                raise EngineBuildError('existing engine artifact integrity mismatch')
            if leaves:
                native=final / 'native'; leaf=native / 'openclank-gcf.node'
                if (native.is_symlink() or leaf.is_symlink() or not leaf.is_file()
                        or leaf.stat().st_size != leaves[0]['size'] or _sha256_file(leaf) != leaves[0]['sha256']):
                    raise EngineBuildError('existing native artifact integrity mismatch')
        else:
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, final)
            _fsync_directory(final.parent)
        relative = final.relative_to(install_root).as_posix()
        pointer = {
            "schema_version": CURRENT_POINTER_SCHEMA_VERSION,
            "artifact": relative,
            "provenance_sha256": _sha256_file(final / "provenance.json"),
        }
        _atomic_write(install_root / "current.json", _canonical_json_bytes(pointer))
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def build_current(
    repo_root: Path | str | None = None,
    install_root: Path | str | None = None,
    *,
    version: str | None = None,
    bun_executable: Path | str = "bun",
    allow_bun_download: bool = True,
    install_dependencies: bool = True,
    force: bool = False,
    acp_smoke: bool = True,
    target: str | None = None,
    include_gcf: bool = False,
    gcf_cargo: Path | str | None = None,
    gcf_rustc: Path | str | None = None,
    gcf_cargo_home: Path | str | None = None,
) -> EngineVerification:
    if include_gcf and (install_root is None or not all((gcf_cargo, gcf_rustc, gcf_cargo_home))):
        raise EngineBuildError("GCF build requires an explicit install root, Cargo, rustc and private Cargo cache")
    if not include_gcf and any((gcf_cargo, gcf_rustc, gcf_cargo_home)):
        raise EngineBuildError("GCF toolchain options require include_gcf")
    root = Path(repo_root or REPO_ROOT).resolve()
    destination = Path(install_root or default_install_root(root)).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    manifest = load_vendor_manifest(root)
    manifest_path = _vendor_root(root) / VENDOR_MANIFEST_NAME
    manifest_sha256 = _sha256_file(manifest_path)
    vendor_root = _vendor_root(root)
    source_sha256 = source_fingerprint(vendor_root)
    target = target or current_target()
    if include_gcf and (target != "linux-x64" or current_target() != "linux-x64"):
        raise EngineBuildError("optional GCF build is supported only on native linux-x64")
    if target not in TIER_1_TARGETS | EXPERIMENTAL_TARGETS:
        raise EngineBuildError(f"unsupported explicit engine target: {target}")
    if target.split("-", 1)[0] != current_target().split("-", 1)[0]:
        raise EngineBuildError("engine version/ACP verification requires the target operating system")
    release_version = version or _read_app_version(root)
    if not _VERSION_RE.fullmatch(release_version):
        raise EngineBuildError("Open Clank version is unsafe for an engine artifact path")

    if not force:
        existing = verify_install(
            destination,
            expected_version=release_version,
            expected_target=target,
            expected_source_sha256=source_sha256,
            expected_vendor_manifest_sha256=manifest_sha256,
            run_smoke=True,
            acp_smoke=acp_smoke,
        )
        if existing.ok and (not include_gcf or existing.addon is not None):
            return existing

    # Keep the destination lock beside, rather than inside, the install root so
    # private lock state is never mistaken for part of a release payload.
    destination_lock = destination.parent / f".{destination.name}.build.lock"
    with _build_lock(root / "cache" / "engine-build" / ".source.lock"), _build_lock(
        destination_lock
    ):
        if not force:
            existing = verify_install(
                destination,
                expected_version=release_version,
                expected_target=target,
                expected_source_sha256=source_sha256,
                expected_vendor_manifest_sha256=manifest_sha256,
                run_smoke=True,
                acp_smoke=acp_smoke,
            )
            if existing.ok and (not include_gcf or existing.addon is not None):
                return existing

        bun = _ensure_bun(root, manifest, bun_executable, allow_download=allow_bun_download)
        # Dependency lifecycle wrappers invoke Bun by name even when the
        # parent build uses its absolute admitted executable. Keep those
        # children on the same pinned toolchain without changing caller PATH.
        environment = os.environ.copy()
        inherited_path = environment.get("PATH", "")
        environment["PATH"] = str(bun.resolve().parent) + (os.pathsep + inherited_path if inherited_path else "")
        lockfile = _safe_child(vendor_root, manifest["inputs"]["lockfile"]["path"], "lockfile")
        lock_hash = _sha256_file(lockfile)
        if install_dependencies:
            subprocess.run([str(bun), "ci"], cwd=vendor_root, env=environment, check=True)
            if _sha256_file(lockfile) != lock_hash:
                raise EngineBuildError("bun ci modified the pinned lockfile")

        environment.update({
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "OPENCLANK_CHANNEL": "openclank",
            "OPENCLANK_COMMIT_SHA": source_sha256[:12],
            "OPENCLANK_VERSION": release_version,
            "SOURCE_DATE_EPOCH": str(manifest["build"]["source_date_epoch"]),
            "TZ": "UTC",
        })
        environment.pop("OPENCLANK_RELEASE", None)
        if os.name == "nt":
            # Start compilation with generated catalog files already present.
            # Resolving models-schema during in-process generation can leave
            # Windows Bun's compiler unaware of the newly created sibling.
            subprocess.run(
                [str(bun), "run", "--cwd", "packages/cli", "script/generate.ts"],
                cwd=vendor_root,
                env=environment,
                check=True,
            )
            provider_root = vendor_root / "packages" / "cli" / "src" / "provider"
            for name in ("models-snapshot.js", "models-snapshot.d.ts"):
                generated = provider_root / name
                if generated.is_symlink() or not generated.is_file() or generated.stat().st_size == 0:
                    raise EngineBuildError(f"model catalog generation did not produce a regular nonempty {name}")
        subprocess.run(
            [
                str(bun),
                "run",
                "--cwd",
                "packages/cli",
                "script/build.ts",
                "--single",
                "--target",
                target,
                "--skip-install",
            ],
            cwd=vendor_root,
            env=environment,
            check=True,
        )
        output = vendor_root / "packages" / "cli" / "dist" / f"openclank-engine-{target}" / "bin"
        built_binary = output / ("openclank-engine.exe" if target.startswith("windows-") else "openclank-engine")
        if not built_binary.is_file():
            raise EngineBuildError(f"engine build did not produce {built_binary}")
        _verify_pe_target(built_binary, target)
        _sign_darwin_build_binary(built_binary, target)
        if source_fingerprint(vendor_root) != source_sha256:
            raise EngineBuildError("vendored engine source changed during the build")

        temporary_build = tempfile.TemporaryDirectory(prefix="openclank-gcf-build-") if include_gcf else contextlib.nullcontext(None)
        with temporary_build as temporary:
            addon = None
            gcf_root = vendor_root / "packages" / "cli" / "native" / "gcf"
            if include_gcf:
                addon = _build_gcf_addon(gcf_root, Path(temporary), Path(gcf_cargo).resolve(), Path(gcf_rustc).resolve(), Path(gcf_cargo_home).resolve())
                if source_fingerprint(vendor_root) != source_sha256:
                    raise EngineBuildError("vendored engine source changed during the GCF build")
            provenance = _provenance_payload(
                manifest=manifest, manifest_sha256=manifest_sha256, version=release_version,
                target=target, source_sha256=source_sha256, binary=built_binary,
                addon=addon, gcf_root=gcf_root if addon is not None else None,
            )
            shape_errors = _validate_provenance_shape(provenance)
            if shape_errors:
                raise EngineBuildError("invalid generated provenance: " + "; ".join(shape_errors))
            _install_artifact(install_root=destination, built_binary=built_binary, provenance=provenance, built_addon=addon)

    return verify_install(
        destination,
        expected_version=release_version,
        expected_target=target,
        expected_source_sha256=source_sha256,
        expected_vendor_manifest_sha256=manifest_sha256,
        run_smoke=True,
        acp_smoke=acp_smoke,
    ).require()


def ensure_engine_ready(
    repo_root: Path | str | None = None,
    install_root: Path | str | None = None,
    *,
    version: str | None = None,
) -> EngineVerification:
    """Verify a packaged engine or build the exact vendored source before launch."""

    root = Path(repo_root or REPO_ROOT).resolve()
    destination = Path(install_root or default_install_root(root)).resolve()
    release_version = version or _read_app_version(root)
    vendor_root = _vendor_root(root)
    source_sha256: str | None = None
    manifest_sha256: str | None = None
    if vendor_root.is_dir():
        manifest = load_vendor_manifest(root)
        source_sha256 = str(manifest["build"]["source_sha256"])
        manifest_sha256 = _sha256_file(vendor_root / VENDOR_MANIFEST_NAME)
    verified = verify_install(
        destination,
        expected_version=release_version,
        expected_source_sha256=source_sha256,
        expected_vendor_manifest_sha256=manifest_sha256,
    )
    if verified.ok:
        return verified
    # A missing or damaged pointer is not evidence of a fresh install.  Only
    # an intact, shape-valid v1 provenance permits legacy engine-only repair.
    pointer = destination / 'current.json'
    if pointer.exists() or pointer.is_symlink():
        try:
            _, provenance_path, _ = _read_pointer(destination)
            recorded = _load_json(provenance_path, 'engine provenance')
            trusted_v1 = recorded.get('schema_version') == 1 and not _validate_provenance_shape(recorded)
        except (OSError, EngineBuildError, ValueError):
            trusted_v1 = False
        if not trusted_v1:
            raise EngineBuildError('installed engine authority is unavailable; use explicit engine repair')
    elif destination.exists():
        # A version/target/artifact directory with an engine leaf or provenance
        # is recognizable prior installation evidence; unrelated files are not.
        for version_dir in destination.iterdir():
            if not version_dir.is_dir() or not _VERSION_RE.fullmatch(version_dir.name):
                continue
            for target_dir in version_dir.iterdir():
                if not target_dir.is_dir() or target_dir.name not in TIER_1_TARGETS | EXPERIMENTAL_TARGETS:
                    continue
                for artifact_dir in target_dir.iterdir():
                    if artifact_dir.is_dir() and any((artifact_dir / n).exists() or (artifact_dir / n).is_symlink()
                                                     for n in ('provenance.json','openclank-engine','openclank-engine.exe')):
                        raise EngineBuildError('installed engine pointer is missing; use explicit engine repair')
    if not vendor_root.is_dir():
        return verified.require()
    return build_current(root, destination, version=release_version)


__all__ = [
    "EngineBuildError",
    "EngineVerification",
    "build_current",
    "current_target",
    "default_install_root",
    "ensure_engine_ready",
    "load_vendor_manifest",
    "resolve_installed_binary",
    "source_fingerprint",
    "verify_install",
]
