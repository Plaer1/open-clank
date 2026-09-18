#!/usr/bin/env python3
"""Build and verify the private Open Clank managed engine artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from src.openclank.engine_build import (  # noqa: E402
    EngineBuildError,
    build_current,
    current_target,
    default_install_root,
    load_vendor_manifest,
    resolve_installed_binary,
    source_fingerprint,
    verify_install,
)
from src.constants import APP_VERSION  # noqa: E402


def _verification_json(result) -> dict:
    return result.to_dict()


def _expected_contract(repo_root: Path) -> tuple[str | None, str | None]:
    vendor = repo_root / "packages" / "mimo-code"
    if not vendor.is_dir():
        return None, None
    manifest = load_vendor_manifest(repo_root)
    manifest_path = vendor / "openclank-vendor.json"
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return str(manifest["build"]["source_sha256"]), manifest_sha256


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openclank engine",
        description="Open Clank managed engine build and provenance tools",
    )
    parser.add_argument("--repo-root", type=Path, default=SCRIPT_ROOT)
    parser.add_argument("--install-root", type=Path)
    subcommands = parser.add_subparsers(dest="command", required=True)

    build = subcommands.add_parser("build", help="build, install, and verify the current host engine")
    build.add_argument("--version")
    build.add_argument("--bun", default="bun")
    build.add_argument("--no-download-bun", action="store_true")
    build.add_argument("--skip-dependencies", action="store_true")
    build.add_argument("--force", action="store_true")
    build.add_argument("--skip-acp", action="store_true")
    build.add_argument("--json", action="store_true")

    verify = subcommands.add_parser("verify", help="verify the activated artifact and provenance")
    verify.add_argument("--version")
    verify.add_argument("--target")
    verify.add_argument("--source-sha256")
    verify.add_argument("--skip-version-smoke", action="store_true")
    verify.add_argument("--skip-acp", action="store_true")
    verify.add_argument("--json", action="store_true")

    resolve = subcommands.add_parser("resolve", help="print the verified artifact's binary path")
    resolve.add_argument("--verify", action="store_true")

    contract = subcommands.add_parser("contract", help="validate pinned source inputs without building")
    contract.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    install_root = (args.install_root or default_install_root(repo_root)).resolve()
    try:
        if args.command == "contract":
            manifest = load_vendor_manifest(repo_root)
            report = {
                "ok": True,
                "target": current_target(),
                "bun_version": manifest["toolchain"]["bun"]["version"],
                "upstream_commit": manifest["vendor"]["upstream_commit"],
                "source_sha256": source_fingerprint(repo_root / "packages" / "mimo-code"),
                "model_catalog_sha256": manifest["inputs"]["model_catalog"]["sha256"],
                "managed_schema_sha256": manifest["managed_schema"]["sha256"],
                "protocols": manifest["protocols"],
            }
            print(json.dumps(report, sort_keys=True) if args.json else "\n".join(f"{k}: {v}" for k, v in report.items()))
            return 0

        if args.command == "build":
            result = build_current(
                repo_root,
                install_root,
                version=args.version,
                bun_executable=args.bun,
                allow_bun_download=not args.no_download_bun,
                install_dependencies=not args.skip_dependencies,
                force=args.force,
                acp_smoke=not args.skip_acp,
            )
            report = _verification_json(result)
            print(json.dumps(report, sort_keys=True) if args.json else f"Installed and verified: {result.binary}")
            return 0

        if args.command == "verify":
            expected_source, expected_manifest = _expected_contract(repo_root)
            result = verify_install(
                install_root,
                expected_version=args.version or APP_VERSION,
                expected_target=args.target,
                expected_source_sha256=args.source_sha256 or expected_source,
                expected_vendor_manifest_sha256=expected_manifest,
                run_smoke=not args.skip_version_smoke,
                acp_smoke=not args.skip_acp,
            )
            report = _verification_json(result)
            if args.json:
                print(json.dumps(report, sort_keys=True))
            elif result.ok:
                print(f"Verified: {result.binary}")
            else:
                print("Engine verification failed:", file=sys.stderr)
                for error in result.errors:
                    print(f"- {error}", file=sys.stderr)
            return 0 if result.ok else 1

        if args.verify:
            expected_source, expected_manifest = _expected_contract(repo_root)
            verify_install(
                install_root,
                expected_version=APP_VERSION,
                expected_source_sha256=expected_source,
                expected_vendor_manifest_sha256=expected_manifest,
            ).require()
        print(resolve_installed_binary(install_root))
        return 0
    except (EngineBuildError, OSError, subprocess.CalledProcessError) as exc:
        print(f"openclank engine: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
