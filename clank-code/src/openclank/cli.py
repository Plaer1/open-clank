"""Public ``openclank`` command and profile-aware TUI launcher."""

from __future__ import annotations

import argparse
import builtins
import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from src.openclank.cli_output import clean_text
from src.openclank.client_profiles import (
    ClientCredentialVault,
    ClientProfile,
    ProfileError,
    ProfileStore,
)
from src.openclank.server_manager import LocalServerManager, ServerManagerError
from src.openclank.tui_app import OpenClankTui
from src.openclank.tui_client import OpenClankTuiClient, TuiClientError
from src.runtime_paths import get_app_root


REPO_ROOT = Path(get_app_root()).resolve()
DEFAULT_LOCAL_URL = "http://127.0.0.1:7777"
if getattr(sys, "frozen", False) and sys.platform == "darwin":
    # The Mac entrypoint resolves/validates its explicit launch profile before
    # this import. Only initial local-profile creation follows that port;
    # existing named/selected client profiles retain their own URLs.
    DEFAULT_LOCAL_URL = "http://127.0.0.1:" + os.environ.get("APP_PORT", "7777")
RESERVED_COMMANDS = {
    "tui",
    "run",
    "profile",
    "login",
    "logout",
    "server",
    "engine",
    "hex",
    "assets",
    "help",
}


def _terminal_print(*values, **kwargs):
    """Keep server/profile text from becoming terminal control sequences."""
    builtins.print(*(clean_text(str(value)) for value in values), **kwargs)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openclank", description="Open Clank terminal client and service manager")
    parser.add_argument("--profile", dest="global_profile", help="use a named client profile")
    parser.add_argument("--version", action="store_true")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run one prompt in an existing chat")
    run.add_argument("--profile")
    run.add_argument("--session", required=True)
    run.add_argument("--prompt")
    run.add_argument("--quiet", "-quiet", action="store_true")
    run.add_argument("--format", choices=("text", "json", "jsonl"), default="text")

    tui = sub.add_parser("tui", help="open the Open Clank terminal interface")
    tui.add_argument("--profile")

    profile = sub.add_parser("profile", help="manage local and remote server profiles")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)
    profile_sub.add_parser("list")
    add = profile_sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("url")
    add.add_argument("--auto-start", action="store_true")
    add.add_argument("--use", action="store_true")
    use = profile_sub.add_parser("use")
    use.add_argument("name")
    remove = profile_sub.add_parser("remove")
    remove.add_argument("name")

    login = sub.add_parser("login", help="authorize this terminal through the browser")
    login.add_argument("--profile")
    login.add_argument("--no-browser", action="store_true")
    login.add_argument("--label", default="Open Clank TUI")
    login.add_argument("--questions", action="store_true", help="request permission to answer questions")
    login.add_argument("--approvals", action="store_true", help="request permission to approve actions once or reject them")

    logout = sub.add_parser("logout", help="revoke the current terminal credential")
    logout.add_argument("--profile")

    server = sub.add_parser("server", help="manage the complete local Open Clank service")
    server.add_argument("action", choices=("start", "stop", "status"))
    server.add_argument("--profile")
    server.add_argument("--open-browser", action="store_true", help="start the local app and open its authenticated browser interface")

    engine = sub.add_parser("engine", help="build and verify the private managed engine")
    engine.add_argument("engine_args", nargs=argparse.REMAINDER)

    hexes = sub.add_parser(
        "hex",
        help="inspect and enforce the Open Clank project contract",
        add_help=False,
    )
    hexes.add_argument("hex_args", nargs=argparse.REMAINDER)

    assets = sub.add_parser("assets", help="assemble or verify the complete offline artwork")
    assets.add_argument("action", choices=("assemble", "verify"))
    assets.add_argument("--parts", type=Path, help="directory containing the five release parts and their manifest")

    return parser


def _run_once(args, store, vault) -> int:
    from src.openclank.cli_run import run_one_shot

    try:
        piped = "" if sys.stdin.isatty() else sys.stdin.read(200001)
        parts = [part for part in (args.prompt, piped) if part]
        prompt = "\n".join(parts)
        if not prompt.strip() or len(prompt) > 200000:
            _terminal_print("openclank: provide a nonempty prompt of at most 200000 characters", file=sys.stderr)
            return 2
        profile = store.get(args.profile or args.global_profile or None)
        token = vault.get(profile.name)
        if not token:
            _terminal_print("openclank: profile is not logged in; use openclank login", file=sys.stderr)
            return 3
        with OpenClankTuiClient(profile, token=token) as client:
            bootstrap = client.bootstrap()
            client.messages(args.session, limit=1)
            owner = bootstrap.get("principal", {}).get("owner")
            return run_one_shot(client, args.session, prompt, args.format, sys.stdout, sys.stderr, owner)
    except KeyboardInterrupt:
        _terminal_print("openclank: interrupted before a run identity was received; outcome unknown", file=sys.stderr)
        return 6
    except ProfileError as exc:
        _terminal_print(f"openclank: {exc}", file=sys.stderr)
        return 2
    except TuiClientError as exc:
        _terminal_print(f"openclank: {exc}", file=sys.stderr)
        return 3 if exc.status_code in (401, 403) else 2 if exc.status_code == 404 else 6


def _run_assets(args) -> int:
    from scripts.emoji_asset_bundle import BundleError, assemble, expected_manifest, runtime_asset_paths, verify

    try:
        pack, manifest = runtime_asset_paths()
        expected = expected_manifest(manifest)
        if args.action == "assemble":
            if args.parts is None:
                raise ProfileError("assets assemble requires --parts")
            assemble(args.parts, pack, expected)
        else:
            verify(pack, expected)
    except (BundleError, OSError, ValueError, KeyError) as exc:
        raise ProfileError("Offline artwork verification or assembly failed; existing installed artwork was preserved") from exc
    _terminal_print("Complete offline artwork verified.")
    return 0


def _version() -> str:
    from core.constants import APP_VERSION

    return str(APP_VERSION)


def _ensure_default_profile(store: ProfileStore) -> ClientProfile:
    profiles = store.list()
    if not profiles:
        default = ClientProfile.create("local", DEFAULT_LOCAL_URL, auto_start=True)
        store.put(default, make_active=True)
        return default
    return store.get()


def _profile_for(args, store: ProfileStore) -> ClientProfile:
    name = getattr(args, "profile", None) or getattr(args, "global_profile", None)
    if name:
        return store.get(name)
    return _ensure_default_profile(store)


def _connected_client(
    profile: ClientProfile,
    vault: ClientCredentialVault,
    *,
    auto_start: bool,
) -> OpenClankTuiClient:
    client = OpenClankTuiClient(profile, token=vault.get(profile.name))
    try:
        client.info()
        return client
    except TuiClientError as exc:
        client.close()
        if (
            exc.reason != "connection_refused"
            or not (auto_start and profile.local and profile.auto_start)
        ):
            raise
    LocalServerManager(repo_root=REPO_ROOT).start(profile)
    client = OpenClankTuiClient(profile, token=vault.get(profile.name))
    client.info()
    return client


def _authorize(client: OpenClankTuiClient, *, label: str, open_browser: bool, questions: bool = False, approvals: bool = False) -> str:
    scopes = None
    if questions or approvals:
        scopes = ["tui:diagnostics", "tui:providers", "tui:sessions", "tui:shares", "tui:tasks", "tui:workspaces"]
        scopes += (["tui:questions"] if questions else []) + (["tui:approvals"] if approvals else [])
    flow = client.start_device_authorization(device_label=label, scopes=scopes)
    _terminal_print(f"Open this address and approve the terminal:\n  {flow.verification_uri_complete}")
    _terminal_print(f"Device code: {flow.user_code}")
    if open_browser:
        webbrowser.open(flow.verification_uri_complete)
    _terminal_print("Waiting for approval", end="", flush=True)
    result = client.poll_device_authorization(
        flow,
        on_wait=lambda count: _terminal_print("." if count % 12 else ".\n", end="", flush=True),
    )
    _terminal_print(" approved")
    return str(result["access_token"])


def _run_tui(args, store: ProfileStore, vault: ClientCredentialVault) -> int:
    profile = _profile_for(args, store)
    with _connected_client(profile, vault, auto_start=True) as client:
        if not client.token:
            token = _authorize(client, label="Open Clank TUI", open_browser=True)
            vault.set(profile.name, token)
            if not vault.persistent:
                _terminal_print("No OS credential vault is available; this login lasts only for this process.")
        return OpenClankTui(client).run()


def _run_profile(args, store: ProfileStore, vault: ClientCredentialVault) -> int:
    if args.profile_command == "list":
        active = store.active_name()
        for item in store.list():
            marker = "*" if item.name == active else " "
            mode = "local, auto-start" if item.auto_start else ("local" if item.local else "remote")
            _terminal_print(f"{marker} {item.name:<20} {item.url:<40} {mode}")
        return 0
    if args.profile_command == "add":
        item = ClientProfile.create(args.name, args.url, auto_start=args.auto_start)
        store.put(item, make_active=args.use)
        _terminal_print(f"Saved profile {item.name!r} ({item.url})")
        return 0
    if args.profile_command == "use":
        item = store.use(args.name)
        _terminal_print(f"Active profile: {item.name}")
        return 0
    if args.profile_command == "remove":
        if not store.remove(args.name):
            raise ProfileError(f"unknown profile {args.name!r}")
        vault.delete(args.name)
        _terminal_print(f"Removed profile {args.name!r}")
        return 0
    return 2


def _run_login(args, store: ProfileStore, vault: ClientCredentialVault) -> int:
    profile = _profile_for(args, store)
    with _connected_client(profile, vault, auto_start=True) as client:
        token = _authorize(client, label=args.label, open_browser=not args.no_browser, questions=args.questions, approvals=args.approvals)
        vault.set(profile.name, token)
    _terminal_print(f"Logged in to {profile.name!r}; credential storage: {vault.status}.")
    return 0


def _run_logout(args, store: ProfileStore, vault: ClientCredentialVault) -> int:
    profile = _profile_for(args, store)
    token = vault.get(profile.name)
    if token:
        try:
            with OpenClankTuiClient(profile, token=token) as client:
                client.revoke_current_device()
        except TuiClientError as exc:
            if exc.status_code not in {401, 404}:
                raise
    vault.delete(profile.name)
    _terminal_print(f"Logged out of {profile.name!r}.")
    return 0


def _run_server(args, store: ProfileStore) -> int:
    profile = _profile_for(args, store)
    manager = LocalServerManager(repo_root=REPO_ROOT)
    if args.open_browser and args.action != "start":
        raise ProfileError("--open-browser is available only with server start")
    if args.action == "start":
        status = manager.start(profile, allow_auth_setup=args.open_browser)
        if args.open_browser:
            if not webbrowser.open(status.url):
                raise ServerManagerError("Windows could not open the browser; open " + status.url)
    elif args.action == "stop":
        status = manager.stop(profile)
    else:
        status = manager.status(profile)
    state = "ready" if status.ready else ("running" if status.running else "stopped")
    pid = f" pid={status.pid}" if status.pid else ""
    _terminal_print(f"Open Clank {state} at {status.url}{pid}: {status.detail}")
    if args.action == "stop":
        return 0
    if args.action == "status" and status.running:
        return 0
    return 0 if status.ready or (args.open_browser and status.running) else 1


def _run_engine(args) -> int:
    values = list(args.engine_args)
    if not values:
        values = ["verify"]
    if values[0] == "doctor":
        values[0] = "verify"
    elif values[0] == "version":
        values = ["verify"]
    if getattr(sys, "frozen", False):
        from scripts.openclank_engine import main as engine_main
        if values[0] == "verify":
            try:
                from scripts.openclank_bootstrap import verify_portable_payload

                verify_portable_payload()
            except Exception as exc:
                _terminal_print(f"openclank engine: {exc}", file=sys.stderr)
                return 1

        return int(engine_main(values))
    script = REPO_ROOT / "scripts" / "openclank_engine.py"
    return subprocess.call([sys.executable, str(script), *values], cwd=REPO_ROOT)


def _run_hex(args) -> int:
    from src.hex_contract_cli import main as hex_main

    return int(hex_main(list(args.hex_args)))


def _run_managed_server(values: list[str]) -> int:
    """Private frozen entrypoint used by local TUI auto-start.

    It is intentionally absent from argparse/help and is accepted only by a
    frozen release payload.  Verification and current-store validation finish before the
    FastAPI application module is imported.
    """

    if not getattr(sys, "frozen", False):
        _terminal_print("openclank: private server entrypoint is available only in a packaged build", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--host", default=os.environ.get("APP_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("APP_PORT", "7777")))
    args = parser.parse_args(values)
    if not 1 <= args.port <= 65535:
        _terminal_print("openclank: server port must be between 1 and 65535", file=sys.stderr)
        return 2
    if str(args.host).lower().rstrip(".") not in {"localhost", "127.0.0.1", "::1"}:
        _terminal_print("openclank: packaged server startup is restricted to loopback", file=sys.stderr)
        return 2
    phase = "bootstrap"
    try:
        from scripts.openclank_bootstrap import prepare_service

        prepare_service()
        phase = "uvicorn-import"
        import uvicorn
        phase = "application-import"
        from app import app

        phase = "serving"
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0
    except Exception as exc:
        if sys.platform == "darwin":
            from scripts.macos_entry import STARTUP_DIAGNOSTIC_PREFIX, startup_failure_record

            _terminal_print(STARTUP_DIAGNOSTIC_PREFIX + json.dumps(startup_failure_record(exc, phase), sort_keys=True), file=sys.stderr, flush=True)
        # Bootstrap exceptions contain invariant names and paths, never
        # provider credential values.  Do not expose tracebacks in the normal
        # packaged startup path.
        _terminal_print(f"openclank: packaged server startup failed: {exc}", file=sys.stderr)
        return 1


def _dispatch_private_automation(argv: list[str]) -> int | None:
    if not argv or argv[0].startswith("-") or argv[0] in RESERVED_COMMANDS:
        return None
    name = argv[0]
    if not name.replace("-", "").replace("_", "").isalnum():
        return None
    script = REPO_ROOT / "scripts" / f"odysseus-{name}"
    if not script.is_file():
        return None
    return subprocess.call([sys.executable, str(script), *argv[1:]], cwd=REPO_ROOT)


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] == "__managed-server":
        return _run_managed_server(values[1:])
    if values and values[0] == "hex":
        from src.hex_contract_cli import main as hex_main

        return int(hex_main(values[1:]))
    private = _dispatch_private_automation(values)
    if private is not None:
        return private
    args = _parser().parse_args(values)
    if args.version:
        _terminal_print(f"Open Clank {_version()}")
        return 0
    if args.command == "assets":
        try:
            return _run_assets(args)
        except ProfileError as exc:
            _terminal_print(f"openclank: {exc}", file=sys.stderr)
            return 1
    store = ProfileStore()
    vault = ClientCredentialVault()
    try:
        if args.command == "run":
            return _run_once(args, store, vault)
        if args.command in {None, "tui"}:
            return _run_tui(args, store, vault)
        if args.command == "profile":
            return _run_profile(args, store, vault)
        if args.command == "login":
            return _run_login(args, store, vault)
        if args.command == "logout":
            return _run_logout(args, store, vault)
        if args.command == "server":
            return _run_server(args, store)
        if args.command == "engine":
            return _run_engine(args)
        if args.command == "hex":
            return _run_hex(args)
        return 2
    except (ProfileError, ServerManagerError, TuiClientError) as exc:
        _terminal_print(f"openclank: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
