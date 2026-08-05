"""Supervisor for the mimo ACP child process.

Handles spawn, ACP handshake, crash detection, bounded restart backoff,
and session reconciliation via session/resume.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx

from src.endpoint_resolver import (
    DIRECT_ENDPOINT_PROVIDER_PREFIX,
    direct_runtime_provider_id,
)
from src.openclank.acp_client import ACPClient, TransportError
from src.openclank.acp_bridge import (
    ACPBridge,
    PermissionHandler,
    frankenmemory_child_env,
    register_client_callbacks,
)
from src.memory_scope import chat_workspace

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
MIMO_BIN = REPO_ROOT / "bin" / "mimo"

# Open Clank skill root — where SkillsManager stores SKILL.md files.
# The bundled runtime's discoverSkills scans cfg.skills.paths for **/SKILL.md.
_OPEN_CLANK_DATA_DIR = (
    os.getenv("OPEN_CLANK_DATA_DIR")
    or os.getenv("ODYSSEUS_DATA_DIR")
    or str(REPO_ROOT / "data")
)
_OPEN_CLANK_SKILLS_DIR = str(Path(_OPEN_CLANK_DATA_DIR) / "skills")


def _inject_skill_catalog(env: dict[str, str]) -> None:
    """Point an isolated worker at the central, owner-filtered skill catalogue."""
    env["OPEN_CLANK_CONTROL_DATA_DIR"] = _OPEN_CLANK_DATA_DIR
    env["MIMOCODE_CONFIG_CONTENT"] = json.dumps({
        "skills": {"paths": [_OPEN_CLANK_SKILLS_DIR]},
        "memory": {"provider": "frankenmemory"},
    })
    env["OPEN_CLANK_SKILLS_DIR"] = _OPEN_CLANK_SKILLS_DIR


# Restart backoff
# Strips ANSI color/style sequences from mimo's stderr log lines.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Max size of one newline-delimited ACP JSON message from the child. Tool
# results embed whole file contents, so this must comfortably exceed any
# single read (asyncio's default is 64 KiB — a live worker-killer).
ACP_STDOUT_LIMIT = 32 * 1024 * 1024

_READINESS_BUDGET = float(
    os.environ.get("OPEN_CLANK_AGENT_READINESS_BUDGET")
    or os.environ.get("ODYSSEUS_MIMO_READINESS_BUDGET")
    or "30"
)
_RESTART_DELAY_INITIAL = 0.25
_RESTART_DELAY_MAX = 5.0
_RESTART_DELAY_MULTIPLIER = 2.0

# Model ids that cannot hold a chat conversation — never a small_model pick.
_NON_CHAT_MODEL_RE = re.compile(r"tts|voice|embed|rerank|image|audio|video|ocr|asr", re.IGNORECASE)


def _pick_small_model(providers: dict) -> str | None:
    """First chat-capable model across the injected providers (provider/model).

    Mimo's title/summarize subagents use the configured ``small_model``; left
    unset, its picker grabbed xiaomi's TTS voicedesign model from our injected
    list and every title call 400'd ("system role is not allowed for TTS
    model"). We injected the list, so we pin a sane default. Override with
    OPEN_CLANK_SMALL_MODEL (the injected env config is merged last in the
    bundled runtime's chain, so a file-level small_model would not win).
    """
    override = (
        os.environ.get("OPEN_CLANK_SMALL_MODEL")
        or os.environ.get("ODYSSEUS_SMALL_MODEL")
    )
    if override:
        return override
    for pid, cfg in providers.items():
        models = cfg.get("models") or {}
        ids = list(models) if isinstance(models, (dict, list, tuple)) else []
        for mid in ids:
            if not _NON_CHAT_MODEL_RE.search(str(mid)):
                return f"{pid}/{mid}"
    return None


# OAuth access JWTs are routinely larger than static API keys (the ChatGPT
# subscription token is currently ~2.3 KiB). Keep the handoff bounded while
# allowing a normal HTTP authorization credential through.
_MAX_PROVIDER_KEY_LENGTH = 8192


def migrate_agent_runtime_root(data_dir: Path, *, rollback: bool = False) -> Path:
    """Atomically move embedded-agent state under Open Clank's runtime root."""
    data_dir = Path(data_dir)
    legacy = data_dir / "mimocode"
    current = data_dir / "runtime" / "agent-engine"
    source, target = (current, legacy) if rollback else (legacy, current)
    status = "unchanged"
    if source.exists() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        status = "rolled_back" if rollback else "migrated"
    elif source.exists() and target.exists():
        status = "conflict"
        logger.error(
            "Open Clank agent-runtime migration conflict: both %s and %s exist",
            source,
            target,
        )

    marker_dir = data_dir / ".migrations"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / "open-clank-agent-runtime-v1.json"
    marker.write_text(json.dumps({
        "canonical": str(current.relative_to(data_dir)),
        "legacy": str(legacy.relative_to(data_dir)),
        "status": status,
        "version": 1,
    }, sort_keys=True) + "\n", encoding="utf-8")
    marker.chmod(0o600)
    return legacy if rollback else current


def _mimo_child_environment() -> dict[str, str]:
    """Build from an allowlist so host credentials/config never leak by name."""
    allowed = {
        "COLORTERM",
        "LANG",
        "LANGUAGE",
        "NODE_EXTRA_CA_CERTS",
        "OPEN_CLANK_SHELL_NETWORK",
        "OPEN_CLANK_SHELL_SANDBOX",
        "PATH",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "TZ",
    }
    env = {
        name: value
        for name, value in os.environ.items()
        if name in allowed or name.startswith("LC_")
    }
    env.update(frankenmemory_child_env())
    env["FM_WORKSPACE_ID"] = chat_workspace()
    # Open Clank supplies credentials, config, skills, and account identity.
    # Embedded workers must not discover state from a personal MiMo install.
    env.update({
        "MIMOCODE_DISABLE_PROVIDER_ENV": "1",
        "MIMOCODE_DISABLE_PROJECT_CONFIG": "1",
        "MIMOCODE_DISABLE_EXTERNAL_SKILLS": "1",
        "MIMOCODE_DISABLE_REMOTE_SKILLS": "1",
        "MIMOCODE_DISABLE_CLAUDE_CODE": "1",
    })
    return env


ENDPOINT_PROVIDER_PREFIX = DIRECT_ENDPOINT_PROVIDER_PREFIX


def _endpoint_registry_providers(
    owner: str = "",
    *,
    shared_access=None,
) -> tuple[dict, dict[str, str]]:
    """Project Open Clank's ModelEndpoint registry into mimo providers.

    Every enabled OpenAI-compatible endpoint becomes a provider named
    `ody-<endpoint_id>` (collision-proof against xiaomi/deepseek/native
    mimo providers) with the same model list the Settings UI shows
    (cached + pinned − hidden; no probing at spawn). Keys ride the
    credential dict for the pipe FD, NEVER the config content or env.

    Open Clank agent can drive every OpenAI-compatible model here. Per-model Tools
    preferences control the turn's tool policy, not whether the model exists
    in the runtime catalog.
    """
    try:
        from core.database import SessionLocal, ModelEndpoint
        from src.auth_helpers import owner_filter
        from src.endpoint_resolver import (
            _endpoint_enabled_models,
            build_headers,
            normalize_base,
            resolve_endpoint_runtime,
        )
        from src.chatgpt_subscription import is_chatgpt_subscription_base
        from src.model_shares import shared_runtime_provider_id, source_endpoint_for_access
    except Exception as exc:  # pragma: no cover - import cycle guard
        logger.warning("endpoint projection unavailable: %s", exc)
        return {}, {}

    providers: dict[str, dict] = {}
    credentials: dict[str, str] = {}
    db = SessionLocal()
    try:
        query = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        query = owner_filter(
            query, ModelEndpoint, owner or "", include_shared=False
        )
        if shared_access is None:
            registrations = [
                (
                    ep,
                    direct_runtime_provider_id(ep.id),
                    _endpoint_enabled_models(ep),
                    owner or None,
                )
                for ep in query.all()
            ]
        else:
            endpoint = source_endpoint_for_access(db, shared_access)
            registrations = (
                [(
                    endpoint,
                    shared_runtime_provider_id(shared_access.share_id),
                    [shared_access.model_id],
                    shared_access.credential_owner,
                )]
                if endpoint is not None
                else []
            )
        for ep, provider_id, model_ids, credential_owner in registrations:
            if (getattr(ep, "model_type", None) or "llm") != "llm":
                continue
            try:
                base, api_key = resolve_endpoint_runtime(
                    ep,
                    owner=credential_owner,
                )
            except Exception as exc:
                logger.warning("endpoint %s credential resolution failed: %s", ep.id, exc)
                continue
            base = normalize_base(base or "")
            if not base.startswith(("http://", "https://")):
                continue
            if not model_ids:
                continue
            is_chatgpt_subscription = is_chatgpt_subscription_base(base)
            adapter = (
                "@ai-sdk/openai"
                if is_chatgpt_subscription
                else "@ai-sdk/openai-compatible"
            )
            options = {"baseURL": base}
            if is_chatgpt_subscription:
                headers = build_headers(api_key, base)
                headers.pop("Authorization", None)
                options["headers"] = headers
            providers[provider_id] = {
                "name": ep.name or ep.id,
                "npm": adapter,
                "api": base,
                "options": options,
                "models": {
                    model_id: {
                        "id": model_id,
                        "name": model_id,
                        "provider": {"npm": adapter, "api": base},
                    }
                    for model_id in model_ids
                },
                "only_configured_models": True,
            }
            if isinstance(api_key, str) and 0 < len(api_key) <= _MAX_PROVIDER_KEY_LENGTH:
                credentials[provider_id] = api_key
    except Exception as exc:
        logger.warning("endpoint projection query failed: %s", exc)
        return {}, {}
    finally:
        db.close()

    return ({"provider": providers} if providers else {}), credentials


def _load_stored_auth(owner: str) -> tuple[str | None, float]:
    """(payload_json, updated_at_epoch) for an owner's provider credentials."""
    from core.database import SessionLocal, MimoAuthStore

    db = SessionLocal()
    try:
        row = db.query(MimoAuthStore).filter(MimoAuthStore.owner == (owner or "")).first()
        if row is None or not row.payload:
            return None, 0.0
        updated = getattr(row, "updated_at", None)
        epoch = updated.replace(tzinfo=timezone.utc).timestamp() if updated else 0.0
        return row.payload, epoch
    finally:
        db.close()


def _store_auth(owner: str, payload: str) -> None:
    from core.database import SessionLocal, MimoAuthStore

    db = SessionLocal()
    try:
        row = db.query(MimoAuthStore).filter(MimoAuthStore.owner == (owner or "")).first()
        if row is None:
            row = MimoAuthStore(owner=owner or "", payload=payload)
            db.add(row)
        else:
            row.payload = payload
        db.commit()
    finally:
        db.close()


def _select_host_provider_owner(
    admin_owners: list[str],
    explicit_owner: str = "",
) -> str:
    admins = {str(owner).strip().lower() for owner in admin_owners if str(owner).strip()}
    explicit = explicit_owner.strip().lower()
    if explicit:
        return explicit if explicit in admins else ""
    return next(iter(admins)) if len(admins) == 1 else ""


def _loopback_port(explicit: str | None = None) -> int:
    """Return a valid configured port or reserve an ephemeral loopback port."""
    if explicit:
        try:
            port = int(explicit)
        except ValueError as exc:
            raise ValueError("OPEN_CLANK_AGENT_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("OPEN_CLANK_AGENT_PORT must be between 1 and 65535")
        return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MimoSupervisor:
    """Spawns and supervises a single `mimo acp` child over stdio.

    Owns the ACPClient and ACPBridge. Detects crashes, restarts with
    bounded backoff, and reconciles in-flight sessions via session/resume.
    """

    def __init__(
        self,
        owner: str = "",
        permission_handler=None,
        memory_provider=None,
        safe_dirs: list[str] | None = None,
        runtime_home: Path | None = None,
        partitioned: bool = False,
        grant_store=None,
        projection_snapshot=None,
        projection_generation: int = 0,
        crash_callback=None,
        credential_sources: dict[str, str] | None = None,
    ) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._client: ACPClient | None = None
        self._bridge: ACPBridge | None = None
        self._health_task: asyncio.Task | None = None
        self._stopping = False
        self._owner = owner
        self._permission_handler = permission_handler
        self._memory_provider = memory_provider
        self._safe_dirs = safe_dirs
        self._grant_store = grant_store
        self._runtime_home = runtime_home
        self._partitioned = partitioned
        self.projection_snapshot = projection_snapshot
        self.installed_fingerprint = getattr(projection_snapshot, "fingerprint", "")
        self.installed_generation = projection_generation
        self._crash_callback = crash_callback
        self._provider_apis: dict[str, str] = {}
        self._mimocode_home: str | None = None
        # None = this account's personal auth store. A dict (including empty)
        # = a dedicated trusted-share worker with only those provider sources.
        self._credential_sources = (
            None
            if credential_sources is None
            else dict(credential_sources)
        )
        self._auth_seed_payload: dict = {}
        # The ACP command already owns an HTTP server. Pin its loopback port so
        # Open Clank can expose a narrow provider-auth adapter without launching
        # a second `mimo serve` process. Keep it stable across child restarts.
        self._http_port = _loopback_port(
            None
            if partitioned
            else (
                os.environ.get("OPEN_CLANK_AGENT_PORT")
                or os.environ.get("ODYSSEUS_MIMO_PORT")
            )
        )
        # Loopback is not an authorization boundary: another local process could
        # otherwise reach this owner's provider and session APIs. Keep a unique
        # credential in this supervisor only; callers receive an authenticated
        # client, never the raw secret.
        self.__http_username = "open-clank"
        self.__http_password = secrets.token_urlsafe(48)
        # The live handler (caller-supplied or auto-built); chat_routes uses
        # this to resolve permission prompts from the UI.
        self.permission_handler = permission_handler

    async def start(self) -> None:
        """Spawn the child, perform ACP handshake, set up bridge."""
        await self._spawn_and_init()

    async def _spawn_and_init(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            logger.warning("mimo child already running (pid %d)", self._proc.pid)
            return

        logger.info("starting mimo acp child: %s acp (http 127.0.0.1:%d)", MIMO_BIN, self._http_port)

        # Inject Open Clank skills into the embedded agent engine.
        # MIMOCODE_CONFIG_CONTENT is loaded last in mimo's config chain
        # (config.ts L835) and merges on top of everything else.
        # The child receives only the allowlisted runtime shape. Memory calls
        # borrow its owner-scoped lifetools transport, which authenticates to
        # the one app-owned fm-mcp broker instead of spawning another engine.
        env = _mimo_child_environment()
        env["OPEN_CLANK_MANAGED"] = "1"
        env["OPEN_CLANK_OWNER"] = str(self._owner or "")
        env["OPEN_CLANK_PROJECT_POLICY_BRIDGE"] = "required"
        http_auth_fd: int | None = None
        provider_auth_fd: int | None = None
        # Server directory-containment check (middleware.ts:24-29): when no
        # server password is set, the server requires requested directories to
        # be within its CWD. Change the child's CWD to the host user's home so
        # all user workspaces (~/sauce, ~/entities, ~/open-clank) are reachable.
        _inject_skill_catalog(env)
        if os.path.isdir(_OPEN_CLANK_SKILLS_DIR) and not self._partitioned:
            child_data_dir = str(Path(_OPEN_CLANK_SKILLS_DIR).parent)
            env["OPEN_CLANK_DATA_DIR"] = child_data_dir
            # Compatibility for the bundled runtime until all older builds
            # consume OPEN_CLANK_DATA_DIR.
            env["ODYSSEUS_DATA_DIR"] = child_data_dir
            logger.info("injected Open Clank skills path: %s", _OPEN_CLANK_SKILLS_DIR)
        elif not os.path.isdir(_OPEN_CLANK_SKILLS_DIR):
            logger.warning("Open Clank skills dir not found: %s", _OPEN_CLANK_SKILLS_DIR)

        # The embedded mimo must NEVER share state with a personal mimocode
        # install: under XDG defaults it reads ~/.config/mimocode (the user's
        # model defaults + provider config) and writes sessions/auth/logs into
        # ~/.local/share/mimocode — both directions of that are wrong. Always
        # set MIMOCODE_HOME (the bundled-runtime boundary) to Open Clank's own
        # data dir. Host MIMOCODE_HOME is intentionally ignored. Precedence:
        # OPEN_CLANK_AGENT_HOME > legacy THESIUS_AGENT_HOME > internal default.
        # Config + auth in that home are hand-managed (e's ruling 2026-07-09:
        # no automatic copying of credential files — boot once, edit config).
        if self._runtime_home is not None:
            self._runtime_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._runtime_home.chmod(0o700)
            private_home = self._runtime_home / "home"
            private_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            private_home.chmod(0o700)
            private_tmp = private_home / "tmp"
            private_tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
            private_tmp.chmod(0o700)
            env["MIMOCODE_HOME"] = str(self._runtime_home / "mimocode")
            env["HOME"] = str(private_home)
            env["USERPROFILE"] = str(private_home)
            env["TMPDIR"] = str(private_tmp)
            # Pin EVERY XDG authority to the per-owner home. MIMOCODE_HOME + HOME
            # are not enough: mimo's XDG fallback can resolve to the real uid's
            # home via getpwuid (ignoring $HOME) and read the host mimo install's
            # ~/.config/mimocode (provider config) and ~/.local/share/mimocode
            # (auth) — bleeding the host's providers into this owner's runtime.
            # Open Clank's mimo uses Open Clank dirs only, never mimocode dirs.
            env["XDG_CONFIG_HOME"] = str(private_home / ".config")
            env["XDG_DATA_HOME"] = str(private_home / ".local" / "share")
            env["XDG_STATE_HOME"] = str(private_home / ".local" / "state")
            env["XDG_CACHE_HOME"] = str(private_home / ".cache")
            child_data_dir = str(self._runtime_home / "open-clank")
            env["OPEN_CLANK_DATA_DIR"] = child_data_dir
            env["ODYSSEUS_DATA_DIR"] = child_data_dir
            _inject_skill_catalog(env)
        elif "MIMOCODE_HOME" not in env:
            _agent_home = (
                os.environ.get("OPEN_CLANK_AGENT_HOME")
                or os.environ.get("THESIUS_AGENT_HOME")
            )
            if _agent_home:
                env["MIMOCODE_HOME"] = os.path.join(
                    os.path.expanduser(_agent_home),
                    "runtime",
                    "agent-engine",
                )
            else:
                _data_dir = (
                    os.environ.get("OPEN_CLANK_DATA_DIR")
                    or os.environ.get("ODYSSEUS_DATA_DIR")
                    or str(REPO_ROOT / "data")
                )
                env["MIMOCODE_HOME"] = os.path.join(_data_dir, "runtime", "agent-engine")
        self._mimocode_home = env["MIMOCODE_HOME"]
        self._reconcile_auth_store()
        snapshot = self.projection_snapshot
        if snapshot is None:
            from src.openclank.mimo_projection import build_projection_snapshot
            snapshot = build_projection_snapshot(self._owner)
            self.projection_snapshot = snapshot
            self.installed_fingerprint = snapshot.fingerprint
        merged_providers = dict(snapshot.providers)
        merged_credentials = dict(snapshot.credentials)
        if merged_providers:
            config_content = json.loads(env.get("MIMOCODE_CONFIG_CONTENT", "{}"))
            config_content.setdefault("provider", {}).update(merged_providers)
            small_model = snapshot.small_model
            if small_model and "small_model" not in config_content:
                config_content["small_model"] = small_model
                logger.info("mimo small_model pinned: %s", small_model)
            env["MIMOCODE_CONFIG_CONTENT"] = json.dumps(config_content)
            # Remember each provider's API base so Settings can render
            # these as ordinary endpoint rows (URL and all).
            self._provider_apis = {
                pid: cfg.get("api", "")
                for pid, cfg in merged_providers.items()
            }
            if merged_credentials:
                provider_auth_fd, write_fd = os.pipe()
                try:
                    payload = json.dumps(merged_credentials).encode()
                    if os.write(write_fd, payload) != len(payload):
                        raise RuntimeError("incomplete Open Clank agent provider credential handoff")
                finally:
                    os.close(write_fd)
                env["MIMOCODE_PROVIDER_AUTH_FD"] = str(provider_auth_fd)
            logger.info(
                "injected host model providers for owner %s: %s",
                self._owner,
                ", ".join(sorted(merged_providers)),
            )
        elif snapshot.small_model:
            config_content = json.loads(
                env.get("MIMOCODE_CONFIG_CONTENT", "{}")
            )
            config_content.setdefault("small_model", snapshot.small_model)
            env["MIMOCODE_CONFIG_CONTENT"] = json.dumps(config_content)
        env["MIMOCODE_ENABLE_QUESTION_TOOL"] = "1"
        logger.info("mimo child MIMOCODE_HOME: %s", env["MIMOCODE_HOME"])

        try:
            http_auth_fd = self._configure_internal_http_auth(env)
            spawn_options = {
                "stdin": asyncio.subprocess.PIPE,
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
                # ACP messages are newline-delimited JSON on stdout; a single
                # tool result carrying a large file easily exceeds asyncio's
                # default 64 KiB StreamReader line limit, which kills the
                # reader (LimitOverrunError) and takes the whole worker down
                # mid-turn. Size the buffer for real tool traffic.
                "limit": ACP_STDOUT_LIMIT,
                "env": env,
                "cwd": str(Path.home()),
                # Detach from the terminal's process group: Ctrl+C must reach
                # only the server. Otherwise the child dies on the operator's
                # SIGINT before the shutdown event runs, and the health
                # monitor "helpfully" respawns it mid-shutdown, blocking exit.
                # Shutdown is owned by stop(): stdin EOF, then kill.
                "start_new_session": True,
            }
            inherited_fds = tuple(
                fd for fd in (http_auth_fd, provider_auth_fd) if fd is not None
            )
            if inherited_fds:
                spawn_options["pass_fds"] = inherited_fds
            self._proc = await asyncio.create_subprocess_exec(
                # --print-logs mirrors mimo's own log stream to stderr, which
                # _drain_stderr folds into the host log — ONE log to read
                # instead of chasing per-owner files under data/mimocode.
                str(MIMO_BIN), "acp", "--hostname", "127.0.0.1", "--port", str(self._http_port),
                "--print-logs",
                **spawn_options,
            )
        except NotImplementedError as exc:
            # A synchronous fallback cannot safely provide the asyncio stream
            # objects ACPClient requires; the old fallback also leaked a first
            # untracked child before spawning a second one.
            raise RuntimeError("async subprocess support is required for mimo ACP") from exc
        finally:
            if http_auth_fd is not None:
                os.close(http_auth_fd)
            if provider_auth_fd is not None:
                os.close(provider_auth_fd)

        logger.info("mimo child started (pid %d)", self._proc.pid)
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        # Create ACP client on the child's stdio
        assert self._proc.stdin and self._proc.stdout
        self._client = ACPClient(self._proc.stdout, self._proc.stdin)

        # Build permission handler: caller-supplied handler takes precedence.
        # C1: otherwise always create one — safe-dirs auto-approve, then
        # durable grants from app.db, then an interactive prompt in the chat
        # stream (previously, requests outside safe dirs silently rejected).
        perm_handler = self._permission_handler
        if perm_handler is None:
            if self._grant_store is None:
                try:
                    from src.constants import DATA_DIR
                    from src.openclank.permission_grants import GrantStore
                    self._grant_store = GrantStore(str(Path(DATA_DIR) / "app.db"))
                except Exception as e:
                    logger.warning("permission grant store unavailable: %s", e)
            perm_handler = PermissionHandler(
                safe_dirs=self._safe_dirs, grant_store=self._grant_store
            )
            logger.info(
                "permission handler created (safe dirs: %s, durable grants: %s)",
                self._safe_dirs,
                "on" if self._grant_store else "off",
            )
        self.permission_handler = perm_handler

        register_client_callbacks(self._client, permission_handler=perm_handler)
        await self._client.start_reader()

        # Perform ACP handshake
        try:
            result = await self._client.initialize()
            logger.info("ACP handshake complete: %s", result.get("agentInfo", {}))
        except (TransportError, Exception) as e:
            logger.error("ACP handshake failed: %s", e)
            await self._teardown_child()
            raise

        # Create the bridge (B2: owner flows through for lifetools MCP context)
        configured_cwd = Path(
            os.environ.get("OPEN_CLANK_GLOBAL_CWD")
            or os.environ.get("OPENTHESIUS_GLOBAL_CWD")
            or str(REPO_ROOT)
        ).expanduser()
        if not configured_cwd.is_dir():
            logger.warning(
                "configured OPEN_CLANK_GLOBAL_CWD is missing: %s; using %s",
                configured_cwd,
                REPO_ROOT,
            )
            configured_cwd = REPO_ROOT
        self._bridge = ACPBridge(
            self._client,
            str(configured_cwd),
            owner=self._owner,
            permission_handler=perm_handler,
            memory_provider=self._memory_provider,
            session_map_path=(
                self._runtime_home / "session-map.json"
                if self._runtime_home is not None
                else None
            ),
        )
        self._bridge.set_session_delete_callback(self.delete_session)

        # Warm the model catalog: mimo only reports availableModels in a
        # session handshake, so open one throwaway session at boot. Lives
        # only in the isolated mimo store; Open Clank never lists it.
        catalog_session = None
        try:
            catalog_session = await self._bridge.open_session(with_agent_tools=False)
            logger.info(
                "mimo model catalog warmed: %d models", len(self._bridge.available_models)
            )
        except Exception as e:
            logger.warning("mimo model catalog warmup failed: %s", e)
        finally:
            if catalog_session:
                try:
                    await self.delete_session(catalog_session)
                except Exception as e:
                    logger.warning("mimo model catalog cleanup failed: %s", e)

        await self._purge_stale_projections()

        # Start health monitor
        self._health_task = asyncio.create_task(self._health_monitor())

    async def _drain_stderr(self) -> None:
        """Fold the child's stderr — mimo's log stream under --print-logs —
        into the host log at the line's own severity, so app.log is the one
        place to look. Must never die while the child lives: an undrained
        stderr pipe fills up and blocks the child mid-turn."""
        assert self._proc and self._proc.stderr
        try:
            async for raw in self._proc.stderr:
                line = _ANSI_RE.sub("", raw.decode(errors="replace")).rstrip()
                if not line:
                    continue
                level = line.split(" ", 1)[0]
                if level == "ERROR":
                    logger.error("mimo: %s", line)
                elif level == "WARN":
                    logger.warning("mimo: %s", line)
                elif level in ("INFO", "DEBUG"):
                    logger.info("mimo: %s", line)
                else:
                    # Not a mimo log line (raw crash output, bun panic, …).
                    logger.warning("mimo stderr: %s", line)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("mimo stderr drain failed (child output no longer mirrored): %s", exc)

    async def _health_monitor(self) -> None:
        """Detect child crash (EOF or proc exit) and trigger restart."""
        try:
            while not self._stopping:
                await asyncio.sleep(1.0)
                if self._proc is None:
                    break
                if self._proc.returncode is not None:
                    logger.warning("mimo child exited (code %d)", self._proc.returncode)
                    await self._handle_crash(self._proc.returncode)
                    return
                if self._client and self._client.is_closed:
                    logger.warning("ACP client closed (child likely crashed)")
                    await self._handle_crash()
                    return
        except asyncio.CancelledError:
            pass

    async def _handle_crash(self, returncode: int | None = None) -> None:
        """Fail this generation; the pool owns single-flight cold recovery."""
        if self._stopping:
            return
        # SIGINT/SIGTERM death usually means host shutdown (systemd kills the
        # whole cgroup before our shutdown event runs). Give stop() a moment
        # to raise the flag before treating it as a crash worth restarting.
        if returncode in (-signal.SIGINT, -signal.SIGTERM):
            await asyncio.sleep(2.0)
            if self._stopping:
                return

        # Fail all pending requests on the old client
        if self._client:
            await self._client.close()

        # The process may have refreshed an OAuth token immediately before it
        # died. The cache is still readable even when the child is gone.
        self.sync_auth_to_db()
        await self._teardown_child()

        if self._crash_callback:
            await self._crash_callback(self, returncode)

    async def _restart_with_backoff(self) -> None:
        """Compatibility entry point: one pool-owned readiness campaign replaces it."""
        raise RuntimeError("Open Clank agent restart is coordinated by MimoSupervisorPool")

    async def _reconcile_sessions(self) -> None:
        """Discard interrupted projections; the next turn replays canonical history."""
        await self._purge_stale_projections()

    async def _purge_stale_projections(self) -> None:
        if not self._bridge:
            return
        sessions = self._bridge.mapped_sessions()
        # A recipient can have a personal worker plus multiple trusted-share
        # workers. Transcript rows are owner-scoped, not partition-scoped, so
        # sweeping every row for this owner here lets one worker delete another
        # worker's live projection. Only this worker's persisted map is safe.
        for session_id, mimo_session_id in sessions.items():
            try:
                await self.delete_session(
                    session_id, mimo_session_id=mimo_session_id
                )
            except Exception as exc:
                logger.warning("failed to purge stale Open Clank agent projection %s: %s", session_id, exc)

    async def _teardown_child(self) -> None:
        """Clean up the child process and associated tasks."""
        if self._bridge:
            try:
                await self._bridge.terminal_manager.close()
            except Exception as exc:
                logger.warning("Open Clank agent terminal cleanup failed: %s", exc)
        if (
            self._health_task
            and self._health_task is not asyncio.current_task()
            and not self._health_task.done()
        ):
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None
        elif self._health_task is asyncio.current_task():
            self._health_task = None

        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None

        if self._proc:
            proc = self._proc
            self._proc = None
            if proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass

        self._client = None
        self._bridge = None

    async def stop(self) -> None:
        """Graceful shutdown — stop health monitor, close client, kill child."""
        self._stopping = True

        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        if self._client:
            await self._client.close()

        if self._proc is None:
            self.sync_auth_to_db()
            await self._teardown_child()
            return

        proc = self._proc
        self._proc = None

        if proc.returncode is not None:
            logger.info("mimo child already exited (code %d)", proc.returncode)
            self.sync_auth_to_db()
            await self._teardown_child()
            return

        logger.info("stopping mimo child (pid %d)", proc.pid)

        if proc.stdin:
            try:
                proc.stdin.close()
            except Exception:
                pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
            logger.info("mimo child exited gracefully (code %d)", proc.returncode)
        except asyncio.TimeoutError:
            logger.warning("mimo child did not exit in time, killing")
            proc.kill()
            await proc.wait()

        # Child is down; its auth store is final — capture OAuth refreshes.
        self.sync_auth_to_db()

        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass

    def available_models(self, owner: str | None = None) -> list:
        """mimo's model catalog ({modelId, name} dicts) from the last handshake."""
        return list(self._bridge.available_models) if self._bridge else []

    def provider_apis(self, owner: str | None = None) -> dict[str, str]:
        """Injected provider id → API base URL (for Settings endpoint rows)."""
        return dict(self._provider_apis)

    def _auth_file(self) -> Path | None:
        home = getattr(self, "_mimocode_home", None)
        return Path(home) / "data" / "auth.json" if home else None

    def _reconcile_auth_store(self) -> None:
        """Sync provider credentials between app.db (source of truth) and the
        runtime's on-disk auth.json (regenerable cache), newest side wins.

        Runs before every spawn: seeds a fresh/wiped runtime home from the DB;
        adopts a file the DB has never seen (legacy stores); and recovers
        OAuth refreshes written to the file after the last mirror."""
        path = self._auth_file()
        if path is None:
            return
        file_payload: dict | None = None
        file_text: str | None = None
        file_at = 0.0
        try:
            if path.is_file():
                file_text = path.read_text(encoding="utf-8")
                value = json.loads(file_text)
                file_payload = value if isinstance(value, dict) else None
                file_at = path.stat().st_mtime
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("unreadable runtime auth store %s: %s", path, exc)
            file_payload = None
        try:
            sources = self._credential_sources
            if sources is None:
                if file_payload is not None and self._auth_seed_payload:
                    from core.database import SessionLocal
                    from src.model_shares import merge_runtime_auth_changes

                    db = SessionLocal()
                    try:
                        merge_runtime_auth_changes(
                            db,
                            payload=file_payload,
                            baseline=self._auth_seed_payload,
                            provider_sources={
                                provider_id: self._owner
                                for provider_id in (
                                    set(file_payload)
                                    | set(self._auth_seed_payload)
                                )
                            },
                            file_updated_at=file_at,
                        )
                    finally:
                        db.close()
                stored, stored_at = _load_stored_auth(self._owner)
                if (
                    file_payload is not None
                    and (stored is None or file_at > stored_at)
                ):
                    if file_text != stored:
                        _store_auth(self._owner, file_text or "{}")
                    effective = file_payload
                    text = file_text or "{}"
                elif stored:
                    parsed = json.loads(stored)
                    effective = parsed if isinstance(parsed, dict) else {}
                    text = stored
                else:
                    effective = {}
                    text = "{}"
            else:
                from core.database import SessionLocal
                from src.model_shares import (
                    merge_runtime_auth_changes,
                    own_native_auth,
                )

                db = SessionLocal()
                try:
                    if file_payload is not None:
                        merge_runtime_auth_changes(
                            db,
                            payload=file_payload,
                            baseline=self._auth_seed_payload,
                            provider_sources=sources,
                            file_updated_at=file_at,
                        )
                    effective = {
                        provider_id: credential
                        for provider_id, source_owner in sources.items()
                        for credential in [
                            own_native_auth(db, source_owner).get(provider_id)
                        ]
                        if credential is not None
                    }
                finally:
                    db.close()
                text = json.dumps(
                    effective,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            if self._runtime_home is not None and sources is not None:
                manifest = self._runtime_home / "shared-native-auth-sources.json"
                manifest_fd = os.open(
                    manifest,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o600,
                )
                with os.fdopen(manifest_fd, "w", encoding="utf-8") as fh:
                    json.dump(sources, fh, sort_keys=True)
            self._auth_seed_payload = json.loads(text)
            logger.info(
                "provider credentials restored for owner %r (%d trusted share source(s))",
                self._owner,
                len(sources or {}),
            )
        except Exception as exc:
            logger.warning("provider credential reconcile failed: %s", exc)

    def sync_auth_to_db(self) -> None:
        """Mirror the runtime's auth store into app.db (after mutations)."""
        path = self._auth_file()
        if path is None or not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("provider credential store is not an object")
            if (
                self._credential_sources is None
                and not self._auth_seed_payload
            ):
                text = json.dumps(payload)
                _store_auth(self._owner, text)
                self._auth_seed_payload = json.loads(text)
                return
            from core.database import SessionLocal
            from src.model_shares import merge_runtime_auth_changes

            sources = self._credential_sources
            if sources is None:
                sources = {
                    provider_id: self._owner
                    for provider_id in (
                        set(payload) | set(self._auth_seed_payload)
                    )
                }
            db = SessionLocal()
            try:
                merge_runtime_auth_changes(
                    db,
                    payload=payload,
                    baseline=self._auth_seed_payload,
                    provider_sources=sources,
                    file_updated_at=path.stat().st_mtime,
                )
            finally:
                db.close()
            self._auth_seed_payload = json.loads(json.dumps(payload))
        except Exception as exc:
            logger.warning("provider credential mirror failed: %s", exc)

    @property
    def grant_store(self):
        return self._grant_store

    async def refresh_model_catalog(self, *, owner: str | None = None) -> list:
        """Refresh Open Clank agent's authenticated provider/model catalog in-place."""
        if not self._bridge or not self.is_alive():
            raise RuntimeError("mimo ACP is unavailable")
        session_id = await self._bridge.open_session()
        try:
            return self.available_models()
        finally:
            await self.delete_session(session_id)

    async def negotiate_session(
        self,
        session_id: str,
        *,
        owner: str,
        cwd: str | None = None,
    ) -> dict:
        """Refresh one canonical session's negotiated Open Clank agent control plane."""
        if not self._bridge or not self.is_alive():
            raise RuntimeError("mimo ACP is unavailable")
        await self._bridge.ensure_session(session_id, cwd=cwd, owner=owner)
        try:
            for _ in range(10):
                state = self._bridge.negotiated_state(session_id)
                if state.get("commands"):
                    break
                await asyncio.sleep(0.01)
            return dict(state)
        finally:
            await self.delete_session(session_id)

    async def set_session_config(
        self,
        session_id: str,
        config_id: str,
        value: str,
        *,
        owner: str,
        cwd: str | None = None,
    ) -> dict:
        """Acknowledge and persist a typed Open Clank agent session config value."""
        if not self._bridge or not self.is_alive():
            raise RuntimeError("mimo ACP is unavailable")
        try:
            return await self._bridge.set_config_option(
                session_id,
                config_id,
                value,
                cwd=cwd,
                owner=owner,
            )
        finally:
            if session_id in self._bridge.mapped_sessions():
                await self.delete_session(session_id)

    async def delete_session(
        self,
        odysseus_session: str,
        *,
        owner: str | None = None,
        mimo_session_id: str | None = None,
    ) -> None:
        """Delete a Open Clank agent-side session and forget any Open Clank remap."""
        if not self._bridge or not self.is_alive():
            raise RuntimeError("mimo ACP is unavailable")
        mimo_session = mimo_session_id or self._bridge.mapped_session_id(
            odysseus_session
        )
        await self._bridge.cleanup_session(mimo_session)
        if self._client and not self._client.is_closed:
            try:
                await self._client.release_session(mimo_session)
            except Exception as exc:
                logger.warning("failed to release Open Clank agent session MCP clients %s: %s", mimo_session, exc)
        async with self.internal_http_client(timeout=10.0) as client:
            response = await client.delete(f"/session/{quote(mimo_session, safe='')}")
        if response.status_code != 404:
            response.raise_for_status()
        self._bridge.forget_session(odysseus_session)

    def is_alive(self, owner: str | None = None) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def client(self) -> ACPClient | None:
        return self._client

    @property
    def bridge(self) -> ACPBridge | None:
        return self._bridge

    @property
    def question_handler(self):
        return self._bridge.question_handler if self._bridge else None

    @property
    def http_port(self) -> int:
        return self._http_port

    @property
    def http_base_url(self) -> str:
        return f"http://127.0.0.1:{self._http_port}"

    def _configure_internal_http_auth(self, env: dict[str, str]) -> int:
        read_fd, write_fd = os.pipe()
        payload = self.__http_password.encode("utf-8")
        try:
            if os.write(write_fd, payload) != len(payload):
                raise RuntimeError("incomplete Open Clank worker-auth handoff")
        except Exception:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        env["MIMOCODE_SERVER_USERNAME"] = self.__http_username
        env["OPEN_CLANK_WORKER_AUTH_FD"] = str(read_fd)
        return read_fd

    def internal_http_client(self, *, timeout: float = 20.0) -> httpx.AsyncClient:
        """Return a locked-down client for this worker's loopback HTTP API."""
        return httpx.AsyncClient(
            base_url=self.http_base_url,
            auth=httpx.BasicAuth(self.__http_username, self.__http_password),
            follow_redirects=False,
            timeout=timeout,
            trust_env=False,
        )

    async def session_http_request(
        self,
        session_id: str,
        method: str,
        suffix: str,
        *,
        owner: str,
        payload: dict | None = None,
        timeout: float = 20.0,
    ) -> httpx.Response:
        """Call one mapped session API through this worker's private client."""
        if not self._bridge or not self.is_alive():
            raise RuntimeError("mimo ACP is unavailable")
        if session_id not in self._bridge.mapped_sessions():
            await self._bridge.ensure_session(session_id, owner=owner)
        mimo_session = self._bridge.mapped_session_id(session_id)
        workspace = self._bridge.mapped_session_workspace(session_id)
        try:
            from src.openclank.transcript_projection import get_projection

            projection = get_projection(session_id, owner=owner)
            if projection and projection.get("mimo_session_id") == mimo_session:
                workspace = str(projection.get("workspace") or workspace)
        except KeyError:
            pass
        path = f"/session/{quote(mimo_session, safe='')}/{suffix.lstrip('/')}"
        kwargs = {"params": {"directory": workspace}}
        if payload is not None:
            kwargs["json"] = payload
        async with self.internal_http_client(timeout=timeout) as client:
            return await client.request(method, path, **kwargs)


class SupervisorAdmissionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: str = "readiness",
        retryable: bool = True,
        status: int = 503,
        actions: tuple[str, ...] = (),
        details: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.retryable = retryable
        self.status = status
        self.actions = tuple(actions)
        self.details = dict(details or {})

    def as_dict(self) -> dict:
        payload = {
            "code": self.code,
            "error": str(self),
            "phase": self.phase,
            "retryable": self.retryable,
            "status": self.status,
        }
        if self.actions:
            payload["actions"] = list(self.actions)
        if self.details:
            payload["details"] = self.details
        return payload


@dataclass
class _OwnerLifecycle:
    active: MimoSupervisor | None = None
    snapshot: object | None = None
    generation: int = 0
    fence: str = ""
    status: str = "stopped"
    in_flight: dict[object, int] = field(default_factory=dict)
    drain_events: dict[object, asyncio.Event] = field(default_factory=dict)
    breaker_open_until: dict[str, float] = field(default_factory=dict)
    last_failure: str | None = None
    credential_owner: str | None = None


class AgentWorkerLease:
    """One generation-fenced Agent admission; release is idempotent."""

    def __init__(
        self,
        pool,
        owner: str,
        worker,
        *,
        owner_epoch: int,
        projection_pending: bool = False,
    ):
        self._pool = pool
        self.owner = owner
        self.worker = worker
        self._owner_epoch = owner_epoch
        self.projection_pending = projection_pending
        self.generation = getattr(worker, "installed_generation", 0)
        self.fingerprint = getattr(worker, "installed_fingerprint", "")
        self._released = False

    async def release(self, *, successful_terminal: bool = False) -> None:
        if self._released:
            return
        self._released = True
        await self._pool._release_lease(
            self.owner,
            self.worker,
            successful_terminal,
            self._owner_epoch,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.release(successful_terminal=exc_type is None)


class MimoSupervisorPool:
    """Lazy owner-keyed Open Clank agent runtimes; auth-disabled mode keeps one worker."""

    def __init__(
        self,
        *,
        memory_provider=None,
        safe_dirs: list[str] | None = None,
        auth_enabled: bool = True,
        initial_owner: str = "",
        host_provider_owner: str = "",
        data_dir: Path | None = None,
        grant_store=None,
        readiness_budget: float = _READINESS_BUDGET,
        clock=None,
        sleep=None,
    ) -> None:
        self._memory_provider = memory_provider
        self._safe_dirs = safe_dirs
        self._auth_enabled = auth_enabled
        self._initial_owner = self._key(initial_owner) if initial_owner else ""
        self._host_provider_owner = self._key(host_provider_owner) if host_provider_owner else ""
        self._workers: dict[str, MimoSupervisor] = {}
        self._share_workers: dict[str, MimoSupervisor] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._states: dict[str, _OwnerLifecycle] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._background_task_owners: dict[asyncio.Task, str] = {}
        self._owner_lifecycle_epochs: dict[str, int] = {}
        self._owner_lifecycle_blocked: set[str] = set()
        self._readiness_budget = max(0.0, float(readiness_budget))
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        from src.constants import DATA_DIR
        from src.openclank.permission_grants import GrantStore

        root = Path(data_dir) if data_dir is not None else Path(DATA_DIR)
        self._agent_runtime_root = migrate_agent_runtime_root(root)
        self._owners_root = self._agent_runtime_root / "owners"
        self._grant_store = grant_store or GrantStore(str(root / "app.db"))
        # Sync catalogue routes run in Starlette's worker threads, while the
        # supervisor and its ACP child belong to the application event loop.
        # Keep that loop so those routes can marshal owner startup back to the
        # live loop instead of creating a short-lived asyncio.run() loop that
        # leaves a second, orphaned worker behind on the next request.
        self._event_loop: asyncio.AbstractEventLoop | None = None

    @staticmethod
    def _key(owner: str | None) -> str:
        return str(owner or "").strip().lower()

    def _runtime_home(self, owner: str) -> Path:
        digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()
        return self._owners_root / digest

    def _owner_lock(self, owner: str) -> asyncio.Lock:
        lock = self._locks.get(owner)
        if lock is not None:
            try:
                lock._get_loop()
            except RuntimeError:
                # Lock was created in a previous event loop (e.g. after
                # process restart).  Replace with a fresh one for the
                # current loop so _ensure_worker doesn't crash.
                self._locks.pop(owner, None)
                lock = None
        if lock is None:
            lock = asyncio.Lock()
            self._locks[owner] = lock
        return lock

    def _owner_state(self, owner: str) -> _OwnerLifecycle:
        return self._states.setdefault(owner, _OwnerLifecycle())

    def _owner_lifecycle_epoch(self, owner: str) -> int:
        return self._owner_lifecycle_epochs.get(owner, 0)

    def _assert_owner_lifecycle(self, owner: str, epoch: int) -> None:
        if (
            owner in self._owner_lifecycle_blocked
            or self._owner_lifecycle_epoch(owner) != epoch
        ):
            raise SupervisorAdmissionError(
                "SUPERVISOR_UNAVAILABLE",
                "Open Clank account runtime is changing ownership",
                phase="identity",
            )

    def _new_worker(self, owner: str, snapshot, generation: int, fence: str) -> MimoSupervisor:
        runtime_home = None
        if self._auth_enabled:
            runtime_home = self._runtime_home(owner) / "generations" / f"{generation}-{fence}"
        return MimoSupervisor(
            owner=owner,
            memory_provider=self._memory_provider,
            safe_dirs=self._safe_dirs,
            runtime_home=runtime_home,
            partitioned=self._auth_enabled,
            grant_store=self._grant_store,
            projection_snapshot=snapshot,
            projection_generation=generation,
            crash_callback=self._worker_crashed,
        )

    @staticmethod
    def _share_partition(actor_owner: str, share_id: str) -> str:
        return f"@share:{actor_owner}:{share_id}"

    def _new_shared_worker(
        self,
        partition: str,
        access,
        snapshot,
        generation: int,
        fence: str,
    ) -> MimoSupervisor:
        runtime_home = (
            self._runtime_home(partition)
            / "generations"
            / f"{generation}-{fence}"
        )
        credential_sources = {}
        if access.source_kind == "native":
            from src.model_shares import native_provider_id

            credential_sources[native_provider_id(access.model_id)] = (
                access.credential_owner
            )
        worker = MimoSupervisor(
            owner=access.actor_owner,
            memory_provider=self._memory_provider,
            safe_dirs=self._safe_dirs,
            runtime_home=runtime_home,
            partitioned=True,
            grant_store=self._grant_store,
            projection_snapshot=snapshot,
            projection_generation=generation,
            crash_callback=lambda worker, returncode: (
                self._shared_worker_crashed(
                    partition,
                    worker,
                    returncode,
                )
            ),
            credential_sources=credential_sources,
        )
        worker._shared_credential_owner = self._key(access.credential_owner)
        return worker

    async def _start_campaign(
        self,
        owner: str,
        snapshot,
        generation: int,
        fence: str,
        *,
        worker_factory=None,
    ):
        deadline = self._clock() + self._readiness_budget
        delay = _RESTART_DELAY_INITIAL
        last_error: Exception | None = None
        while True:
            candidate = (
                worker_factory()
                if worker_factory is not None
                else self._new_worker(owner, snapshot, generation, fence)
            )
            try:
                await candidate.start()
                candidate.installed_fingerprint = snapshot.fingerprint
                candidate.installed_generation = generation
                return candidate
            except Exception as exc:
                last_error = exc
                await candidate.stop()
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            await self._sleep(min(delay, remaining))
            delay = min(delay * _RESTART_DELAY_MULTIPLIER, _RESTART_DELAY_MAX)
        if isinstance(last_error, SupervisorAdmissionError):
            raise last_error
        raise SupervisorAdmissionError(
            "SUPERVISOR_CRASHLOOP",
            "Open Clank agent readiness campaign exhausted its deadline",
            phase="startup",
        ) from last_error

    async def _ensure_worker(self, owner: str) -> MimoSupervisor:
        from src.openclank.mimo_projection import (
            build_projection_snapshot,
            mark_projection,
            reconcile_projection,
            safe_additive_delta,
        )

        epoch = self._owner_lifecycle_epoch(owner)
        self._assert_owner_lifecycle(owner, epoch)
        lock = self._owner_lock(owner)
        async with lock:
            self._assert_owner_lifecycle(owner, epoch)
            # Coalesce drift until the candidate is built from the newest
            # committed snapshot immediately before publication.
            while True:
                snapshot = build_projection_snapshot(owner)
                desired = reconcile_projection(snapshot, materializing=True)
                generation = int(desired["generation"])
                state = self._owner_state(owner)
                active = state.active
                if (
                    active is not None
                    and active.is_alive()
                    and getattr(active, "installed_fingerprint", "") == snapshot.fingerprint
                    and getattr(active, "installed_generation", 0) == generation
                ):
                    state.status = "ready"
                    return active

                now = self._clock()
                if state.breaker_open_until.get(snapshot.fingerprint, 0) > now:
                    raise SupervisorAdmissionError(
                        "SUPERVISOR_CRASHLOOP",
                        "Open Clank agent readiness breaker is open for this projection",
                        phase="startup",
                    )

                old = active if active is not None and active.is_alive() else None
                old_snapshot = state.snapshot
                state.status = "swapping" if old else "starting"
                fence = uuid.uuid4().hex[:12]
                state.fence = fence
                try:
                    candidate = await self._start_campaign(owner, snapshot, generation, fence)
                except SupervisorAdmissionError as exc:
                    self._assert_owner_lifecycle(owner, epoch)
                    state.breaker_open_until[snapshot.fingerprint] = now + self._readiness_budget
                    state.last_failure = exc.code
                    state.status = "open"
                    mark_projection(
                        owner, snapshot.fingerprint, generation,
                        status="failed", error_code=exc.code,
                    )
                    if old is not None and not (
                        old_snapshot is not None and safe_additive_delta(old_snapshot, snapshot)
                    ):
                        state.active = None
                        self._workers.pop(owner, None)
                        await old.stop()
                    raise

                try:
                    self._assert_owner_lifecycle(owner, epoch)
                except SupervisorAdmissionError:
                    await candidate.stop()
                    raise
                current = build_projection_snapshot(owner)
                current_state = reconcile_projection(current, materializing=True)
                if current.fingerprint != snapshot.fingerprint:
                    await candidate.stop()
                    continue

                candidate.installed_fingerprint = current.fingerprint
                candidate.installed_generation = int(current_state["generation"])
                state.active = candidate
                state.snapshot = current
                state.generation = candidate.installed_generation
                state.status = "ready"
                state.last_failure = None
                self._workers[owner] = candidate
                mark_projection(
                    owner, current.fingerprint, state.generation,
                    status="installed",
                )
                if old is not None and old is not candidate:
                    if old_snapshot is not None and safe_additive_delta(old_snapshot, current):
                        self._schedule_background(
                            self._retire_when_drained(owner, old),
                            owner=owner,
                        )
                    else:
                        await old.stop()
                return candidate

    def _schedule_background(self, coroutine, *, owner: str = "") -> None:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        self._background_task_owners[task] = self._key(owner)

        def _discard(done: asyncio.Task) -> None:
            self._background_tasks.discard(done)
            self._background_task_owners.pop(done, None)

        task.add_done_callback(_discard)

    async def start(self) -> None:
        self._event_loop = asyncio.get_running_loop()
        self._recover_generation_auth_caches()
        self._reclaim_retired_generations()
        if not self._auth_enabled:
            await self.for_owner("")
            return
        owners = {self._initial_owner, self._host_provider_owner} - {""}
        results = await asyncio.gather(
            *(self.for_owner(owner) for owner in owners),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            await self.stop()
            raise errors[0]

    def run_sync(self, coroutine, *, timeout: float | None = None):
        """Run one supervisor coroutine on the application's event loop.

        Model/catalogue endpoints that do not need streaming remain synchronous
        for compatibility, but they must never run supervisor coroutines with
        ``asyncio.run``.  The latter creates a new loop, binds locks/tasks and
        the ACP child to it, then closes the loop as soon as the HTTP handler
        returns.  That race was the source of duplicate owner workers and
        hung resumed turns.  This bridge is intentionally narrow and only
        accepts an already-created coroutine from a sync route.
        """
        loop = self._event_loop
        if loop is None or not loop.is_running():
            raise RuntimeError("Open Clank supervisor event loop is unavailable")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            raise RuntimeError("run_sync cannot block the supervisor event loop")
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        wait_timeout = float(timeout if timeout is not None else max(self._readiness_budget + 5.0, 30.0))
        try:
            return future.result(timeout=wait_timeout)
        except BaseException:
            if not future.done():
                future.cancel()
            raise

    async def for_owner(self, owner: str | None) -> MimoSupervisor:
        key = self._key(owner)
        if self._auth_enabled and not key:
            raise RuntimeError("authenticated Open Clank agent execution requires an owner")
        if not self._auth_enabled:
            key = ""
        return await self._ensure_worker(key)

    async def admit_agent(self, owner: str | None, provider_id: str, model_id: str) -> AgentWorkerLease:
        """Reconcile projection and acquire one generation-scoped Agent lease."""
        from src.openclank.mimo_projection import build_projection_snapshot, safe_additive_delta

        key = self._key(owner)
        if self._auth_enabled and not key:
            raise SupervisorAdmissionError(
                "SUPERVISOR_UNAVAILABLE", "Authenticated Agent execution requires an owner",
                phase="identity", retryable=False,
            )
        if not self._auth_enabled:
            key = ""
        epoch = self._owner_lifecycle_epoch(key)
        self._assert_owner_lifecycle(key, epoch)
        pending = False
        try:
            worker = await self._ensure_worker(key)
        except SupervisorAdmissionError:
            self._assert_owner_lifecycle(key, epoch)
            desired = build_projection_snapshot(key)
            state = self._owner_state(key)
            old = state.active
            if not (
                old is not None and old.is_alive() and state.snapshot is not None
                and safe_additive_delta(state.snapshot, desired)
                and state.snapshot.run_closure(provider_id, model_id) == desired.run_closure(provider_id, model_id)
                and desired.run_closure(provider_id, model_id) is not None
            ):
                raise
            worker = old
            pending = True

        async with self._owner_lock(key):
            self._assert_owner_lifecycle(key, epoch)
            snapshot = getattr(worker, "projection_snapshot", None)
            if snapshot is None or snapshot.run_closure(provider_id, model_id) is None:
                raise SupervisorAdmissionError(
                    "MODEL_NOT_PROJECTED",
                    f"Endpoint model {provider_id}/{model_id} is not in the current Open Clank agent spawn config",
                    phase="routing", retryable=False,
                )
            qualified_model = f"{provider_id}/{model_id}"
            available = {
                str(item.get("modelId"))
                for item in (worker.available_models() or [])
                if item.get("modelId")
            }
            if qualified_model not in available:
                raise SupervisorAdmissionError(
                    "MODEL_NOT_PROJECTED",
                    f"Open Clank agent did not advertise the selected endpoint/model {qualified_model}",
                    phase="catalog", retryable=False,
                )
            state = self._owner_state(key)
            state.in_flight[worker] = state.in_flight.get(worker, 0) + 1
            state.drain_events.setdefault(worker, asyncio.Event()).clear()
            lease = AgentWorkerLease(
                self,
                key,
                worker,
                owner_epoch=epoch,
                projection_pending=pending,
            )
        return lease

    async def admit_shared_agent(
        self,
        access,
        provider_id: str,
        model_id: str,
    ) -> AgentWorkerLease:
        """Acquire one full-capability worker partition for one trusted route."""
        from src.model_shares import resolve_shared_model_access, shared_endpoint_id
        from src.openclank.mimo_projection import build_shared_projection_snapshot

        actor = self._key(access.actor_owner)
        credential_owner = self._key(access.credential_owner)
        partition = self._share_partition(actor, access.share_id)
        actor_epoch = self._owner_lifecycle_epoch(actor)
        source_epoch = self._owner_lifecycle_epoch(credential_owner)
        partition_epoch = self._owner_lifecycle_epoch(partition)
        self._assert_owner_lifecycle(actor, actor_epoch)
        self._assert_owner_lifecycle(credential_owner, source_epoch)
        self._assert_owner_lifecycle(partition, partition_epoch)
        lock = self._owner_lock(partition)
        async with lock:
            self._assert_owner_lifecycle(actor, actor_epoch)
            self._assert_owner_lifecycle(credential_owner, source_epoch)
            self._assert_owner_lifecycle(partition, partition_epoch)
            # Re-resolve under the admission lock so a revoked or retargeted
            # grant cannot reuse a cached worker.
            from core.database import SessionLocal

            db = SessionLocal()
            try:
                current_access = resolve_shared_model_access(
                    db,
                    actor_owner=actor,
                    endpoint_id=shared_endpoint_id(access.share_id),
                    model_id=access.model_id,
                )
            finally:
                db.close()
            if current_access is None:
                raise SupervisorAdmissionError(
                    "SHARED_MODEL_REVOKED",
                    "This shared model is no longer connected to your account",
                    phase="routing",
                    retryable=False,
                    status=403,
                )
            credential_owner = self._key(current_access.credential_owner)
            source_epoch = self._owner_lifecycle_epoch(credential_owner)
            self._assert_owner_lifecycle(credential_owner, source_epoch)
            snapshot = build_shared_projection_snapshot(current_access)
            if snapshot.run_closure(provider_id, model_id) is None:
                raise SupervisorAdmissionError(
                    "SHARED_MODEL_SOURCE_UNAVAILABLE",
                    "The account behind this shared model is disconnected",
                    phase="routing",
                    retryable=False,
                    status=410,
                )
            state = self._owner_state(partition)
            state.credential_owner = credential_owner
            worker = state.active
            if not (
                worker is not None
                and worker.is_alive()
                and worker.installed_fingerprint == snapshot.fingerprint
            ):
                old = worker if worker is not None and worker.is_alive() else None
                generation = (
                    state.generation
                    if state.snapshot is not None
                    and getattr(state.snapshot, "fingerprint", None)
                    == snapshot.fingerprint
                    else state.generation + 1
                )
                generation = max(1, generation)
                fence = uuid.uuid4().hex[:12]
                factory = lambda: self._new_shared_worker(
                    partition,
                    current_access,
                    snapshot,
                    generation,
                    fence,
                )
                worker = await self._start_campaign(
                    partition,
                    snapshot,
                    generation,
                    fence,
                    worker_factory=factory,
                )
                try:
                    self._assert_owner_lifecycle(actor, actor_epoch)
                    self._assert_owner_lifecycle(credential_owner, source_epoch)
                    self._assert_owner_lifecycle(partition, partition_epoch)
                except SupervisorAdmissionError:
                    await worker.stop()
                    raise
                worker.installed_fingerprint = snapshot.fingerprint
                worker.installed_generation = generation
                state.active = worker
                state.snapshot = snapshot
                state.generation = generation
                state.status = "ready"
                self._share_workers[partition] = worker
                if old is not None and old is not worker:
                    self._schedule_background(
                        self._retire_when_drained(partition, old),
                        owner=partition,
                    )

            qualified_model = f"{provider_id}/{model_id}"
            available = {
                str(item.get("modelId"))
                for item in worker.available_models()
                if item.get("modelId")
            }
            if qualified_model not in available:
                raise SupervisorAdmissionError(
                    "MODEL_NOT_PROJECTED",
                    f"Shared model {qualified_model} was not advertised",
                    phase="catalog",
                    retryable=False,
                )
            state.in_flight[worker] = state.in_flight.get(worker, 0) + 1
            state.drain_events.setdefault(worker, asyncio.Event()).clear()
            lease = AgentWorkerLease(
                self,
                partition,
                worker,
                owner_epoch=partition_epoch,
            )
        return lease

    async def _release_lease(
        self,
        owner: str,
        worker,
        successful_terminal: bool,
        owner_epoch: int,
    ) -> None:
        async with self._owner_lock(owner):
            if self._owner_lifecycle_epoch(owner) != owner_epoch:
                return
            state = self._states.get(owner)
            if state is None:
                return
            remaining = max(0, state.in_flight.get(worker, 0) - 1)
            state.in_flight[worker] = remaining
            if remaining == 0:
                state.drain_events.setdefault(worker, asyncio.Event()).set()
            if successful_terminal:
                state.breaker_open_until.pop(getattr(worker, "installed_fingerprint", ""), None)

    async def _retire_when_drained(self, owner: str, worker) -> None:
        async with self._owner_lock(owner):
            state = self._states.get(owner)
            if state is None:
                await worker.stop()
                self._reclaim_generation_dir(owner, worker)
                return
            event = state.drain_events.setdefault(worker, asyncio.Event())
            if state.in_flight.get(worker, 0) == 0:
                event.set()
        await event.wait()
        await worker.stop()
        self._reclaim_generation_dir(owner, worker)

    def _reclaim_generation_dir(self, owner: str, worker) -> None:
        """Remove one retired worker's generation dir; never the active worker's.

        Pre-fix, every projection change orphaned a ``generations/<n>-<fence>``
        tree (each carrying its own mimocode/odysseus auth caches), which
        accumulated hundreds of MB per churning owner. The retire path stopped
        the process but left the dir on disk; this reclaims it.
        """
        raw = getattr(worker, "_runtime_home", None)
        if not raw:
            return
        try:
            path = Path(raw).resolve()
            owners_root = self._owners_root.resolve()
            if owners_root not in path.parents:
                return  # outside our managed tree — refuse to touch it
            state = self._states.get(owner)
            active = state.active if state is not None else None
            active_home = getattr(active, "_runtime_home", None) if active else None
            if active_home is not None and Path(active_home).resolve() == path:
                return  # never reclaim the live worker's dir
            shutil.rmtree(path, ignore_errors=True)
        except Exception as exc:
            logger.warning("generation dir reclaim failed (%s): %s", owner, exc)

    def _reclaim_retired_generations(self) -> None:
        """Startup sweep: drop generation dirs not matching each owner's current
        DB generation. The live worker always carries the DB generation number,
        so keeping every dir whose number equals the active generation preserves
        the live dir (and any duplicate-fence siblings) while reclaiming the
        backlog the pre-fix retire path leaked. Idempotent and safe any time."""
        if not self._owners_root.exists():
            return
        active: dict[str, int] = {}
        try:
            from core.database import MimoProjectionState, SessionLocal
            db = SessionLocal()
            try:
                rows = db.query(MimoProjectionState).all()
                active = {
                    hashlib.sha256(self._key(r.owner_id).encode("utf-8")).hexdigest(): int(r.generation)
                    for r in rows
                }
            finally:
                db.close()
        except Exception as exc:
            logger.warning("generation reclaim skipped (projection state unreadable): %s", exc)
            return
        reclaimed = 0
        for owner_dir in self._owners_root.iterdir():
            gens_root = owner_dir / "generations"
            if not gens_root.is_dir():
                continue
            keep = active.get(owner_dir.name)
            if keep is None:
                # Trusted-share workers are lazy derived partitions and do not
                # own a MimoProjectionState row. Startup already recovered any
                # refreshed native credential above, so every manifest-marked
                # share generation is retired now.
                for gen_dir in gens_root.iterdir():
                    if (
                        gen_dir.is_dir()
                        and (gen_dir / "shared-native-auth-sources.json").is_file()
                    ):
                        shutil.rmtree(gen_dir, ignore_errors=True)
                        reclaimed += 1
                continue
            for gen_dir in gens_root.iterdir():
                if not gen_dir.is_dir():
                    continue
                try:
                    num = int(gen_dir.name.split("-", 1)[0])
                except ValueError:
                    continue
                if num != keep:
                    shutil.rmtree(gen_dir, ignore_errors=True)
                    reclaimed += 1
        if reclaimed:
            logger.info("reclaimed %d retired agent-runtime generation dirs", reclaimed)

    def _recover_generation_auth_caches(self) -> None:
        """Mirror the newest valid stopped-worker auth cache before cleanup."""
        if not self._owners_root.exists():
            return
        try:
            from core.database import MimoAuthStore, MimoProjectionState, SessionLocal
            from src.model_shares import persist_shared_native_auth

            db = SessionLocal()
            try:
                for state in db.query(MimoProjectionState).all():
                    owner = self._key(state.owner_id)
                    owner_root = self._runtime_home(owner).resolve()
                    generations = owner_root / "generations"
                    if not generations.is_dir():
                        continue
                    stored = db.query(MimoAuthStore).filter(
                        MimoAuthStore.owner == owner
                    ).first()
                    stored_at = (
                        stored.updated_at.replace(tzinfo=timezone.utc).timestamp()
                        if stored is not None and stored.updated_at is not None
                        else 0.0
                    )
                    candidates = sorted(
                        generations.glob("*/mimocode/data/auth.json"),
                        key=lambda path: path.stat().st_mtime,
                        reverse=True,
                    )
                    for auth_path in candidates:
                        resolved = auth_path.resolve()
                        if owner_root not in resolved.parents:
                            continue
                        if auth_path.stat().st_mtime <= stored_at:
                            break
                        if auth_path.stat().st_size > 4 * 1024 * 1024:
                            continue
                        try:
                            payload = json.loads(
                                auth_path.read_text(encoding="utf-8")
                            )
                            if not isinstance(payload, dict):
                                continue
                            generation_root = auth_path.parents[2]
                            manifest = (
                                generation_root
                                / "shared-native-auth-sources.json"
                            )
                            sources = {}
                            dedicated = manifest.is_file()
                            if manifest.is_file():
                                raw_sources = json.loads(
                                    manifest.read_text(encoding="utf-8")
                                )
                                if isinstance(raw_sources, dict):
                                    sources = {
                                        str(provider): self._key(source_owner)
                                        for provider, source_owner
                                        in raw_sources.items()
                                        if str(provider).strip()
                                        and self._key(source_owner)
                                    }
                            if dedicated:
                                persist_shared_native_auth(
                                    db,
                                    payload=payload,
                                    shared_sources=sources,
                                )
                            else:
                                row = db.query(MimoAuthStore).filter(
                                    MimoAuthStore.owner == owner
                                ).first()
                                text = json.dumps(
                                    payload,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                )
                                if row is None:
                                    db.add(MimoAuthStore(
                                        owner=owner,
                                        payload=text,
                                    ))
                                else:
                                    row.payload = text
                            db.commit()
                            logger.info(
                                "recovered provider credentials from stopped "
                                "agent generation for owner %r",
                                owner,
                            )
                            break
                        except (OSError, ValueError, TypeError):
                            continue
                owners_root = self._owners_root.resolve()
                for manifest in self._owners_root.glob(
                    "*/generations/*/shared-native-auth-sources.json"
                ):
                    try:
                        resolved_manifest = manifest.resolve()
                        if owners_root not in resolved_manifest.parents:
                            continue
                        auth_path = manifest.parent / "mimocode" / "data" / "auth.json"
                        if (
                            not auth_path.is_file()
                            or auth_path.stat().st_size > 4 * 1024 * 1024
                        ):
                            continue
                        raw_sources = json.loads(
                            manifest.read_text(encoding="utf-8")
                        )
                        payload = json.loads(auth_path.read_text(encoding="utf-8"))
                        if not isinstance(raw_sources, dict) or not isinstance(payload, dict):
                            continue
                        file_at = auth_path.stat().st_mtime
                        fresh_sources = {}
                        for provider_id, source_owner in raw_sources.items():
                            provider_id = str(provider_id or "").strip()
                            source_owner = self._key(source_owner)
                            if not provider_id or provider_id not in payload:
                                continue
                            row = db.query(MimoAuthStore).filter(
                                MimoAuthStore.owner == source_owner
                            ).first()
                            updated = getattr(row, "updated_at", None) if row else None
                            stored_at = (
                                updated.replace(tzinfo=timezone.utc).timestamp()
                                if updated is not None
                                else 0.0
                            )
                            if file_at > stored_at:
                                fresh_sources[provider_id] = source_owner
                        if not fresh_sources:
                            continue
                        persist_shared_native_auth(
                            db,
                            payload=payload,
                            shared_sources=fresh_sources,
                        )
                        db.commit()
                        logger.info(
                            "recovered provider credentials from stopped "
                            "trusted-share generation",
                        )
                    except (OSError, ValueError, TypeError):
                        continue
            finally:
                db.close()
        except Exception as exc:
            logger.warning("generation auth recovery skipped: %s", exc)

    async def _worker_crashed(self, worker, returncode=None) -> None:
        owner = self._key(getattr(worker, "_owner", ""))
        async with self._owner_lock(owner):
            state = self._states.get(owner)
            if state is None:
                return
            if state.active is worker:
                state.active = None
                state.status = "restarting"
                state.last_failure = "IN_FLIGHT_INTERRUPTED"
                self._workers.pop(owner, None)

    async def _shared_worker_crashed(
        self,
        partition: str,
        worker,
        returncode=None,
    ) -> None:
        async with self._owner_lock(partition):
            state = self._states.get(partition)
            if state is None:
                return
            if state.active is worker:
                state.active = None
                state.status = "stopped"
                state.last_failure = "IN_FLIGHT_INTERRUPTED"
                self._share_workers.pop(partition, None)

    def worker_for_owner(self, owner: str | None) -> MimoSupervisor | None:
        key = self._key(owner) if self._auth_enabled else ""
        return self._workers.get(key)

    def _default_worker(self) -> MimoSupervisor | None:
        if self._initial_owner in self._workers:
            return self._workers[self._initial_owner]
        return next(iter(self._workers.values()), None)

    def available_models(self, owner: str | None = None) -> list:
        # A catalogue is an owner capability boundary, not a process-wide
        # inventory.  In authenticated mode an absent/unknown owner must not
        # fall back to the initial worker or merge models from other owners.
        if self._auth_enabled:
            key = self._key(owner)
            if not key:
                return []
            worker = self._workers.get(key)
        else:
            worker = self._workers.get("")
        return worker.available_models() if worker else []

    def provider_apis(self, owner: str | None = None) -> dict[str, str]:
        if self._auth_enabled:
            key = self._key(owner)
            if not key:
                return {}
            worker = self._workers.get(key)
        else:
            worker = self._workers.get("")
        return worker.provider_apis() if worker else {}

    async def refresh_model_catalog(self, *, owner: str | None = None) -> list:
        return await (await self.for_owner(owner)).refresh_model_catalog()

    async def _session_control_worker(
        self,
        session_id: str,
        *,
        owner: str,
    ) -> tuple[MimoSupervisor, AgentWorkerLease | None]:
        """Route session controls through the same personal/share partition as turns."""
        from core.database import Session as DbSession, SessionLocal
        from src.model_shares import (
            resolve_shared_model_access,
            runtime_model_for_access,
            share_id_from_endpoint,
        )

        owner_key = self._key(owner)
        db = SessionLocal()
        try:
            row = db.query(DbSession).filter(DbSession.id == session_id).first()
            if row is None or share_id_from_endpoint(row.endpoint_id) is None:
                return await self.for_owner(owner_key), None
            if self._key(row.owner) != owner_key:
                raise SupervisorAdmissionError(
                    "SHARED_MODEL_REVOKED",
                    "This shared model is no longer connected to your account",
                    phase="routing",
                    retryable=False,
                    status=403,
                )
            access = resolve_shared_model_access(
                db,
                actor_owner=owner_key,
                endpoint_id=row.endpoint_id,
                model_id=row.model,
            )
            runtime_model = (
                runtime_model_for_access(db, access)
                if access is not None
                else None
            )
        finally:
            db.close()
        if access is None or runtime_model is None:
            raise SupervisorAdmissionError(
                "SHARED_MODEL_REVOKED",
                "This shared model is no longer connected to your account",
                phase="routing",
                retryable=False,
                status=403,
            )
        provider_id, separator, model_id = runtime_model.partition("/")
        if not separator:
            raise SupervisorAdmissionError(
                "SHARED_MODEL_SOURCE_UNAVAILABLE",
                "The account behind this shared model is disconnected",
                phase="routing",
                retryable=False,
                status=410,
            )
        lease = await self.admit_shared_agent(
            access,
            provider_id,
            model_id,
        )
        return lease.worker, lease

    async def negotiate_session(self, session_id: str, *, owner: str, cwd: str | None = None) -> dict:
        worker, lease = await self._session_control_worker(
            session_id,
            owner=owner,
        )
        successful = False
        try:
            result = await worker.negotiate_session(
                session_id,
                owner=owner,
                cwd=cwd,
            )
            successful = True
            return result
        finally:
            if lease is not None:
                await lease.release(successful_terminal=successful)

    async def session_http_request(
        self,
        session_id: str,
        method: str,
        suffix: str,
        *,
        owner: str,
        payload: dict | None = None,
        timeout: float = 20.0,
    ) -> httpx.Response:
        worker, lease = await self._session_control_worker(
            session_id,
            owner=owner,
        )
        successful = False
        try:
            response = await worker.session_http_request(
                session_id,
                method,
                suffix,
                owner=owner,
                payload=payload,
                timeout=timeout,
            )
            successful = True
            return response
        finally:
            if lease is not None:
                await lease.release(successful_terminal=successful)

    async def set_session_config(
        self,
        session_id: str,
        config_id: str,
        value: str,
        *,
        owner: str,
        cwd: str | None = None,
    ) -> dict:
        worker, lease = await self._session_control_worker(
            session_id,
            owner=owner,
        )
        successful = False
        try:
            result = await worker.set_session_config(
                session_id,
                config_id,
                value,
                owner=owner,
                cwd=cwd,
            )
            successful = True
            return result
        finally:
            if lease is not None:
                await lease.release(successful_terminal=successful)

    async def delete_session(
        self,
        odysseus_session: str,
        *,
        owner: str | None = None,
        mimo_session_id: str | None = None,
    ) -> None:
        if self._auth_enabled and owner is None:
            raise RuntimeError(
                "authenticated Open Clank agent session deletion requires an owner"
            )
        owner_key = self._key(owner)
        candidates = [
            worker
            for worker in [
                *self._workers.values(),
                *self._share_workers.values(),
            ]
            if not self._auth_enabled
            or self._key(getattr(worker, "_owner", "")) == owner_key
        ]
        worker = next(
            (
                candidate
                for candidate in candidates
                if candidate.bridge
                and (
                    odysseus_session in candidate.bridge.mapped_sessions()
                    or (
                        mimo_session_id is not None
                        and mimo_session_id
                        in candidate.bridge.mapped_sessions().values()
                    )
                )
            ),
            None,
        )
        if worker is None:
            raise RuntimeError("owner Open Clank agent runtime is unavailable")
        await worker.delete_session(
            odysseus_session, mimo_session_id=mimo_session_id
        )

    def mapped_sessions(self, owner: str | None = None) -> dict[str, str]:
        if self._auth_enabled and owner is None:
            return {}
        owner_key = self._key(owner)
        result: dict[str, str] = {}
        for candidate in [
            *self._workers.values(),
            *self._share_workers.values(),
        ]:
            if (
                candidate.bridge
                and (
                    not self._auth_enabled
                    or self._key(getattr(candidate, "_owner", "")) == owner_key
                )
            ):
                result.update(candidate.bridge.mapped_sessions())
        return result

    def permission_handler_for(
        self,
        owner: str | None,
        request_id: str | None = None,
    ):
        owner_key = self._key(owner)
        candidates = [
            worker
            for worker in [
                *self._workers.values(),
                *self._share_workers.values(),
            ]
            if self._key(getattr(worker, "_owner", "")) == owner_key
        ]
        if request_id:
            for worker in candidates:
                handler = worker.permission_handler
                if handler and request_id in handler.pending_requests:
                    return handler
        return candidates[0].permission_handler if candidates else None

    def question_handler_for(
        self,
        owner: str | None,
        request_id: str | None = None,
    ):
        owner_key = self._key(owner)
        candidates = [
            worker
            for worker in [
                *self._workers.values(),
                *self._share_workers.values(),
            ]
            if self._key(getattr(worker, "_owner", "")) == owner_key
        ]
        if request_id:
            for worker in candidates:
                handler = worker.question_handler
                if handler and request_id in handler.pending_requests:
                    return handler
        return candidates[0].question_handler if candidates else None

    def grant_store_for(self, owner: str | None):
        return self._grant_store

    def is_alive(self, owner: str | None = None) -> bool:
        worker = self.worker_for_owner(owner) if owner is not None else None
        if worker:
            return worker.is_alive()
        return any(item.is_alive() for item in self._workers.values())

    @property
    def bridge(self):
        worker = self._default_worker()
        return worker.bridge if worker else None

    @property
    def permission_handler(self):
        worker = self._default_worker()
        return worker.permission_handler if worker else None

    @property
    def http_base_url(self) -> str:
        worker = self._default_worker()
        if worker is None:
            raise RuntimeError("Open Clank agent owner runtime is unavailable")
        return worker.http_base_url

    async def stop(self) -> None:
        workers = {
            worker
            for state in self._states.values()
            for worker in [state.active, *state.in_flight.keys()]
            if worker is not None
        }
        self._workers.clear()
        self._share_workers.clear()
        for state in self._states.values():
            state.active = None
            state.status = "stopped"
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*list(self._background_tasks), return_exceptions=True)
        await asyncio.gather(*(worker.stop() for worker in workers), return_exceptions=True)

    async def _quiesce_owner_locked(self, owner: str) -> None:
        """Stop every worker/task reachable from one owner lifecycle.

        The caller holds the owner lock. That lock also fences a candidate
        campaign because `_ensure_worker` owns it from candidate creation
        through publication.
        """
        state = self._states.get(owner)
        workers = {
            worker
            for worker in [
                self._workers.pop(owner, None),
                self._share_workers.pop(owner, None),
                state.active if state is not None else None,
                *(state.in_flight.keys() if state is not None else ()),
            ]
            if worker is not None
        }
        tasks = [
            task
            for task, task_owner in list(self._background_task_owners.items())
            if task_owner == owner and not task.done()
        ]
        if state is not None:
            state.active = None
            state.status = "stopping"
            for event in state.drain_events.values():
                event.set()
            state.in_flight.clear()
            state.drain_events.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if workers:
            await asyncio.gather(
                *(worker.stop() for worker in workers),
                return_exceptions=True,
            )
        self._states.pop(owner, None)

    def _related_share_partitions(self, owner: str) -> list[str]:
        """Return live trusted-share partitions owned by or sourced from owner."""
        prefix = f"@share:{owner}:"
        partitions = {
            partition
            for partition in (
                set(self._states)
                | set(self._share_workers)
                | set(self._background_task_owners.values())
            )
            if partition.startswith(prefix)
        }
        partitions.update(
            partition
            for partition, state in self._states.items()
            if partition.startswith("@share:")
            and state.credential_owner == owner
        )
        return sorted(partitions)

    def _begin_owner_lifecycle(self, owner: str) -> list[str]:
        keys = [owner, *self._related_share_partitions(owner)]
        for key in keys:
            self._owner_lifecycle_blocked.add(key)
            self._owner_lifecycle_epochs[key] = (
                self._owner_lifecycle_epoch(key) + 1
            )
        return keys

    async def _quiesce_owner_lifecycle(self, keys: list[str]) -> None:
        for key in keys:
            async with self._owner_lock(key):
                await self._quiesce_owner_locked(key)
            if key.startswith("@share:"):
                path = self._runtime_home(key)
                if path.exists():
                    shutil.rmtree(path)

    async def refresh_endpoint_projection(self) -> None:
        """Eager convergence hint; admission-time reconciliation is authoritative."""
        owners = list(self._workers)
        if not owners:
            return
        results = await asyncio.gather(
            *(self._ensure_worker(owner) for owner in owners),
            return_exceptions=True,
        )
        for owner, result in zip(owners, results):
            if isinstance(result, BaseException):
                logger.warning("Open Clank agent reprojection pending for owner %r: %s", owner, result)

    async def revoke_shared_access(
        self,
        actor_owner: str,
        share_id: str,
    ) -> None:
        """Stop and remove one recipient-owned trusted-share partition."""
        partition = self._share_partition(self._key(actor_owner), share_id)
        self._owner_lifecycle_blocked.add(partition)
        self._owner_lifecycle_epochs[partition] = (
            self._owner_lifecycle_epoch(partition) + 1
        )
        try:
            async with self._owner_lock(partition):
                await self._quiesce_owner_locked(partition)
            path = self._runtime_home(partition)
            if path.exists():
                shutil.rmtree(path)
        finally:
            self._owner_lifecycle_blocked.discard(partition)

    async def rename_owner(self, old_owner: str, new_owner: str) -> None:
        old_key = self._key(old_owner)
        new_key = self._key(new_owner)
        if old_key == new_key:
            return
        lifecycle_keys = self._begin_owner_lifecycle(old_key)
        try:
            await self._quiesce_owner_lifecycle(lifecycle_keys)
            async with self._owner_lock(old_key):
                self._grant_store.rename_owner(old_key, new_key)
                old_path = self._runtime_home(old_key)
                new_path = self._runtime_home(new_key)
                if old_path.exists():
                    if new_path.exists():
                        raise RuntimeError("target Open Clank agent owner partition already exists")
                    new_path.parent.mkdir(parents=True, exist_ok=True)
                    old_path.rename(new_path)
                if self._initial_owner == old_key:
                    self._initial_owner = new_key
                if self._host_provider_owner == old_key:
                    self._host_provider_owner = new_key
                try:
                    from core.database import (
                        MimoAuthStore,
                        MimoModelPref,
                        MimoProjectionState,
                        SessionLocal,
                    )
                    db = SessionLocal()
                    try:
                        row = db.query(MimoAuthStore).filter(MimoAuthStore.owner == old_key).first()
                        if row is not None:
                            db.query(MimoAuthStore).filter(MimoAuthStore.owner == new_key).delete()
                            row.owner = new_key
                        projection = (
                            db.query(MimoProjectionState)
                            .filter(MimoProjectionState.owner_id == old_key)
                            .first()
                        )
                        if projection is not None:
                            (
                                db.query(MimoProjectionState)
                                .filter(MimoProjectionState.owner_id == new_key)
                                .delete()
                            )
                            projection.owner_id = new_key
                        db.query(MimoModelPref).filter(MimoModelPref.owner == new_key).delete()
                        for pref in (
                            db.query(MimoModelPref)
                            .filter(MimoModelPref.owner == old_key)
                            .all()
                        ):
                            pref.owner = new_key
                        db.commit()
                    finally:
                        db.close()
                except Exception as exc:
                    logger.warning("provider credential rename failed: %s", exc)
        finally:
            for key in lifecycle_keys:
                self._owner_lifecycle_blocked.discard(key)

    async def purge_owner(self, owner: str) -> None:
        key = self._key(owner)
        lifecycle_keys = self._begin_owner_lifecycle(key)
        try:
            await self._quiesce_owner_lifecycle(lifecycle_keys)
            async with self._owner_lock(key):
                self._grant_store.purge_owner(key)
                path = self._runtime_home(key)
                if path.exists():
                    shutil.rmtree(path)
                try:
                    from core.database import (
                        MimoAuthStore,
                        MimoModelPref,
                        MimoProjectionState,
                        SessionLocal,
                    )
                    db = SessionLocal()
                    try:
                        db.query(MimoAuthStore).filter(MimoAuthStore.owner == key).delete()
                        (
                            db.query(MimoProjectionState)
                            .filter(MimoProjectionState.owner_id == key)
                            .delete()
                        )
                        db.query(MimoModelPref).filter(MimoModelPref.owner == key).delete()
                        db.commit()
                    finally:
                        db.close()
                except Exception as exc:
                    logger.warning("provider credential purge failed: %s", exc)
        finally:
            for lifecycle_key in lifecycle_keys:
                self._owner_lifecycle_blocked.discard(lifecycle_key)
