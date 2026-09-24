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
import sqlite3
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx

from src.openclank.acp_client import ACPClient, TransportError
from src.openclank.acp_bridge import (
    ACPBridge,
    PermissionHandler,
    frankenmemory_child_env,
    register_client_callbacks,
)
from src.openclank.agent_supervisor import AgentSupervisorAdmissionError
from src.memory_scope import chat_workspace
from src.openclank.filesystem_registry import FilesystemRootRegistry

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]


def managed_engine_binary() -> Path:
    """Resolve the verified private engine selected by the build contract."""

    from src.openclank.engine_build import verify_install

    # Startup already performs the executable/ACP smoke.  Worker admission
    # revalidates provenance and bytes without spawning two extra subprocesses
    # for every owner generation.
    verification = verify_install(run_smoke=False, acp_smoke=False).require()
    if verification.binary is None:
        raise RuntimeError("verified Open Clank engine has no executable")
    configured = str(os.environ.get("OPEN_CLANK_ENGINE_BIN") or "").strip()
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if candidate != verification.binary.resolve():
            raise RuntimeError(
                "OPEN_CLANK_ENGINE_BIN does not match the activated verified engine"
            )
    return verification.binary

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
_MODEL_CATALOG_WARMUP_TIMEOUT = 30.0
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


def _mimo_child_environment(owner: str = "") -> dict[str, str]:
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
    # This is a supervisor-to-child handoff, not model-controlled input. The
    # binding is scoped to this owner and is consumed only to construct the
    # shared filesystem mutation context in the child.
    try:
        from src.openclank.files_service_client import history_binding_for

        binding = history_binding_for(owner or "local-installation", "agent")
    except Exception:
        binding = None
    if binding:
        env["OPENCLANK_MIMO_HISTORY_CONTEXT"] = json.dumps(
            {
                "accountId": binding["account_id"],
                "workspaceId": binding.get("workspace_id") or chat_workspace(),
                "workspaceRoot": binding.get("workspace_root") or str(Path.cwd()),
                "socketPath": binding["socket"],
                "token": binding["token"],
            },
            separators=(",", ":"),
        )
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


def _endpoint_registry_providers(
    owner: str = "",
    *,
    shared_access=None,
) -> tuple[dict, dict[str, str]]:
    """Return the normalized, nonsecret engine catalogue during cutover.

    The name remains temporarily import-compatible for callers outside the
    supervisor, but the legacy ``ModelEndpoint`` projection and its synthetic
    identities are gone.  Provider credentials are never returned.  Legacy
    share projections fail closed; current grants are enforced by the managed
    provider-store callbacks on the recipient's ordinary owner worker.
    """
    if shared_access is not None:
        return {}, {}

    from src.openclank.mimo_projection import build_projection_snapshot

    snapshot = build_projection_snapshot(owner)
    providers = dict(snapshot.providers)
    return ({"provider": providers} if providers else {}), {}


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
        local_executor_broker=None,
        lifecycle_epoch: str | None = None,
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
        self._managed_callbacks = None
        self._local_executor_broker = local_executor_broker
        self._lifecycle_epoch = lifecycle_epoch
        # The ACP command already owns an HTTP server. Pin its loopback port so
        # Open Clank can use the private session-control HTTP API without
        # launching a second engine server. Keep it stable across restarts.
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

    def _validate_session_workspace(self, chat_id: str, cwd: str, context: dict) -> str:
        """Approve a mapped chat cwd against the owner filesystem authority."""
        if str(context.get("owner") or "") != str(self._owner):
            raise ValueError("session owner does not match the active host owner")
        if str(context.get("odysseus_session_id") or "") != str(chat_id):
            raise ValueError("session chat identity does not match the host mapping")
        candidate = Path(cwd).expanduser().resolve(strict=False)
        if not candidate.is_dir():
            raise ValueError("session workspace must be an existing directory")
        policy_root = str(
            context.get("file_policy_root") or context.get("file_policy_workspace") or ""
        ).strip()
        if not policy_root or not os.path.isabs(policy_root):
            raise ValueError("session has no stable file-policy workspace binding")
        registry = FilesystemRootRegistry()
        scope = registry.agent_scope(
            self._owner,
            active_workspace=policy_root,
            app_visibility=context.get("app_visibility"),
        )
        active = scope.get("active_folder") or {}
        approved_root = str(active.get("canonical_path") or "")
        if not approved_root:
            raise ValueError("session workspace is outside the owner filesystem policy")
        try:
            candidate.relative_to(Path(approved_root))
        except ValueError as exc:
            raise ValueError("session workspace is outside its stable file-policy workspace") from exc
        if "read" not in (active.get("capabilities") or []):
            raise ValueError("session workspace lacks file-policy read capability")
        return str(candidate)

    async def start(self) -> None:
        """Spawn the child, perform ACP handshake, set up bridge."""
        await self._spawn_and_init()

    async def _spawn_and_init(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            logger.warning("mimo child already running (pid %d)", self._proc.pid)
            return

        engine_binary = managed_engine_binary()
        logger.info(
            "starting Open Clank managed engine: %s acp (http 127.0.0.1:%d)",
            engine_binary,
            self._http_port,
        )

        # Inject Open Clank skills into the embedded agent engine.
        # MIMOCODE_CONFIG_CONTENT is loaded last in mimo's config chain
        # (config.ts L835) and merges on top of everything else.
        # The child receives only the allowlisted runtime shape. Memory calls
        # borrow its owner-scoped lifetools transport, which authenticates to
        # the one app-owned fm-mcp broker instead of spawning another engine.
        env = _mimo_child_environment(self._owner)
        env["OPEN_CLANK_MANAGED"] = "1"
        env["OPEN_CLANK_OWNER"] = str(self._owner or "")
        env["OPEN_CLANK_PROJECT_POLICY_BRIDGE"] = "required"
        http_auth_fd: int | None = None
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
        # model defaults + provider config) and writes sessions/logs into
        # ~/.local/share/mimocode — both directions of that are wrong. Always
        # set MIMOCODE_HOME (the bundled-runtime boundary) to Open Clank's own
        # data dir. Host MIMOCODE_HOME is intentionally ignored. Precedence:
        # OPEN_CLANK_AGENT_HOME > legacy THESIUS_AGENT_HOME > internal default.
        # Provider config and credentials never live in this home; only
        # regenerable engine/session state is permitted.
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
            # ~/.config/mimocode (provider config) or ~/.local/share/mimocode,
            # bleeding a personal install into this owner's runtime.
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
        snapshot = self.projection_snapshot
        if snapshot is None:
            from src.openclank.mimo_projection import build_projection_snapshot
            snapshot = build_projection_snapshot(self._owner)
            self.projection_snapshot = snapshot
            self.installed_fingerprint = snapshot.fingerprint
        merged_providers = dict(snapshot.providers)
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
            logger.info(
                "injected nonsecret host model catalog for owner %s: %s",
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
            inherited_fds = tuple(fd for fd in (http_auth_fd,) if fd is not None)
            if inherited_fds:
                spawn_options["pass_fds"] = inherited_fds
            self._proc = await asyncio.create_subprocess_exec(
                # --print-logs mirrors mimo's own log stream to stderr, which
                # _drain_stderr folds into the host log — ONE log to read
                # instead of chasing per-owner files under data/mimocode.
                str(engine_binary), "acp", "--hostname", "127.0.0.1", "--port", str(self._http_port),
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

        logger.info("mimo child started (pid %d)", self._proc.pid)
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        # Create ACP client on the child's stdio
        assert self._proc.stdin and self._proc.stdout
        self._client = ACPClient(self._proc.stdout, self._proc.stdin)

        # Bind every managed provider callback to this supervisor-owned
        # principal and this exact child generation. Child-supplied owner
        # strings are never consulted by the callback router.
        from src.openclank.provider_callbacks import ManagedProviderCallbacks

        artifact_store = (
            self._local_executor_broker.artifacts
            if self._local_executor_broker is not None
            else None
        )
        self._managed_callbacks = ManagedProviderCallbacks(
            owner=self._owner or "local-installation",
            holder_id=f"pwg_{uuid.uuid4().hex}",
            artifact_store=artifact_store,
            executor_broker=self._local_executor_broker,
        )
        self._managed_callbacks.register(self._client)

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
            managed_provider_context=self._managed_callbacks.resolve_route_context,
            session_workspace_adapter=self._validate_session_workspace,
            lifecycle_epoch=self._lifecycle_epoch,
            session_map_path=(
                (
                    self._runtime_home.parent.parent
                    if self._runtime_home.parent.name == "generations"
                    else self._runtime_home
                ) / "session-map.json"
                if self._runtime_home is not None
                else None
            ),
        )
        self._bridge.set_session_delete_callback(self.delete_session)

        await self._warm_model_catalog()

        # Start health monitor
        self._health_task = asyncio.create_task(self._health_monitor())

    async def _warm_model_catalog(self) -> None:
        """Best-effort catalog handshake without holding web startup hostage."""

        # Mimo only reports availableModels in a session handshake, so open
        # one throwaway session at boot. This is an optimization, not an
        # installation boundary: a real session can populate the same catalog
        # later. Bound it because plugin/provider initialization may be slow or
        # unavailable while the rest of the application is perfectly healthy.
        assert self._bridge is not None
        catalog_session = None
        try:
            catalog_session = await asyncio.wait_for(
                self._bridge.open_session(with_agent_tools=False),
                timeout=_MODEL_CATALOG_WARMUP_TIMEOUT,
            )
            logger.info(
                "mimo model catalog warmed: %d models", len(self._bridge.available_models)
            )
        except asyncio.TimeoutError:
            logger.warning(
                "mimo model catalog warmup timed out after %.0fs; deferring to first session",
                _MODEL_CATALOG_WARMUP_TIMEOUT,
            )
        except Exception as e:
            logger.warning("mimo model catalog warmup failed: %s", e)
        finally:
            if catalog_session:
                try:
                    await self.delete_session(catalog_session)
                except Exception as e:
                    logger.warning("mimo model catalog cleanup failed: %s", e)

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
        # Only this owner worker's persisted map is safe to purge. Canonical
        # transcript rows stay host-owned and are replayed on the next turn.
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
            await self._teardown_child()
            return

        proc = self._proc
        self._proc = None

        if proc.returncode is not None:
            logger.info("mimo child already exited (code %d)", proc.returncode)
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

    async def execute_operation(self, payload: dict) -> dict:
        """Run one non-streaming model operation through managed ACP."""

        if not self._client or not self.is_alive():
            raise RuntimeError("Open Clank managed operation router is unavailable")
        return await self._client.execute_managed_operation(payload)

    async def managed_engine_call(self, method: str, params: dict) -> dict:
        """Invoke one exact host-to-engine managed provider extension."""

        if not self._client or not self.is_alive():
            raise RuntimeError("Open Clank managed provider control is unavailable")
        return await self._client.managed_engine_call(method, params)

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
        for _ in range(10):
            state = self._bridge.negotiated_state(session_id)
            if state.get("commands"):
                break
            await asyncio.sleep(0.01)
        return dict(state)

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
        return await self._bridge.set_config_option(
            session_id,
            config_id,
            value,
            cwd=cwd,
            owner=owner,
        )

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
        path = f"/session/{quote(mimo_session, safe='')}/{suffix.lstrip('/')}"
        kwargs = {"params": {"directory": workspace}}
        if payload is not None:
            kwargs["json"] = payload
        async with self.internal_http_client(timeout=timeout) as client:
            return await client.request(method, path, **kwargs)


class SupervisorAdmissionError(AgentSupervisorAdmissionError):
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
        super().__init__(
            code,
            message,
            phase=phase,
            retryable=retryable,
            status=status,
            actions=actions,
            details=details,
        )
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


# Compatibility name for callers that still import the retired backend class.
# The neutral application seam owns the actual exception identity so errors
# raised before/after the ACP strangler boundary remain catchable by one type.
SupervisorAdmissionError = AgentSupervisorAdmissionError


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
        local_executor_broker=None,
    ) -> None:
        self._memory_provider = memory_provider
        self._safe_dirs = safe_dirs
        self._auth_enabled = auth_enabled
        self._initial_owner = self._key(initial_owner) if initial_owner else ""
        self._host_provider_owner = self._key(host_provider_owner) if host_provider_owner else ""
        self._workers: dict[str, MimoSupervisor] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._states: dict[str, _OwnerLifecycle] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._background_task_owners: dict[asyncio.Task, str] = {}
        self._owner_lifecycle_epochs: dict[str, int] = {}
        self._owner_lifecycle_blocked: set[str] = set()
        self._readiness_budget = max(0.0, float(readiness_budget))
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._local_executor_broker = local_executor_broker
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

    def _owner_epoch_path(self, owner: str) -> Path:
        digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:24]
        return self._owners_root / f".session-map-{digest}.epoch"

    def _durable_owner_epoch(self, owner: str) -> str:
        path = self._owner_epoch_path(owner)
        try:
            value = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return "0"
        if not re.fullmatch(r"(?:0|[1-9][0-9]*)", value):
            raise RuntimeError("owner lifecycle epoch is corrupt")
        return value

    @contextmanager
    def _owner_map_guards(self, owners):
        with ExitStack() as stack:
            for owner in sorted(set(owners)):
                path = self._runtime_home(owner) / "session-map.json"
                mapping = __import__("src.openclank.session_map", fromlist=["OwnerSessionMap"]).OwnerSessionMap(path, owner)
                stack.enter_context(mapping._lock(mapping.lifecycle_lock_path))
            yield

    def _advance_durable_owner_epoch(self, owner: str, value: int) -> str:
        path = self._owner_epoch_path(owner)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
        temporary.write_text(str(value), encoding="utf-8")
        with temporary.open("r+", encoding="utf-8") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        self._fsync_directory(path.parent)
        return str(value)

    def _owner_memory_data_dirs(self, owner: str) -> list[Path]:
        """Return only this owner's managed-engine data roots.

        Authenticated installs are confined to the exact owner's private
        runtime root. Auth-disabled installs have exactly one local runtime,
        so their unpartitioned engine data is that local user's data.
        """
        if not self._auth_enabled:
            candidate = self._agent_runtime_root / "data"
            if candidate.is_symlink():
                raise RuntimeError("agent data root cannot be a symlink")
            return [candidate] if candidate.is_dir() else []
        root = self._runtime_home(owner)
        if root.is_symlink():
            raise RuntimeError("agent owner runtime root cannot be a symlink")
        if not root.exists():
            return []
        candidates = [root / "mimocode" / "data"]
        generations = root / "generations"
        if generations.is_symlink():
            raise RuntimeError("agent generation root cannot be a symlink")
        if generations.is_dir():
            for generation in sorted(generations.iterdir(), key=lambda item: item.name):
                if generation.is_symlink():
                    raise RuntimeError("agent generation cannot be a symlink")
                if generation.is_dir():
                    candidates.append(generation / "mimocode" / "data")
        result: list[Path] = []
        for candidate in candidates:
            if candidate.is_symlink():
                raise RuntimeError("agent data root cannot be a symlink")
            if candidate.is_dir():
                result.append(candidate)
        return result

    @staticmethod
    def _authored_memory_files(data_dir: Path) -> list[Path]:
        memory_root = data_dir / "memory"
        if memory_root.is_symlink():
            raise RuntimeError("agent memory root cannot be a symlink")
        if not memory_root.is_dir():
            return []
        files: list[Path] = []
        for current, dirs, names in os.walk(memory_root, topdown=True, followlinks=False):
            dirs.sort()
            names.sort()
            current_path = Path(current)
            for name in dirs:
                if (current_path / name).is_symlink():
                    raise RuntimeError("agent memory tree contains a symlink")
            for name in names:
                item = current_path / name
                if item.is_symlink() or not item.is_file():
                    raise RuntimeError("agent memory tree contains a non-regular file")
                if item.suffix.casefold() != ".md":
                    continue
                if re.match(r"^memory(?:-|$)", item.stem, re.IGNORECASE):
                    files.append(item)
        return files

    def _agent_memory_snapshot(self, owner: str) -> dict[str, object]:
        keys: list[str] = []
        runtime_root = (
            self._runtime_home(owner)
            if self._auth_enabled
            else self._agent_runtime_root
        )
        for data_dir in self._owner_memory_data_dirs(owner):
            relative_root = data_dir.relative_to(runtime_root).as_posix()
            for item in self._authored_memory_files(data_dir):
                content = item.read_bytes()
                keys.append(
                    "file:"
                    + relative_root
                    + ":"
                    + item.relative_to(data_dir).as_posix()
                    + ":"
                    + hashlib.sha256(content).hexdigest()
                )
            database = data_dir / "mimocode.db"
            if database.is_symlink():
                raise RuntimeError("agent memory database cannot be a symlink")
            if not database.is_file():
                continue
            connection = sqlite3.connect(
                f"file:{database}?mode=ro", uri=True, timeout=2
            )
            try:
                present = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_fts'"
                ).fetchone()
                if not present:
                    continue
                rows = connection.execute(
                    "SELECT id,path,scope,scope_id,type,body,fingerprint,last_indexed_at "
                    "FROM memory_fts WHERE type='memory' ORDER BY id"
                ).fetchall()
                for row in rows:
                    body_hash = hashlib.sha256(str(row[5]).encode("utf-8")).hexdigest()
                    keys.append(
                        "row:"
                        + relative_root
                        + ":"
                        + json.dumps(
                            [row[0], row[1], row[2], row[3], row[4], body_hash, row[6], row[7]],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
            finally:
                connection.close()
        keys.sort()
        encoded = json.dumps(keys, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return {
            "count": len(keys),
            "fingerprint": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        }

    async def preview_owner_memory(self, owner: str) -> dict[str, object]:
        key = self._key(owner)
        if self._auth_enabled and not key:
            raise RuntimeError("authenticated agent-memory reset requires an owner")
        return self._agent_memory_snapshot(key)

    async def reset_owner_memory(
        self,
        owner: str,
        *,
        expected: dict[str, object],
    ) -> dict[str, object]:
        """Clear authored memory files/index rows without deleting sessions."""
        key = self._key(owner)
        if self._auth_enabled and not key:
            raise RuntimeError("authenticated agent-memory reset requires an owner")
        lifecycle_keys = self._begin_owner_lifecycle(key)
        try:
            await self._quiesce_owner_lifecycle(lifecycle_keys)
            async with self._owner_lock(key):
                current = self._agent_memory_snapshot(key)
                if current != expected and int(current.get("count") or 0) != 0:
                    raise RuntimeError("agent memory reset preview is stale")
                if int(current.get("count") or 0) == 0:
                    return {"complete": True, "count": 0}

                staged: list[tuple[Path, Path]] = []
                connections: list[sqlite3.Connection] = []
                try:
                    for data_dir in self._owner_memory_data_dirs(key):
                        for source in self._authored_memory_files(data_dir):
                            tombstone = source.with_name(
                                f".{source.name}.brain-nuke-{uuid.uuid4().hex}"
                            )
                            source.replace(tombstone)
                            staged.append((source, tombstone))
                        database = data_dir / "mimocode.db"
                        if not database.is_file():
                            continue
                        connection = sqlite3.connect(database, timeout=5)
                        connection.execute("PRAGMA foreign_keys=ON")
                        connection.execute("BEGIN IMMEDIATE")
                        connections.append(connection)
                        present = connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_fts'"
                        ).fetchone()
                        if not present:
                            continue
                        connection.execute("DELETE FROM memory_fts WHERE type='memory'")
                        fts_present = connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE name='memory_fts_idx'"
                        ).fetchone()
                        if fts_present:
                            connection.execute(
                                "INSERT INTO memory_fts_idx(memory_fts_idx) VALUES('rebuild')"
                            )
                    for connection in connections:
                        connection.commit()
                except Exception:
                    for connection in connections:
                        try:
                            connection.rollback()
                        except sqlite3.Error:
                            pass
                    for source, tombstone in reversed(staged):
                        if tombstone.exists() and not source.exists():
                            tombstone.replace(source)
                    raise
                finally:
                    for connection in connections:
                        connection.close()

                for _source, tombstone in staged:
                    tombstone.unlink(missing_ok=True)
                after = self._agent_memory_snapshot(key)
                if after.get("count") != 0:
                    raise RuntimeError("agent memory reset left live authored-memory rows")
                return {"complete": True, "count": int(current["count"])}
        finally:
            for lifecycle_key in lifecycle_keys:
                self._owner_lifecycle_blocked.discard(lifecycle_key)

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
        local = self._owner_lifecycle_epochs.get(owner, 0)
        try:
            durable = int(self._durable_owner_epoch(owner))
        except ValueError as exc:
            raise RuntimeError("owner lifecycle epoch is corrupt") from exc
        return max(local, durable)

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

    def _new_worker(self, owner: str, snapshot, generation: int, fence: str, lifecycle_epoch: str | None = None) -> MimoSupervisor:
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
            local_executor_broker=self._local_executor_broker,
            lifecycle_epoch=lifecycle_epoch if lifecycle_epoch is not None else self._durable_owner_epoch(owner),
        )

    async def _start_campaign(
        self,
        owner: str,
        snapshot,
        generation: int,
        fence: str,
        *,
        worker_factory=None,
        lifecycle_epoch: str | None = None,
    ):
        deadline = self._clock() + self._readiness_budget
        delay = _RESTART_DELAY_INITIAL
        last_error: Exception | None = None
        while True:
            candidate = (
                worker_factory()
                if worker_factory is not None
                else self._new_worker(owner, snapshot, generation, fence, lifecycle_epoch)
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
                    candidate = await self._start_campaign(
                        owner, snapshot, generation, fence,
                        lifecycle_epoch=self._durable_owner_epoch(owner),
                    )
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

    def readiness(self) -> dict[str, object]:
        """Return a secret-free readiness snapshot for the owning app."""

        loop_ready = self._event_loop is not None and self._event_loop.is_running()
        owner_states = {
            owner: {
                "status": state.status,
                "generation": state.generation,
                "last_failure": state.last_failure,
            }
            for owner, state in self._states.items()
        }
        workers_alive = all(worker.is_alive() for worker in self._workers.values())
        failed = any(
            state.status in {"failed", "open"}
            for state in self._states.values()
        )
        ready = bool(loop_ready and workers_alive and not failed)
        return {
            "ok": ready,
            "event_loop": loop_ready,
            "owner_workers": len(self._workers),
            # Retained as a non-authoritative compatibility counter. Managed
            # provider grants never create a second worker or credential vault.
            "share_workers": 0,
            "owners": owner_states,
        }

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

    async def execute_operation(self, owner: str | None, payload: dict) -> dict:
        """Run one typed operation on a generation-pinned owner worker."""

        lease = await self.admit_provider_control(owner)
        successful = False
        try:
            result = await lease.worker.managed_engine_call(
                "_openclank/operations/v1/execute",
                payload,
            )
            successful = True
            return result
        finally:
            await lease.release(successful_terminal=successful)

    async def admit_provider_control(self, owner: str | None) -> AgentWorkerLease:
        """Pin one owner worker generation for a provider mutation/OAuth flow."""

        key = self._key(owner)
        if self._auth_enabled and not key:
            raise RuntimeError("authenticated provider control requires an owner")
        if not self._auth_enabled:
            key = ""
        epoch = self._owner_lifecycle_epoch(key)
        self._assert_owner_lifecycle(key, epoch)
        while True:
            worker = await self._ensure_worker(key)
            async with self._owner_lock(key):
                self._assert_owner_lifecycle(key, epoch)
                state = self._owner_state(key)
                if state.active is not worker or not worker.is_alive():
                    continue
                state.in_flight[worker] = state.in_flight.get(worker, 0) + 1
                state.drain_events.setdefault(worker, asyncio.Event()).clear()
                return AgentWorkerLease(
                    self,
                    key,
                    worker,
                    owner_epoch=epoch,
                )

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
        """Fail closed for the retired legacy trusted-share execution path.

        Current ``ProviderShareGrant`` requests execute on the recipient's
        ordinary owner worker.  The turn/operation carries ``grantID`` and the
        owner-bound managed callback validates that capability before every
        bind, credential lease, retry, and refresh.  Constructing a separate
        worker here would recreate the forbidden recipient-side provider vault.
        """
        del access, provider_id, model_id
        raise SupervisorAdmissionError(
            "LEGACY_SHARED_PROVIDER_RETIRED",
            "Legacy shared-provider sessions must be migrated to a provider grant",
            phase="routing",
            retryable=False,
            status=410,
        )

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

        Every projection change can orphan a ``generations/<n>-<fence>`` tree.
        The retire path stops the process, then this method reclaims its
        regenerable runtime directory.
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
        """Remove stopped-worker generations before any new worker starts.

        Projection generations are now deterministic values derived from the
        normalized nonsecret snapshot; there is no mutable projection-state
        table to consult.  At pool startup no generation can be live, and all
        state in these directories is a regenerable engine projection.  This
        also guarantees that a pre-cutover provider credential cache cannot
        remain visible to a newly managed worker.
        """
        if not self._owners_root.exists():
            return
        reclaimed = 0
        for owner_dir in self._owners_root.iterdir():
            gens_root = owner_dir / "generations"
            if not gens_root.is_dir():
                continue
            for gen_dir in gens_root.iterdir():
                if gen_dir.is_dir():
                    shutil.rmtree(gen_dir, ignore_errors=True)
                    reclaimed += 1
        if reclaimed:
            logger.info("reclaimed %d retired agent-runtime generation dirs", reclaimed)

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
        """Route every session control through its recipient/owner worker."""
        del session_id
        return await self.for_owner(self._key(owner)), None

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
            for worker in self._workers.values()
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
        for candidate in self._workers.values():
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
            for worker in self._workers.values()
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
            for worker in self._workers.values()
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

    def _begin_owner_lifecycle_locked(self, owner: str) -> None:
        """Fence one owner while its parent lifecycle guard is held."""
        self._owner_lifecycle_blocked.add(owner)
        next_epoch = self._owner_lifecycle_epoch(owner) + 1
        self._owner_lifecycle_epochs[owner] = next_epoch
        self._advance_durable_owner_epoch(owner, next_epoch)

    def _begin_owner_lifecycle(self, owner: str) -> list[str]:
        keys = [owner]
        with self._owner_map_guards(keys):
            self._begin_owner_lifecycle_locked(owner)
        return keys

    async def _quiesce_owner_lifecycle(self, keys: list[str]) -> None:
        for key in keys:
            async with self._owner_lock(key):
                await self._quiesce_owner_locked(key)

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

    async def invalidate_owner_projection(self, owner: str) -> None:
        """Immediately fence and stop one owner's live Agent generations.

        A narrowing file-policy reset must not leave an already-admitted child
        running under its prior projection while a replacement warms. Runtime
        files and durable approvals are retained; the next admission rebuilds a
        fresh worker from current policy under the incremented lifecycle epoch.
        """
        key = self._key(owner)
        if self._auth_enabled and not key:
            raise RuntimeError("authenticated projection invalidation requires an owner")
        if not self._auth_enabled:
            key = ""
        lifecycle_keys = self._begin_owner_lifecycle(key)
        try:
            await self._quiesce_owner_lifecycle(lifecycle_keys)
        finally:
            for lifecycle_key in lifecycle_keys:
                self._owner_lifecycle_blocked.discard(lifecycle_key)

    async def revoke_shared_access(
        self,
        actor_owner: str,
        share_id: str,
    ) -> None:
        """Compatibility no-op: grant revocation is callback-time authority.

        Every managed attempt revalidates the current ``ProviderShareGrant``
        revision, so revocation applies immediately without killing an owner
        worker or maintaining a recipient credential partition.
        """
        del actor_owner, share_id

    @staticmethod
    def _empty_runtime_inventory(owner: str) -> dict[str, object]:
        encoded = json.dumps([], separators=(",", ":")).encode("utf-8")
        return {
            "schema_version": 1,
            "owner": str(owner),
            "present": False,
            "files": 0,
            "directories": 0,
            "bytes": 0,
            "fingerprint": "sha256:" + hashlib.sha256(encoded).hexdigest(),
            "content_included": False,
        }

    @staticmethod
    def _runtime_inventory_matches(actual: dict, expected: dict) -> bool:
        return all(
            actual.get(key) == expected.get(key)
            for key in (
                "present",
                "files",
                "directories",
                "bytes",
                "fingerprint",
            )
        )

    def _runtime_inventory_at(self, owner: str, root: Path) -> dict[str, object]:
        if not os.path.lexists(root):
            return self._empty_runtime_inventory(owner)
        root_stat = root.lstat()
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise RuntimeError("agent owner runtime root must be a real directory")
        entries: list[list[object]] = []
        file_count = 0
        directory_count = 0
        byte_count = 0
        for current, directories, files in os.walk(root, topdown=True, followlinks=False):
            directories.sort()
            files.sort()
            current_path = Path(current)
            for name in directories:
                path = current_path / name
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise RuntimeError("agent owner runtime contains a non-directory entry")
                relative = path.relative_to(root).as_posix()
                entries.append(["directory", relative])
                directory_count += 1
            for name in files:
                path = current_path / name
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise RuntimeError("agent owner runtime contains a non-regular file")
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    while block := handle.read(1024 * 1024):
                        digest.update(block)
                relative = path.relative_to(root).as_posix()
                entries.append(["file", relative, metadata.st_size, digest.hexdigest()])
                file_count += 1
                byte_count += metadata.st_size
        encoded = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return {
            "schema_version": 1,
            "owner": str(owner),
            "present": True,
            "files": file_count,
            "directories": directory_count,
            "bytes": byte_count,
            "fingerprint": "sha256:" + hashlib.sha256(encoded).hexdigest(),
            "content_included": False,
        }

    @staticmethod
    def _require_real_directory(
        path: Path,
        *,
        label: str,
        create: bool = False,
        create_parents: bool = False,
    ) -> None:
        if not os.path.lexists(path):
            if not create:
                return
            path.mkdir(
                mode=0o700,
                parents=create_parents,
                exist_ok=False,
            )
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"{label} must be a real directory")

    def _validate_owner_lifecycle_roots(self, *, create: bool = False) -> None:
        """Reject link traversal anywhere below the configured data root."""
        self._require_real_directory(
            self._agent_runtime_root.parent,
            label="agent runtime parent",
            create=create,
            create_parents=True,
        )
        self._require_real_directory(
            self._agent_runtime_root,
            label="agent runtime root",
            create=create,
        )
        self._require_real_directory(
            self._owners_root,
            label="agent owners root",
            create=create,
        )

    def _assert_no_owner_purge(self, owner: str) -> None:
        self._validate_owner_lifecycle_roots()
        stage_path, journal_root, journal_path = self._owner_purge_paths(owner)
        if os.path.lexists(journal_root):
            self._require_real_directory(
                journal_root,
                label="Agent lifecycle journal root",
            )
        if os.path.lexists(stage_path) or os.path.lexists(journal_path):
            raise RuntimeError("Agent owner has an unfinished purge lifecycle")

    def owner_lifecycle_inventory(self, owner: str) -> dict[str, object]:
        """Return the content-free Agent runtime/grant account inventory."""
        key = self._key(owner)
        if self._auth_enabled and not key:
            raise RuntimeError("authenticated Agent lifecycle requires an owner")
        self._validate_owner_lifecycle_roots()
        runtime = (
            self._runtime_inventory_at(key, self._runtime_home(key))
            if self._auth_enabled
            else self._runtime_inventory_at(key, self._agent_runtime_root)
        )
        grants = self._grant_store.owner_inventory(key)
        material = json.dumps(
            [
                runtime["present"],
                runtime["files"],
                runtime["directories"],
                runtime["bytes"],
                runtime["fingerprint"],
                grants["count"],
                grants["fingerprint"],
            ],
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "schema_version": 1,
            "owner": key,
            "runtime": runtime,
            "permission_grants": grants,
            # Count the partition root itself. An empty-but-present runtime is
            # still durable owner state and must not disappear from account
            # convergence verification or username-reuse conflict checks.
            "count": (
                int(bool(runtime["present"]))
                + int(runtime["files"])
                + int(runtime["directories"])
                + int(grants["count"])
            ),
            "fingerprint": "sha256:" + hashlib.sha256(material).hexdigest(),
            "content_included": False,
        }

    async def preview_owner_rename(self, old_owner: str, new_owner: str) -> dict[str, object]:
        old_key = self._key(old_owner)
        new_key = self._key(new_owner)
        if not self._auth_enabled or not old_key or not new_key or old_key == new_key:
            raise RuntimeError("distinct authenticated Agent owners are required")
        for lifecycle_owner in (old_key, new_key):
            self._assert_no_owner_purge(lifecycle_owner)
        source = self.owner_lifecycle_inventory(old_key)
        target = self.owner_lifecycle_inventory(new_key)
        if bool(target["runtime"]["present"]) or int(target["permission_grants"]["count"]):
            raise RuntimeError("target Agent owner already contains durable state")
        return {
            "schema_version": 1,
            "source": source,
            "target": target,
            "content_included": False,
        }

    @staticmethod
    def _validate_agent_inventory(
        inventory: dict,
        owner: str,
        *,
        label: str,
    ) -> None:
        if not isinstance(inventory, dict):
            raise RuntimeError(f"invalid Agent {label} owner inventory")
        runtime = inventory.get("runtime") or {}
        grants = inventory.get("permission_grants") or {}
        if (
            inventory.get("schema_version") != 1
            or inventory.get("owner") != owner
            or inventory.get("content_included") is not False
            or not isinstance(runtime, dict)
            or runtime.get("schema_version") != 1
            or runtime.get("owner") != owner
            or runtime.get("content_included") is not False
            or not isinstance(grants, dict)
            or grants.get("schema_version") != 1
            or grants.get("owner") != owner
            or grants.get("content_included") is not False
        ):
            raise RuntimeError(f"invalid Agent {label} owner inventory")

    @classmethod
    def _validate_agent_manifest(
        cls,
        manifest: dict,
        old_owner: str,
        new_owner: str,
    ) -> tuple[dict, dict]:
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != 1
            or manifest.get("content_included") is not False
        ):
            raise RuntimeError("invalid Agent owner lifecycle manifest")
        source = dict(manifest.get("source") or {})
        target = dict(manifest.get("target") or {})
        if not source or not target:
            raise RuntimeError("invalid Agent owner lifecycle manifest")
        cls._validate_agent_inventory(source, old_owner, label="source")
        cls._validate_agent_inventory(target, new_owner, label="target")
        if bool((target.get("runtime") or {}).get("present")) or int(
            (target.get("permission_grants") or {}).get("count") or 0
        ):
            raise RuntimeError("Agent lifecycle target manifest is not empty")
        return source, target

    def _runtime_owner_position(
        self,
        old_key: str,
        new_key: str,
        expected: dict,
    ) -> tuple[str, dict, dict]:
        source = self._runtime_inventory_at(old_key, self._runtime_home(old_key))
        target = self._runtime_inventory_at(new_key, self._runtime_home(new_key))
        if not bool(expected.get("present")):
            if source["present"] or target["present"]:
                raise RuntimeError("Agent runtime state changed after preflight")
            return "empty", source, target
        if self._runtime_inventory_matches(source, expected) and not target["present"]:
            return "source", source, target
        if not source["present"] and self._runtime_inventory_matches(target, expected):
            return "target", source, target
        raise RuntimeError("Agent runtime state changed after preflight")

    async def reconcile_owner_rename(
        self,
        old_owner: str,
        new_owner: str,
        manifest: dict,
    ) -> dict[str, object]:
        """Converge a possibly interrupted runtime/grant owner move."""
        old_key = self._key(old_owner)
        new_key = self._key(new_owner)
        if not self._auth_enabled or not old_key or not new_key or old_key == new_key:
            raise RuntimeError("distinct authenticated Agent owners are required")
        expected, _expected_target = self._validate_agent_manifest(
            manifest,
            old_key,
            new_key,
        )
        for lifecycle_owner in (old_key, new_key):
            self._assert_no_owner_purge(lifecycle_owner)
        lifecycle_keys = [old_key, new_key]
        map_guards = self._owner_map_guards(lifecycle_keys)
        map_guards.__enter__()
        try:
            for key in lifecycle_keys:
                self._begin_owner_lifecycle_locked(key)
            await self._quiesce_owner_lifecycle(lifecycle_keys)
            first, second = sorted((old_key, new_key))
            async with self._owner_lock(first):
                async with self._owner_lock(second):
                    runtime_position, _runtime_source, _runtime_target = self._runtime_owner_position(
                        old_key,
                        new_key,
                        dict(expected.get("runtime") or {}),
                    )
                    grant_manifest = {
                        "schema_version": 1,
                        "source": dict(expected.get("permission_grants") or {}),
                        "target": dict(
                            ((manifest.get("target") or {}).get("permission_grants") or {})
                        ),
                        "content_included": False,
                    }
                    grant_receipt = self._grant_store.reconcile_owner_rename(
                        old_key,
                        new_key,
                        grant_manifest,
                    )
                    if runtime_position == "source":
                        old_path = self._runtime_home(old_key)
                        new_path = self._runtime_home(new_key)
                        new_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        os.replace(old_path, new_path)
                        self._fsync_directory(new_path.parent)
                        runtime_state = "applied"
                    elif runtime_position == "target":
                        runtime_state = "already_applied"
                    else:
                        runtime_state = "empty"
                    runtime_position, runtime_source, runtime_target = self._runtime_owner_position(
                        old_key,
                        new_key,
                        dict(expected.get("runtime") or {}),
                    )
                    if runtime_position not in {"target", "empty"}:
                        raise RuntimeError("Agent runtime owner rename did not converge")
                    if self._initial_owner == old_key:
                        self._initial_owner = new_key
                    if self._host_provider_owner == old_key:
                        self._host_provider_owner = new_key
        finally:
            map_guards.__exit__(None, None, None)
            for key in lifecycle_keys:
                self._owner_lifecycle_blocked.discard(key)
        return {
            "schema_version": 1,
            "state": "applied" if "applied" in {runtime_state, grant_receipt["state"]} else "already_applied",
            "runtime": {
                "state": runtime_state,
                "source": runtime_source,
                "target": runtime_target,
            },
            "permission_grants": grant_receipt,
            "source": self.owner_lifecycle_inventory(old_key),
            "target": self.owner_lifecycle_inventory(new_key),
            "content_included": False,
        }

    async def compensate_owner_rename(
        self,
        old_owner: str,
        new_owner: str,
        manifest: dict,
    ) -> dict[str, object]:
        """Restore complete or component-partial lifecycle state to old_owner."""
        old_key = self._key(old_owner)
        new_key = self._key(new_owner)
        if not self._auth_enabled or not old_key or not new_key or old_key == new_key:
            raise RuntimeError("distinct authenticated Agent owners are required")
        expected, _expected_target = self._validate_agent_manifest(
            manifest,
            old_key,
            new_key,
        )
        for lifecycle_owner in (old_key, new_key):
            self._assert_no_owner_purge(lifecycle_owner)
        lifecycle_keys = [old_key, new_key]
        map_guards = self._owner_map_guards(lifecycle_keys)
        map_guards.__enter__()
        try:
            for key in lifecycle_keys:
                self._begin_owner_lifecycle_locked(key)
            await self._quiesce_owner_lifecycle(lifecycle_keys)
            first, second = sorted((old_key, new_key))
            async with self._owner_lock(first):
                async with self._owner_lock(second):
                    runtime_position, _source, _target = self._runtime_owner_position(
                        old_key,
                        new_key,
                        dict(expected.get("runtime") or {}),
                    )
                    if runtime_position == "target":
                        os.replace(self._runtime_home(new_key), self._runtime_home(old_key))
                        self._fsync_directory(self._owners_root)
                        runtime_state = "compensated"
                    elif runtime_position == "source":
                        runtime_state = "already_compensated"
                    else:
                        runtime_state = "empty"
                    grant_manifest = {
                        "schema_version": 1,
                        "source": dict(expected.get("permission_grants") or {}),
                        "target": dict(
                            ((manifest.get("target") or {}).get("permission_grants") or {})
                        ),
                        "content_included": False,
                    }
                    grant_receipt = self._grant_store.compensate_owner_rename(
                        old_key,
                        new_key,
                        grant_manifest,
                    )
                    runtime_position, runtime_source, runtime_target = self._runtime_owner_position(
                        old_key,
                        new_key,
                        dict(expected.get("runtime") or {}),
                    )
                    if runtime_position not in {"source", "empty"}:
                        raise RuntimeError("Agent runtime compensation did not converge")
                    if self._initial_owner == new_key:
                        self._initial_owner = old_key
                    if self._host_provider_owner == new_key:
                        self._host_provider_owner = old_key
        finally:
            map_guards.__exit__(None, None, None)
            for key in lifecycle_keys:
                self._owner_lifecycle_blocked.discard(key)
        return {
            "schema_version": 1,
            "state": "compensated",
            "runtime": {
                "state": runtime_state,
                "source": runtime_source,
                "target": runtime_target,
            },
            "permission_grants": grant_receipt,
            "source": self.owner_lifecycle_inventory(old_key),
            "target": self.owner_lifecycle_inventory(new_key),
            "content_included": False,
        }

    def _owner_purge_paths(self, owner: str) -> tuple[Path, Path, Path]:
        digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()
        lifecycle_root = self._owners_root / ".lifecycle"
        return (
            self._owners_root / f".purge-{digest}",
            lifecycle_root,
            lifecycle_root / f"purge-{digest}.json",
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _atomic_json(path: Path, value: dict) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            MimoSupervisorPool._fsync_directory(path.parent)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    async def purge_owner_lifecycle(
        self,
        owner: str,
        *,
        expected: dict,
    ) -> dict[str, object]:
        """Physically purge a frozen owner partition with crash replay."""
        key = self._key(owner)
        if not self._auth_enabled or not key:
            raise RuntimeError("authenticated Agent purge requires an owner")
        if not isinstance(expected, dict) or expected.get("schema_version") != 1:
            raise RuntimeError("invalid Agent purge inventory")
        self._validate_agent_inventory(expected, key, label="purge")
        lifecycle_keys = [key]
        map_guards = self._owner_map_guards(lifecycle_keys)
        map_guards.__enter__()
        try:
            for key in lifecycle_keys:
                self._begin_owner_lifecycle_locked(key)
            await self._quiesce_owner_lifecycle(lifecycle_keys)
            async with self._owner_lock(key):
                self._validate_owner_lifecycle_roots(create=True)
                source_path = self._runtime_home(key)
                stage_path, journal_root, journal_path = self._owner_purge_paths(key)
                self._require_real_directory(
                    journal_root,
                    label="Agent lifecycle journal root",
                    create=True,
                )
                source_exists = os.path.lexists(source_path)
                stage_exists = os.path.lexists(stage_path)
                journal_exists = os.path.lexists(journal_path)
                if source_exists and stage_exists:
                    raise RuntimeError("Agent purge source and staging partitions both exist")
                journal = None
                if journal_exists:
                    try:
                        metadata = journal_path.lstat()
                        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                            raise RuntimeError("Agent purge journal is not a regular file")
                        journal = json.loads(journal_path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise RuntimeError("Agent purge journal is unreadable") from exc
                    if (
                        not isinstance(journal, dict)
                        or journal.get("schema_version") != 1
                        or journal.get("owner") != key
                    ):
                        raise RuntimeError("Agent purge journal is invalid")
                    journal_expected = dict(journal.get("expected") or {})
                    self._validate_agent_inventory(
                        journal_expected,
                        key,
                        label="journal",
                    )
                    if journal_expected != expected:
                        raise RuntimeError("Agent purge inventory does not match its journal")
                runtime_expected = dict(expected.get("runtime") or {})
                if source_exists:
                    runtime_before = self._runtime_inventory_at(key, source_path)
                    if not self._runtime_inventory_matches(runtime_before, runtime_expected):
                        raise RuntimeError("Agent runtime changed before purge")
                elif stage_exists:
                    # A crash may have interrupted recursive removal. The
                    # isolated stage is exclusively named by this journal; a
                    # safety traversal still rejects links and special files.
                    self._runtime_inventory_at(key, stage_path)
                    runtime_before = runtime_expected
                else:
                    runtime_before = runtime_expected
                    if bool(runtime_expected.get("present")) and journal is None:
                        raise RuntimeError("Agent runtime disappeared before purge")
                if journal is None:
                    journal = {
                        "schema_version": 1,
                        "owner": key,
                        "expected": expected,
                    }
                    self._atomic_json(journal_path, journal)
                if source_exists:
                    stage_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.replace(source_path, stage_path)
                    self._fsync_directory(stage_path.parent)
                grant_receipt = self._grant_store.purge_owner_lifecycle(
                    key,
                    expected=dict(expected.get("permission_grants") or {}),
                )
                if os.path.lexists(stage_path):
                    self._runtime_inventory_at(key, stage_path)
                    shutil.rmtree(stage_path)
                    self._fsync_directory(stage_path.parent)
                runtime_after = self._runtime_inventory_at(key, source_path)
                if runtime_after["present"]:
                    raise RuntimeError("Agent runtime purge did not converge")
                journal_path.unlink(missing_ok=True)
                self._fsync_directory(journal_root)
                try:
                    journal_root.rmdir()
                except OSError:
                    pass
                self._fsync_directory(journal_root.parent)
        finally:
            map_guards.__exit__(None, None, None)
            for lifecycle_key in lifecycle_keys:
                self._owner_lifecycle_blocked.discard(lifecycle_key)
        return {
            "schema_version": 1,
            "state": "applied",
            "runtime": {"before": runtime_before, "after": runtime_after},
            "permission_grants": grant_receipt,
            "content_included": False,
            "physical_compaction": True,
        }

    async def rename_owner(self, old_owner: str, new_owner: str) -> None:
        manifest = await self.preview_owner_rename(old_owner, new_owner)
        await self.reconcile_owner_rename(old_owner, new_owner, manifest)

    async def purge_owner(self, owner: str) -> None:
        key = self._key(owner)
        stage_path, _journal_root, journal_path = self._owner_purge_paths(key)
        expected = None
        if os.path.lexists(journal_path):
            try:
                metadata = journal_path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise RuntimeError("Agent purge journal is not a regular file")
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                expected = dict(journal.get("expected") or {})
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                expected = None
        if expected is None:
            if os.path.lexists(stage_path):
                raise RuntimeError("unrecognized interrupted Agent purge")
            expected = self.owner_lifecycle_inventory(key)
        await self.purge_owner_lifecycle(key, expected=expected)
