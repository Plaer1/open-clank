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
from urllib.parse import quote, urlencode

import httpx

from src.openclank.acp_client import ACPClient, TransportError
from src.openclank.acp_bridge import (
    ACPBridge,
    PermissionHandler,
    memery_child_env,
    register_client_callbacks,
)
from src.openclank.agent_supervisor import AgentSupervisorAdmissionError
from src.memory_scope import chat_workspace
from src.openclank.filesystem_registry import FilesystemRootRegistry
from src.openclank.orchestration_projection import (
    find_direct_peer, orchestration_read_suffix, session_http_path,
)

logger = logging.getLogger(__name__)


async def _bounded_direct_peer_proof(client, root, target, workspace):
    """Verify the canonical peer actor and direct parent with bounded PK reads."""
    query=urlencode({'limit':1,'target':target})
    response=await client.get(session_http_path(root,f'observation-page/peer?{query}',workspace))
    if response.status_code!=200 or len(response.content)>65536:raise RuntimeError('peer proof unavailable')
    page=response.json()
    if (type(page) is not dict or set(page)!={'items','observedAt'} or type(page['items']) is not list or len(page['items'])!=1
        or type(page['observedAt']) is not int):raise RuntimeError('peer proof invalid')
    item=page['items'][0]
    if (type(item) is not dict or set(item)!={'sessionId','parentSessionId','directory'} or item['sessionId']!=target
        or item['parentSessionId']!=root or type(item['directory']) is not str or not item['directory']):raise RuntimeError('peer proof invalid')
    return {'id':target,'parentID':root,'directory':item['directory']}


async def _queued_owned_thread(fn, *args, **kwargs):
    """Drain a reset authority worker before propagating cancellation."""
    task=asyncio.create_task(asyncio.to_thread(fn,*args,**kwargs));cancelled=False
    while not task.done():
        try: await asyncio.shield(task)
        except asyncio.CancelledError: cancelled=True
    result=task.result()
    if cancelled: raise asyncio.CancelledError
    return result

REPO_ROOT = Path(__file__).resolve().parents[2]


def managed_engine_binary(*, with_verification: bool = False):
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
    return verification if with_verification else verification.binary

# Open Clank skill root — where SkillsManager stores SKILL.md files.
# The bundled runtime's discoverSkills scans cfg.skills.paths for **/SKILL.md.
_OPEN_CLANK_DATA_DIR = (
    os.getenv("OPEN_CLANK_DATA_DIR")
    or str(REPO_ROOT / "data")
)
_OPEN_CLANK_SKILLS_DIR = str(Path(_OPEN_CLANK_DATA_DIR) / "skills")


def _inject_skill_catalog(env: dict[str, str]) -> None:
    """Point an isolated worker at the central, owner-filtered skill catalogue."""
    env["OPEN_CLANK_CONTROL_DATA_DIR"] = _OPEN_CLANK_DATA_DIR
    env["OPENCLANK_CONFIG_CONTENT"] = json.dumps({
        "skills": {"paths": [_OPEN_CLANK_SKILLS_DIR]},
        "memory": {"provider": "memery"},
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
    or "30"
)
_IDLE_WORKER_GRACE = 120.0
_MODEL_CATALOG_WARMUP_TIMEOUT = 30.0
_RESTART_DELAY_INITIAL = 0.25
_RESTART_DELAY_MAX = 5.0
_RESTART_DELAY_MULTIPLIER = 2.0

# Model ids that cannot hold a chat conversation — never a small_model pick.
_NON_CHAT_MODEL_RE = re.compile(r"tts|voice|embed|rerank|image|audio|video|ocr|asr", re.IGNORECASE)


async def _managed_search_delegate(**kwargs):
    """Run the host search seam for one capability-bound callback handoff.

    The callback supplies the already validated owner/caller and a child root;
    this function never reads model-provided authority selectors.  Managed
    MiMo requests remain prepared for the engine, while external searches are
    executed by the existing SearchService implementation.
    """
    from services.search.service import SearchResponse, SearchService
    from src.openclank.operation_router import _thaw_json

    context = kwargs["context"]
    search_input = kwargs.get("input") or {}
    action = kwargs["action"]
    if action == "dispatch":
        deadline_at = float(search_input.get("deadlineAt") or 0.0) / 1000.0
        remaining = deadline_at - time.time()
        if remaining <= 0:
            raise TimeoutError("managed search deadline expired")
        service = SearchService(fetch_content=search_input.get("mode") == "read")
        prepared = service.prepare_search(
            str(search_input.get("query") or ""),
            owner=str(context["owner"]),
            caller=str(context["caller"]),
            mode=str(search_input.get("mode") or "results"),
            count=(int(search_input["count"]) if search_input.get("count") is not None else None),
            freshness=search_input.get("freshness"),
            deadline=min(120.0, remaining),
            locale=str(search_input.get("locale") or "en"),
            extraction=str(search_input.get("extraction") or "none"),
            intent=str(search_input.get("intent") or "web"),
            root_operation_id=kwargs["child_root_operation_id"],
            idempotency_key=kwargs["child_idempotency_key"],
        )
        if isinstance(prepared, SearchResponse):
            # A subscription-unbound early response cannot be represented as
            # an external deployment artifact; preserve it as a typed result.
            kind = "external_result" if prepared.account_scope == "deployment-service" else "finalized"
            return {"kind": kind, "response": prepared.as_dict()}
        if prepared.managed_prepared is not None:
            return {
                "kind": "managed_request",
                "prepared": prepared,
                "preparedRequest": _thaw_json(prepared.managed_prepared.payload),
            }
        response = await service.execute_prepared_external(prepared)
        return {"kind": "external_result", "response": response.as_dict()}
    prepared = kwargs.get("prepared")
    service = SearchService()
    if action == "finalize":
        if prepared is None or prepared.managed_prepared is None:
            raise ValueError("managed search finalization has no prepared operation")
        terminal = kwargs.get("terminal") or {}
        result = prepared.authority.finalize(prepared.managed_prepared, terminal)
        response = service.project_managed(prepared, result)
        return {"kind": "finalized", "response": response.as_dict()}
    if action == "cancel":
        # Engine cancellation is not sent after a managed request starts;
        # callback cancellation is therefore a pre-dispatch terminal receipt.
        return {"kind": "cancelled", "reason": str(kwargs.get("reason") or "aborted")}
    raise ValueError("unsupported managed search delegate action")


def _pick_small_model(providers: dict) -> str | None:
    """First chat-capable model across the injected providers (provider/model).

    Mimo's title/summarize subagents use the configured ``small_model``; left
    unset, its picker grabbed xiaomi's TTS voicedesign model from our injected
    list and every title call 400'd ("system role is not allowed for TTS
    model"). We injected the list, so we pin a sane default. Override with
    OPEN_CLANK_SMALL_MODEL (the injected env config is merged last in the
    bundled runtime's chain, so a file-level small_model would not win).
    """
    override = os.environ.get("OPEN_CLANK_SMALL_MODEL")
    if override:
        return override
    for pid, cfg in providers.items():
        models = cfg.get("models") or {}
        ids = list(models) if isinstance(models, (dict, list, tuple)) else []
        for mid in ids:
            if not _NON_CHAT_MODEL_RE.search(str(mid)):
                return f"{pid}/{mid}"
    return None




def _mimo_child_environment(owner: str = "") -> dict[str, str]:
    """Build from an allowlist so host credentials/config never leak by name."""
    allowed = {
        "COLORTERM",
        "LANG",
        "LANGUAGE",
        "NODE_EXTRA_CA_CERTS",
        "OPEN_CLANK_SHELL_NETWORK",
        "OPEN_CLANK_SHELL_SANDBOX",
        "OPEN_CLANK_RIZIN_EXECUTABLE",
        "OPEN_CLANK_RIZIN_VERSION",
        "OPEN_CLANK_RIZIN_SHA256",
        "OPEN_CLANK_RIZIN_GHIDRA_LIBRARY",
        "OPEN_CLANK_RIZIN_GHIDRA_SHA256",
        "OPEN_CLANK_RIZIN_SLEIGH_HOME",
        "OPEN_CLANK_RIZIN_SLEIGH_MANIFEST",
        "OPEN_CLANK_RIZIN_SLEIGH_MANIFEST_SHA256",
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
    if os.name == "nt":
        # Bun's Windows networking requires this OS runtime locator. Keep
        # the remaining parent environment excluded from the owner worker.
        system_root = os.environ.get("SYSTEMROOT")
        if system_root:
            env["SystemRoot"] = system_root
    env.update(memery_child_env())
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
        env["OPEN_CLANK_ENGINE_HISTORY_CONTEXT"] = json.dumps(
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
        "OPENCLANK_DISABLE_PROVIDER_ENV": "1",
        "OPENCLANK_DISABLE_PROJECT_CONFIG": "1",
        "OPENCLANK_DISABLE_EXTERNAL_SKILLS": "1",
        "OPENCLANK_DISABLE_REMOTE_SKILLS": "1",
        "OPENCLANK_DISABLE_CLAUDE_CODE": "1",
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
        private_runtime: bool = False,
    ) -> None:
        # Immutable configuration only: each call still owns a fresh client
        # and connection pool. Match httpx's default CA verification with no
        # environment-derived overrides.
        self._loopback_ssl_context = httpx.create_ssl_context(verify=True, trust_env=False)
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
        self._private_runtime = private_runtime
        # The ACP command already owns an HTTP server. Pin its loopback port so
        # Open Clank can use the private session-control HTTP API without
        # launching a second engine server. Keep it stable across restarts.
        self._http_port = _loopback_port(
            None
            if partitioned
            else (
                os.environ.get("OPEN_CLANK_AGENT_PORT")
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
            workspace_id=str(context.get("authority_workspace_id") or ""),
            chat_id=str(chat_id),
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

    def _bootstrap_directory(self) -> Path:
        # The engine recognizes its own HOME and skips recursive file indexing
        # there. Real turns receive their authorized workspace through ACP.
        return self._runtime_home / "home" if self._runtime_home is not None else Path.home()

    async def _spawn_and_init(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            logger.warning("mimo child already running (pid %d)", self._proc.pid)
            return

        engine_verification = managed_engine_binary(with_verification=True)
        engine_binary = engine_verification.binary
        logger.info(
            "starting Open Clank managed engine: %s acp (http 127.0.0.1:%d)",
            engine_binary,
            self._http_port,
        )

        # Inject Open Clank skills into the embedded agent engine.
        # OPENCLANK_CONFIG_CONTENT is loaded last in mimo's config chain
        # (config.ts L835) and merges on top of everything else.
        # The child receives only the allowlisted runtime shape. Memory calls
        # borrow its owner-scoped lifetools transport, which authenticates to
        # the one app-owned fm-mcp broker instead of spawning another engine.
        env = _mimo_child_environment(self._owner)
        env["OPEN_CLANK_MANAGED"] = "1"
        if self._private_runtime:
            env["OPEN_CLANK_PRIVATE_RUNTIME"] = "1"
            env["OPENCLANK_DB"] = ":memory:"
        env["OPEN_CLANK_OWNER"] = str(self._owner or "")
        env["OPEN_CLANK_PROJECT_POLICY_BRIDGE"] = "required"
        http_auth_fd: int | None = None
        http_auth_pipe = None
        _inject_skill_catalog(env)
        if os.path.isdir(_OPEN_CLANK_SKILLS_DIR) and not self._partitioned:
            child_data_dir = str(Path(_OPEN_CLANK_SKILLS_DIR).parent)
            env["OPEN_CLANK_DATA_DIR"] = child_data_dir
            logger.info("injected Open Clank skills path: %s", _OPEN_CLANK_SKILLS_DIR)
        elif not os.path.isdir(_OPEN_CLANK_SKILLS_DIR):
            logger.warning("Open Clank skills dir not found: %s", _OPEN_CLANK_SKILLS_DIR)

        # The embedded mimo must NEVER share state with a personal mimocode
        # install: under XDG defaults it reads ~/.config/mimocode (the user's
        # model defaults + provider config) and writes sessions/logs into
        # ~/.local/share/mimocode — both directions of that are wrong. Always
        # set OPENCLANK_HOME (the bundled-runtime boundary) to Open Clank's own
        # data dir. Host OPENCLANK_HOME is intentionally ignored. Precedence:
        # OPEN_CLANK_AGENT_HOME > internal default.
        # Provider config and credentials never live in this home; only
        # regenerable engine/session state is permitted.
        if self._runtime_home is not None:
            self._runtime_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._runtime_home.chmod(0o700)
            private_home = self._bootstrap_directory()
            private_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            private_home.chmod(0o700)
            private_tmp = private_home / "tmp"
            private_tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
            private_tmp.chmod(0o700)
            env["OPENCLANK_HOME"] = str(self._runtime_home / "openclank-engine")
            env["HOME"] = str(private_home)
            env["USERPROFILE"] = str(private_home)
            env["TMPDIR"] = str(private_tmp)
            # Pin EVERY XDG authority to the per-owner home. OPENCLANK_HOME + HOME
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
            _inject_skill_catalog(env)
        elif "OPENCLANK_HOME" not in env:
            _agent_home = os.environ.get("OPEN_CLANK_AGENT_HOME")
            if _agent_home:
                env["OPENCLANK_HOME"] = os.path.join(
                    os.path.expanduser(_agent_home),
                    "runtime",
                    "agent-engine",
                )
            else:
                _data_dir = (
                    os.environ.get("OPEN_CLANK_DATA_DIR")
                    or str(REPO_ROOT / "data")
                )
                env["OPENCLANK_HOME"] = os.path.join(_data_dir, "runtime", "agent-engine")
        snapshot = self.projection_snapshot
        if snapshot is None:
            from src.openclank.mimo_projection import build_projection_snapshot
            snapshot = build_projection_snapshot(self._owner)
            self.projection_snapshot = snapshot
            self.installed_fingerprint = snapshot.fingerprint
        merged_providers = dict(snapshot.providers)
        if merged_providers:
            config_content = json.loads(env.get("OPENCLANK_CONFIG_CONTENT", "{}"))
            config_content.setdefault("provider", {}).update(merged_providers)
            small_model = snapshot.small_model
            if small_model and "small_model" not in config_content:
                config_content["small_model"] = small_model
                logger.info("mimo small_model pinned: %s", small_model)
            env["OPENCLANK_CONFIG_CONTENT"] = json.dumps(config_content)
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
                env.get("OPENCLANK_CONFIG_CONTENT", "{}")
            )
            config_content.setdefault("small_model", snapshot.small_model)
            env["OPENCLANK_CONFIG_CONTENT"] = json.dumps(config_content)
        # Narrow typed profile preferences use the existing owner generation
        # inline layer. Native org/system/MDM/env precedence still applies.
        # A settings change creates a candidate generation and drains old
        # admitted turns through the existing lifecycle, never Config.update.
        if getattr(snapshot, "profile_settings", None):
            config_content = json.loads(env.get("OPENCLANK_CONFIG_CONTENT", "{}"))
            def merge_profile(target, source):
                for key, value in source.items():
                    if isinstance(value, dict):
                        if not isinstance(target.get(key), dict): target[key] = {}
                        merge_profile(target[key], value)
                    else: target[key] = value
            merge_profile(config_content, snapshot.profile_settings)
            env["OPENCLANK_CONFIG_CONTENT"] = json.dumps(config_content)
        for flag, value in getattr(snapshot, "runtime_flags", {}).items():
            if flag not in {"OPENCLANK_NEVER_ASK", "OPENCLANK_ENABLE_TRY_BEST_HANDOFF"} or value not in {"1", "0"}:
                raise RuntimeError("Unsupported owner worker flag")
            env[flag] = value
        env["OPENCLANK_ENABLE_QUESTION_TOOL"] = "1"
        logger.info("mimo child OPENCLANK_HOME: %s", env["OPENCLANK_HOME"])

        try:
            if os.name == "nt":
                from src.openclank.worker_windows import WorkerAuthPipe
                env["OPENCLANK_SERVER_USERNAME"] = self.__http_username
                http_auth_pipe = WorkerAuthPipe(env)
            else:
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
                "cwd": str(self._bootstrap_directory()),
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
            if os.name == "nt":
                from src.openclank.worker_windows import spawn_owned
                async def spawn_worker(*argv, **options):
                    process, self._windows_job = await spawn_owned(*argv, **options)
                    return process
            else:
                spawn_worker = asyncio.create_subprocess_exec
            self._proc = await spawn_worker(
                # --print-logs mirrors mimo's own log stream to stderr, which
                # _drain_stderr folds into the host log — ONE log to read
                # instead of chasing per-owner files under data/mimocode.
                str(engine_binary), "acp", "--hostname", "127.0.0.1", "--port", str(self._http_port),
                "--print-logs",
                **spawn_options,
            )
            if http_auth_pipe is not None:
                await http_auth_pipe.deliver(self._proc, self.__http_password)
        except NotImplementedError as exc:
            # A synchronous fallback cannot safely provide the asyncio stream
            # objects ACPClient requires; the old fallback also leaked a first
            # untracked child before spawning a second one.
            raise RuntimeError("async subprocess support is required for mimo ACP") from exc
        except BaseException:
            # Includes cancelled or failed credential handoff after child creation.
            job = getattr(self, "_windows_job", None)
            if job is not None:
                job.close()
                self._windows_job = None
            if self._proc is not None:
                proc, self._proc = self._proc, None
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await proc.wait()
            raise
        finally:
            if http_auth_pipe is not None:
                http_auth_pipe.close()
            if http_auth_fd is not None:
                os.close(http_auth_fd)

        logger.info("mimo child started (pid %d)", self._proc.pid)
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        # Create ACP client on the child's stdio
        assert self._proc.stdin and self._proc.stdout
        self._client = ACPClient(self._proc.stdout, self._proc.stdin, private_diagnostics=self._private_runtime)

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
        self._managed_callbacks.set_search_delegate(_managed_search_delegate)
        from src.openclank.logging_capture_store import CAPTURE
        await asyncio.to_thread(CAPTURE.recover_generation, self._managed_callbacks.owner, self._managed_callbacks.holder_id)
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
                    if self._private_runtime:
                        logger.warning("private agent permission grant store unavailable")
                    else:
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
            if engine_verification.addon is not None:
                current = managed_engine_binary(with_verification=True)
                if (self._proc is None or self._proc.returncode is not None
                        or current.binary != engine_binary or current.provenance != engine_verification.provenance
                        or current.addon != engine_verification.addon):
                    raise RuntimeError('native artifact authority changed')
            result = await self._client.initialize(gcf_artifact=engine_verification.addon)
            if self._private_runtime:
                logger.info("private agent handshake complete")
            else:
                logger.info("ACP handshake complete: %s", result.get("agentInfo", {}))
        except (TransportError, Exception) as e:
            if self._private_runtime:
                logger.error("private agent handshake failed")
            else:
                logger.error("ACP handshake failed: %s", e)
            await self._teardown_child()
            raise

        # Create the bridge (B2: owner flows through for lifetools MCP context)
        configured_cwd = Path(
            os.environ.get("OPEN_CLANK_GLOBAL_CWD")
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
        self._bridge._s05_foreground_runtime = getattr(self, '_s05_foreground_runtime', None)
        self._bridge._s05_profile = getattr(self, '_s05_profile', None)
        # Bind bridge-derived search context to this exact callback generation.
        # The callback recipe setter will consume this seam when installed.
        self._bridge.set_managed_provider_generation(self._managed_callbacks.holder_id)
        if self._private_runtime:
            def private_ghost_is_current(chat_id, owner, holder_id):
                from core.models import get_session_manager_instance
                from core.database import SessionLocal, Session as DBSession
                manager = get_session_manager_instance()
                if (self._stopping or owner != self._owner or holder_id != self._managed_callbacks.holder_id
                    or chat_id != getattr(self, "_private_ghost_id", None)
                    or getattr(manager, "sessions", {}).get(chat_id) is not getattr(self, "_private_ghost", None)
                    or not getattr(self._private_ghost, "incognito", False) or self._private_ghost.owner != owner):
                    return False
                db = SessionLocal()
                try:
                    return db.query(DBSession.id).filter(DBSession.id == chat_id).first() is None
                finally:
                    db.close()
            self._bridge._private_ghost_is_current = private_ghost_is_current
        self._bridge.set_managed_actor_execution_reader(self._read_managed_actor_tree)
        self._bridge.set_managed_root_actor_reader(self._read_managed_root_actors)
        policy_installer = getattr(self, "_managed_policy_installer", None)
        if policy_installer is not None:
            policy_installer(self._bridge)
        self._managed_callbacks.set_search_context_resolver(
            self._bridge.resolve_managed_search_context
        )
        self._managed_callbacks.set_actor_execution_context_resolver(
            self._bridge.resolve_managed_actor_execution
        )
        self._bridge._logging_callbacks = self._managed_callbacks
        self._bridge.set_session_delete_callback(self.delete_session)

        # Ordinary restart recovers SQLite ↔ session-map crash-seam candidates
        # before any later remap or new admission can observe them.
        if not self._private_runtime:
            try:
                recovered = await self._bridge.recover_admission_orphans()
                if recovered:
                    logger.info("recovered managed admission orphans: %s", recovered)
            except Exception as exc:
                logger.warning("managed admission orphan recovery failed: %s", exc)

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
                self._bridge.open_session(cwd=str(self._bootstrap_directory()), with_agent_tools=False),
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
            if self._private_runtime:
                logger.warning("private agent catalog warmup failed")
            else:
                logger.warning("mimo model catalog warmup failed: %s", e)
        finally:
            if catalog_session:
                try:
                    await self.delete_session(catalog_session)
                except Exception as e:
                    if self._private_runtime:
                        logger.warning("private agent catalog cleanup failed")
                    else:
                        logger.warning("mimo model catalog cleanup failed: %s", e)

    async def _drain_stderr(self) -> None:
        """Fold the child's stderr — mimo's log stream under --print-logs —
        into the host log at the line's own severity, so app.log is the one
        place to look. Must never die while the child lives: an undrained
        stderr pipe fills up and blocks the child mid-turn."""
        assert self._proc and self._proc.stderr
        try:
            async for raw in self._proc.stderr:
                if self._private_runtime:
                    continue
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
            if self._private_runtime:
                logger.error("private agent stderr drain failed")
            else:
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
        if os.name != "nt" and returncode in (-signal.SIGINT, -signal.SIGTERM):
            await asyncio.sleep(2.0)
            if self._stopping:
                return

        if self._private_runtime and self._managed_callbacks is not None:
            try:
                await asyncio.to_thread(self._managed_callbacks.close_provider_generation)
            except Exception:
                logger.error("private agent credential revocation failed")
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
        if self._bridge is not None:
            await self._bridge._retire_python_kernels()
        private_revocation_failed = False
        if self._private_runtime and self._managed_callbacks is not None:
            try:
                await asyncio.to_thread(self._managed_callbacks.close_provider_generation)
            except Exception:
                private_revocation_failed = True
                logger.error("private agent credential revocation failed")
        if self._bridge:
            try:
                await self._bridge.terminal_manager.close()
            except Exception as exc:
                if self._private_runtime:
                    logger.warning("private agent terminal cleanup failed")
                else:
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

        job = getattr(self, "_windows_job", None)
        if job is not None:
            job.close()
            self._windows_job = None

        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None

        if self._proc:
            proc = self._proc
            if not self._private_runtime:
                self._proc = None
            if proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    if self._private_runtime:
                        raise RuntimeError("private agent process cleanup unavailable")
            if self._private_runtime:
                if proc.returncode is None:
                    raise RuntimeError("private agent process cleanup unavailable")
                self._proc = None

        if self._managed_callbacks is not None:
            self._managed_callbacks.close_search_handoffs()
            try:
                await asyncio.to_thread(self._managed_callbacks.teardown_logging)
            except Exception as exc:
                logger.warning("managed logging teardown unavailable: %s", type(exc).__name__)
            if not private_revocation_failed:
                self._managed_callbacks = None
        self._client = None
        self._bridge = None
        if private_revocation_failed:
            raise RuntimeError("private agent credential revocation unavailable")

    async def stop(self) -> None:
        """Graceful shutdown — stop health monitor, close client, kill child."""
        self._stopping = True

        if self._private_runtime:
            async def stop_private():
                try:
                    if self._client:
                        await self._client.close()
                finally:
                    await self._teardown_child()
            task = asyncio.create_task(stop_private())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                try:
                    await task
                except Exception:
                    logger.warning("private agent cleanup unavailable")
                raise
            return

        revocation_failed = False
        if self._private_runtime and self._managed_callbacks is not None:
            try:
                await asyncio.to_thread(self._managed_callbacks.close_provider_generation)
            except Exception:
                revocation_failed = True
                logger.error("private agent credential revocation failed")

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
            if revocation_failed:
                raise RuntimeError("private agent credential revocation unavailable")
            return

        proc = self._proc
        self._proc = None

        if proc.returncode is not None:
            logger.info("mimo child already exited (code %d)", proc.returncode)
            await self._teardown_child()
            if revocation_failed:
                raise RuntimeError("private agent credential revocation unavailable")
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
            job = getattr(self, "_windows_job", None)
            if job is not None:
                job.close()
                self._windows_job = None
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.wait()
        finally:
            job = getattr(self, "_windows_job", None)
            if job is not None:
                job.close()
                self._windows_job = None

        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        if self._private_runtime:
            await self._teardown_child()
        if revocation_failed:
            raise RuntimeError("private agent credential revocation unavailable")

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
        session_id = await self._bridge.open_session(
            cwd=str(self._bootstrap_directory()), with_agent_tools=False
        )
        try:
            return self.available_models()
        finally:
            await self.delete_session(session_id)

    async def execute_operation(self, payload: dict) -> dict:
        """Run one non-streaming model operation through managed ACP."""

        if not self._client or not self.is_alive():
            raise RuntimeError("Open Clank managed operation router is unavailable")
        root = str(payload.get("rootOperationID") or "")
        admission_gap = False
        callbacks = self._managed_callbacks
        if callbacks is not None:
            try:
                await asyncio.to_thread(callbacks.register_logging_operation, root,
                    {"operation_type": payload.get("operation"), "actor_kind": "internal",
                     "incognito": bool((payload.get("options") or {}).get("incognito")),
                     "allowed_routes": payload.get("routes") or []})
            except Exception as exc:
                admission_gap = True
                logger.warning("managed operation logging unavailable: %s", type(exc).__name__)
        try:
            result = await self._client.execute_managed_operation(payload)
            if admission_gap:
                result = {**result, "identityCoverage": "partial",
                          "lossReasons": list(dict.fromkeys([*(result.get("lossReasons") or []), "admission_unavailable"]))}
            return result
        finally:
            if callbacks is not None:
                try:
                    await asyncio.to_thread(callbacks.finish_logging_operation, root)
                except Exception as exc:
                    logger.warning("managed operation logging finalization unavailable: %s", type(exc).__name__)

    async def execute_prepared_option_operation(self, payload: dict, *, utility=None) -> dict:
        """Pin exact host-prepared auxiliary options without root capture ownership."""
        from src.openclank.operation_journal import canonical_request_hash
        callbacks = self._managed_callbacks
        if callbacks is None or not self._client or not self.is_alive():
            raise RuntimeError("prepared managed operation authority is unavailable")
        root = payload["rootOperationID"]
        key = payload["idempotencyKey"]
        routes = payload["routes"]
        if not isinstance(routes, list) or len(routes) != 1:
            raise RuntimeError("prepared managed operation requires one route")
        route = {name: routes[0][name] for name in
                 ("connectionID", "providerID", "billingLane", "modelRouteID", "modelID", "grantID")
                 if name in routes[0]}
        journal_request = {name: payload[name] for name in ("operation", "routes", "input", "artifactInputs", "options")}
        registration = callbacks.register_prepared_operation(root, key, canonical_request_hash(journal_request),
            payload["options"]["modelRequestOptions"], route)
        try:
            return await self.execute_cancellable_operation(payload, utility=utility)
        finally:
            cleanup = asyncio.create_task(asyncio.to_thread(callbacks.retire_prepared_operation, root, key, registration))
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            try:
                cleanup_ok = cleanup.result()
            except BaseException:
                if utility is not None:
                    utility.host_cleanup_unknown = True
                raise
            if not cleanup_ok:
                if utility is not None:
                    utility.host_cleanup_unknown = True
                raise RuntimeError("prepared managed operation retirement failed")
            if cancelled:
                raise asyncio.CancelledError

    def supports_provider_method_policy(self) -> bool:
        """Read the capability from this live worker’s validated handshake."""
        from src.openclank.managed_protocol import ManagedProtocolError, validate_initialize_result

        client = self._client
        if client is None or client.is_closed:
            return False
        try:
            capabilities = validate_initialize_result(client.initialize_result, private_runtime=self._private_runtime)
        except ManagedProtocolError:
            return False
        return capabilities.providerMethodPolicyContract == 1

    async def managed_engine_call(self, method: str, params: dict, *, queued_pending=None) -> dict:
        """Invoke one exact host-to-engine managed provider extension."""

        if not self._client or not self.is_alive():
            raise RuntimeError("Open Clank managed provider control is unavailable")
        return await self._client.managed_engine_call(method, params, queued_pending=queued_pending) if queued_pending is not None else await self._client.managed_engine_call(method, params)

    async def execute_cancellable_operation(self, payload: dict, *, utility=None) -> dict:
        from src.openclank.managed_protocol import validate_engine_method_request, validate_engine_method_result
        if not self._client or not self.is_alive():
            raise RuntimeError("Open Clank managed provider control is unavailable")
        method = "_openclank/operations/v1/execute"
        request = validate_engine_method_request(method, payload)
        return validate_engine_method_result(method, await self._client.execute_managed_operation(request, utility=utility))

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
        _chat_guard=None,
    ) -> None:
        """Delete a Open Clank agent-side session and forget any Open Clank remap."""
        if not self._bridge or not self.is_alive():
            raise RuntimeError("mimo ACP is unavailable")
        if _chat_guard is not None:
            self._bridge._durable_session_map.validate_chat_guard(odysseus_session, _chat_guard)
            current = await self._bridge._durable_session_map.async_lookup(odysseus_session)
            if not current or current.get("current") != mimo_session_id:
                raise RuntimeError("managed session binding changed before deletion")
        mimo_session = mimo_session_id or self._bridge.mapped_session_id(
            odysseus_session
        )
        await self._bridge.cleanup_session(mimo_session)
        if self._client and not self._client.is_closed:
            try:
                await self._client.release_session(mimo_session)
            except Exception as exc:
                if self._private_runtime:
                    logger.warning("private agent session MCP cleanup failed")
                else:
                    logger.warning("failed to release Open Clank agent session MCP clients %s: %s", mimo_session, exc)
        async with self.internal_http_client(timeout=10.0) as client:
            response = await client.delete(f"/session/{quote(mimo_session, safe='')}")
        if response.status_code != 404:
            response.raise_for_status()
        await self._bridge.forget_session_async(odysseus_session, _chat_guard=_chat_guard)

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
        env["OPENCLANK_SERVER_USERNAME"] = self.__http_username
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
            verify=self._loopback_ssl_context,
        )

    async def _read_managed_root_actors(self, root_session_id: str, *, private_binding=None) -> dict:
        """Fresh root-only actor snapshot; no descendant or cached authority."""
        if not self._bridge or not self.is_alive():
            raise RuntimeError("managed actor registry is unavailable")
        bridge = self._bridge
        root = str(root_session_id or "").strip()
        context = bridge._session_context.get(root) or {}
        chat_id = str(context.get("odysseus_session_id") or "").strip()
        process = self._proc; generation = self.installed_generation
        holder = bridge._managed_provider_generation; turn = bridge._turns.get(root)
        if private_binding is not None:
            authority = private_binding.get('authority') if isinstance(private_binding, dict) else None
            if (not self._private_runtime or not root or not chat_id or not isinstance(authority, dict)
                    or authority.get('kind') != 'private' or context.get('private_authority') != authority
                    or private_binding.get('engineSessionID') != root or private_binding.get('stableChatID') != chat_id
                    or private_binding.get('owner') != bridge._owner or not holder or turn is None
                    or await bridge._handle_session_binding_read({'sessionID': root}) != private_binding):
                raise RuntimeError('private actor root unavailable')
            workspace = private_binding.get('canonicalCwd')
            if not isinstance(workspace, str) or not workspace or context.get('physical_cwd') != workspace:
                raise RuntimeError('private actor workspace unavailable')
        else:
            if not root or not chat_id or bridge.mapped_session_id(chat_id) != root:
                raise RuntimeError("managed actor root is not the current mapped session")
            workspace = bridge.mapped_session_workspace(chat_id)
        async with self.internal_http_client(timeout=8.0) as client:
            response = await client.get(session_http_path(root, "actors", workspace))
            response.raise_for_status()
            from src.openclank.orchestration_projection import validate_actors
            actors = validate_actors(response.json(), root)
        if (self._bridge is not bridge or not self.is_alive()
                or bridge._session_context.get(root) is not context
                or sum(actor.get('actorID') == 'main' for actor in actors) != 1):
            raise RuntimeError("managed root actor snapshot changed")
        if private_binding is not None:
            if (len(actors) != 1 or bridge._managed_provider_generation != holder or bridge._turns.get(root) is not turn
                    or await bridge._handle_session_binding_read({'sessionID': root}) != private_binding
                    or self._bridge is not bridge or not self.is_alive() or self._proc is not process
                    or self.installed_generation != generation or bridge._session_context.get(root) is not context
                    or bridge._managed_provider_generation != holder or bridge._turns.get(root) is not turn):
                raise RuntimeError('private actor root changed')
        elif bridge.mapped_session_id(chat_id) != root or bridge.mapped_session_workspace(chat_id) != workspace:
            raise RuntimeError("managed root actor snapshot changed")
        return {"rootSessionID": root, "sessions": {root: {"sessionID": root, "parentID": None, "actors": actors}}}

    async def _read_managed_actor_tree(self, root_session_id: str) -> dict:
        """Read the complete private native session/actor tree for one mapped root.

        The public inspector intentionally asks for visible peer sessions only.
        Managed execution authorization must also see internal subagent hosts,
        so this private read omits the visibility filter and validates every
        parent edge returned by the engine.
        """
        if not self._bridge or not self.is_alive():
            raise RuntimeError("managed actor registry is unavailable")
        root = str(root_session_id or "").strip()
        context = self._bridge._session_context.get(root) or {}
        chat_id = str(context.get("odysseus_session_id") or "").strip()
        if not root or not chat_id or self._bridge.mapped_session_id(chat_id) != root:
            raise RuntimeError("managed actor root is not the current mapped session")
        workspace = self._bridge.mapped_session_workspace(chat_id)
        sessions: dict[str, dict] = {}
        pending: list[tuple[str, str | None, int]] = [(root, None, 0)]
        async with self.internal_http_client(timeout=8.0) as client:
            while pending:
                session_id, expected_parent, depth = pending.pop(0)
                if session_id in sessions:
                    raise RuntimeError("managed actor tree contains a repeated session")
                if depth > 16 or len(sessions) >= 512:
                    raise RuntimeError("managed actor tree exceeds its bounded read limit")
                actors_response = await client.get(
                    session_http_path(session_id, "actors", workspace)
                )
                actors_response.raise_for_status()
                actors = actors_response.json()
                from src.openclank.orchestration_projection import validate_actors

                actors = validate_actors(actors, session_id)
                sessions[session_id] = {
                    "sessionID": session_id,
                    "parentID": expected_parent,
                    "actors": actors,
                }
                children_response = await client.get(
                    session_http_path(session_id, "children", workspace)
                )
                children_response.raise_for_status()
                children = children_response.json()
                if not isinstance(children, list):
                    raise RuntimeError("managed engine returned invalid child membership data")
                for child in children:
                    if not isinstance(child, dict):
                        raise RuntimeError("managed engine returned invalid child membership data")
                    child_id = child.get("id")
                    if not isinstance(child_id, str) or not child_id:
                        raise RuntimeError("managed engine returned invalid child identity")
                    if find_direct_peer([child], session_id, child_id) is None:
                        raise RuntimeError("managed engine returned a child with the wrong parent")
                    pending.append((child_id, session_id, depth + 1))
        return {"rootSessionID": root, "sessions": sessions}

    async def prepare_managed_handoff(self, **identity) -> dict:
        bridge = self._bridge
        if bridge is None or not self.is_alive():
            raise RuntimeError("managed handoff runtime is unavailable")
        return await bridge.prepare_managed_handoff(**identity)

    async def clear_managed_handoff(self, **identity) -> bool:
        bridge = self._bridge
        if bridge is None:
            return False
        return await bridge.clear_managed_handoff(**identity)

    async def session_http_request(
        self,
        session_id: str,
        method: str,
        suffix: str,
        *,
        owner: str,
        payload: dict | None = None,
        timeout: float = 20.0,
        before_issue=None,
    ) -> httpx.Response:
        """Call one mapped session API through this worker's private client."""
        if not self._bridge or not self.is_alive():
            raise RuntimeError("mimo ACP is unavailable")
        if session_id not in self._bridge.mapped_sessions():
            await self._bridge.ensure_session(session_id, owner=owner)
        mimo_session = self._bridge.mapped_session_id(session_id)
        workspace = self._bridge.mapped_session_workspace(session_id)
        path = session_http_path(mimo_session, suffix, workspace)
        if payload is not None:
            kwargs = {"json": payload}
        else:
            kwargs = {}
        async with self.internal_http_client(timeout=timeout) as client:
            if before_issue is not None:
                import inspect
                if not callable(before_issue):
                    raise ValueError("session request authority changed before issue")
                checked = before_issue()
                if inspect.isawaitable(checked):
                    checked = await checked
                if checked is not True:
                    raise ValueError("session request authority changed before issue")
            return await client.request(method, path, **kwargs)

    async def session_terminal_diagnostic_source(self, host_chat_id, native_session_id, message_id, *, owner, timeout=5.0):
        """Read one fixed diagnostic projection on this already-mapped live worker."""
        bridge=self._bridge; process=self._proc
        if (not bridge or not process or not self.is_alive() or bridge._owner!=owner or host_chat_id not in bridge.mapped_sessions(owner=owner)
                or not isinstance(message_id,str) or not message_id or len(message_id.encode('utf-8'))>256): raise RuntimeError('terminal diagnostic source unavailable')
        root=bridge.mapped_session_id(host_chat_id); workspace=bridge.mapped_session_workspace(host_chat_id); holder=bridge._managed_provider_generation
        if root!=native_session_id or not holder: raise RuntimeError('terminal diagnostic source unavailable')
        path=session_http_path(root,'diagnostic-message/'+quote(message_id,safe=''),workspace)
        async with self.internal_http_client(timeout=timeout) as client:
            async with client.stream('GET',path) as response:
                if response.status_code!=200: raise RuntimeError('terminal diagnostic source unavailable')
                chunks=[]; size=0
                async for chunk in response.aiter_bytes():
                    size+=len(chunk)
                    if size>8192: raise RuntimeError('terminal diagnostic source unavailable')
                    chunks.append(chunk)
        if (self._bridge is not bridge or self._proc is not process or not self.is_alive() or bridge._managed_provider_generation!=holder
                or host_chat_id not in bridge.mapped_sessions(owner=owner) or bridge.mapped_session_id(host_chat_id)!=root
                or bridge.mapped_session_workspace(host_chat_id)!=workspace): raise RuntimeError('terminal diagnostic source changed')
        try: value=json.loads(b''.join(chunks))
        except (ValueError,UnicodeError): raise RuntimeError('terminal diagnostic source unavailable') from None
        if type(value) is not dict: raise RuntimeError('terminal diagnostic source unavailable')
        return value

    async def session_observation_request(self, host_chat_id: str, target_session_id: str | None,
                                          source: str, limit: int, after: str | None, *, owner: str,
                                          timeout: float = 20.0) -> httpx.Response:
        """Read one page from an already-running mapped worker without admitting a session."""
        if source not in {"task", "actor", "context"} or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid observation source or page limit")
        if source == "context" and limit > 20:
            raise ValueError("invalid context observation page limit")
        if after is not None and (not isinstance(after, str) or not after or len(after) > 512):
            raise ValueError("invalid observation continuation")
        bridge = self._bridge
        if not bridge or not self.is_alive() or host_chat_id not in bridge.mapped_sessions(owner=owner):
            raise RuntimeError("managed observation source is not ready")
        root_id = bridge.mapped_session_id(host_chat_id)
        root_workspace = bridge.mapped_session_workspace(host_chat_id)
        target = target_session_id or root_id
        async with self.internal_http_client(timeout=timeout) as client:
            workspace = root_workspace
            if target != root_id:
                peer = await _bounded_direct_peer_proof(client, root_id, target, root_workspace)
                workspace = peer["directory"]
            query = urlencode({"limit": limit, **({"after": after} if after else {})})
            response = await client.get(session_http_path(target, f"observation-page/{source}?{query}", workspace))
            if target != root_id and await _bounded_direct_peer_proof(client, root_id, target, root_workspace) != peer:
                raise RuntimeError("managed peer changed")
            if bridge.mapped_session_id(host_chat_id) != root_id or bridge.mapped_session_workspace(host_chat_id) != root_workspace:
                raise RuntimeError("managed observation binding changed")
            return response

    async def session_direct_peer_observation(self, host_chat_id, root, workspace, after, *, owner, timeout=5.0):
        """Read bounded direct peers on this exact already-running worker."""
        bridge=self._bridge
        if (not bridge or not self.is_alive() or bridge._owner!=owner or bridge.mapped_session_id(host_chat_id)!=root
                or bridge.mapped_session_workspace(host_chat_id)!=workspace):raise RuntimeError('peer observation unavailable')
        original=await bridge._durable_session_map.async_lookup(host_chat_id)
        if not original or original.get('current')!=root:raise RuntimeError('peer map unavailable')
        async with self.internal_http_client(timeout=timeout) as client:
            query=urlencode({'limit':4,**({'after':after} if after else {})})
            response=await client.get(session_http_path(root,f'observation-page/peer?{query}',workspace))
            if response.status_code!=200 or len(response.content)>65536:raise RuntimeError('peer discovery unavailable')
            page=response.json()
            if (type(page) is not dict or set(page)-{'items','nextCursor','observedAt'} or type(page.get('items')) is not list
                or len(page['items'])>4 or type(page.get('observedAt')) is not int
                or (page.get('nextCursor') is not None and (type(page['nextCursor']) is not str or not re.fullmatch(r'[1-9][0-9]{0,15}',page['nextCursor'])))):
                raise RuntimeError('peer discovery invalid')
            items=[];seen=set();verified=[]
            for peer in page['items']:
                if (type(peer) is not dict or set(peer)!={'sessionId','parentSessionId'} or peer.get('parentSessionId')!=root
                    or type(peer.get('sessionId')) is not str or not peer['sessionId'] or len(peer['sessionId'])>256 or peer['sessionId'] in seen):
                    raise RuntimeError('peer discovery invalid')
                target=peer['sessionId'];seen.add(target)
                value=await _bounded_direct_peer_proof(client,root,target,workspace)
                if (type(value) is not dict or value.get('id')!=target or value.get('parentID')!=root or value.get('directory')!=workspace):raise RuntimeError('peer workspace unavailable')
                observed=await client.get(session_http_path(target,'observation-page/actor?limit=32',value['directory']))
                if observed.status_code!=200 or len(observed.content)>1048576:raise RuntimeError('peer actors unavailable')
                actors=observed.json()
                if (type(actors) is not dict or set(actors)-{'items','nextCursor','observedAt'} or type(actors.get('items')) is not list
                    or len(actors['items'])>32 or type(actors.get('observedAt')) is not int
                    or (actors.get('nextCursor') is not None and (type(actors['nextCursor']) is not str or len(actors['nextCursor'])>512))):raise RuntimeError('peer actors invalid')
                if await _bounded_direct_peer_proof(client,root,target,workspace)!=value:raise RuntimeError('peer changed')
                items.append({**peer,'actors':actors});verified.append((target,value))
            for target,value in verified:
                if await _bounded_direct_peer_proof(client,root,target,workspace)!=value:raise RuntimeError('peer changed')
        if (await bridge._durable_session_map.async_lookup(host_chat_id)!=original or not self.is_alive() or self._bridge is not bridge
            or bridge.mapped_session_id(host_chat_id)!=root or bridge.mapped_session_workspace(host_chat_id)!=workspace):raise RuntimeError('peer binding changed')
        return {'items':items,'nextCursor':page.get('nextCursor'),'observedAt':page['observedAt']}

    async def session_workflow_observation(self, host_chat_id, root, workspace, after, *, owner, timeout=5.0, run_id=None):
        bridge=self._bridge
        if (not bridge or not self.is_alive() or bridge._owner!=owner or bridge.mapped_session_id(host_chat_id)!=root
                or bridge.mapped_session_workspace(host_chat_id)!=workspace): raise RuntimeError('workflow observation unavailable')
        original=await bridge._durable_session_map.async_lookup(host_chat_id)
        if not original or original.get('current')!=root: raise RuntimeError('workflow map unavailable')
        async with self.internal_http_client(timeout=timeout) as client:
            query=urlencode({'limit':1,'target':run_id} if run_id else {'limit':32,**({'after':after} if after else {})})
            response=await client.get(session_http_path(root,f'observation-page/workflow?{query}',workspace))
            if response.status_code!=200 or len(response.content)>65536: raise RuntimeError('workflow observation unavailable')
            page=response.json()
        if (await bridge._durable_session_map.async_lookup(host_chat_id)!=original or not self.is_alive() or self._bridge is not bridge
            or bridge.mapped_session_id(host_chat_id)!=root or bridge.mapped_session_workspace(host_chat_id)!=workspace): raise RuntimeError('workflow binding changed')
        if run_id is not None:
            if (type(page) is not dict or set(page)!={'runId','sessionId','status','observedAt','workspaceWitness','coverage','nodes'}
                    or page['runId']!=run_id or page['sessionId']!=root
                    or page['status'] not in ('running','completed','failed','cancelled')
                    or type(page['observedAt']) is not int or page['observedAt']<0
                    or page['coverage'] not in ('unavailable','partial','bounded')
                    or type(page['nodes']) is not list or len(page['nodes'])>128
                    or (page['coverage']=='unavailable')!= (not page['nodes'])):
                raise RuntimeError('workflow structure invalid')
            witness=page['workspaceWitness']
            if (type(witness) is not dict or set(witness)!={'accountID','workspaceID'}
                    or any(type(witness[k]) is not str or not witness[k] or len(witness[k].encode('utf-8'))>256
                           for k in ('accountID','workspaceID'))):
                raise RuntimeError('workflow structure witness invalid')
            ids=set()
            for node in page['nodes']:
                if (type(node) is not dict or set(node)!={'id','kind','title','phaseId','state','actorId'}
                        or type(node['id']) is not str or not re.fullmatch(r'[paw][0-9]{1,6}',node['id']) or node['id'] in ids
                        or node['kind'] not in ('phase','agent','workflow')
                        or type(node['title']) is not str or not node['title'] or len(node['title'])>120
                        or (node['phaseId'] is not None and (type(node['phaseId']) is not str or not re.fullmatch(r'p[0-9]{1,6}',node['phaseId'])))
                        or node['state'] not in (None,'running','completed','succeeded','failed','cancelled')
                        or (node['actorId'] is not None and (node['kind']!='agent' or type(node['actorId']) is not str
                            or not node['actorId'] or len(node['actorId'].encode('utf-8'))>256))):
                    raise RuntimeError('workflow structure node invalid')
                ids.add(node['id'])
            return page
        if (type(page) is not dict or set(page)-{'items','nextCursor','observedAt'} or type(page.get('items')) is not list
            or len(page['items'])>32 or type(page.get('observedAt')) is not int or page['observedAt']<0
            or (page.get('nextCursor') is not None and (type(page['nextCursor']) is not str or not re.fullmatch(r'[1-9][0-9]{0,15}',page['nextCursor'])))):
            raise RuntimeError('workflow page invalid')
        for row in page['items']:
            if (type(row) is not dict or set(row)!={'id','status','running','succeeded','failed','created','updated','workspaceWitness'}
                or type(row['id']) is not str or not re.fullmatch(r'wf_(?:[0-9A-Za-z-]{26}|[0-9a-f]{64})',row['id'])
                or row['status'] not in {'running','completed','failed','cancelled'}
                or any(type(row[k]) is not int or not 0<=row[k]<=9007199254740991 for k in ('running','succeeded','failed','created','updated'))):
                raise RuntimeError('workflow row invalid')
            witness=row['workspaceWitness']
            if (witness is not None and (type(witness) is not dict or set(witness)!={'accountID','workspaceID'}
                or any(type(witness[key]) is not str or not witness[key] or witness[key]!=witness[key].strip()
                       or len(witness[key].encode('utf-8'))>256 or '\x00' in witness[key] for key in ('accountID','workspaceID')))):
                raise RuntimeError('workflow witness invalid')
        return page

    async def session_goal_observation(self, host_chat_id, native_id, workspace, *, owner, timeout=5.0):
        bridge = self._bridge
        if (not bridge or not self.is_alive() or bridge._owner != owner
                or bridge.mapped_session_id(host_chat_id) != native_id
                or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError('goal observation unavailable')
        entry = await bridge._durable_session_map.async_lookup(host_chat_id)
        if not entry or entry.get('current') != native_id: raise RuntimeError('goal binding unavailable')
        async with self.internal_http_client(timeout=timeout) as client:
            response = await client.get(session_http_path(native_id, 'goal-observation', workspace))
        if (await bridge._durable_session_map.async_lookup(host_chat_id) != entry or not self.is_alive()
                or self._bridge is not bridge or bridge.mapped_session_id(host_chat_id) != native_id
                or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError('goal binding changed')
        return response

    async def session_actor_inspection(self, host_chat_id, target, actor_id, revision, *, owner, message_before=None, receipt_after=None, timeout=5.0):
        bridge = self._bridge
        if not bridge or not self.is_alive() or bridge._owner != owner:
            raise RuntimeError('inspection worker unavailable')
        root = bridge.mapped_session_id(host_chat_id)
        workspace = bridge.mapped_session_workspace(host_chat_id)
        entry = await bridge._durable_session_map.async_lookup(host_chat_id)
        if not root or not workspace or not entry or entry.get('current') != root:
            raise RuntimeError('inspection mapping unavailable')
        async with self.internal_http_client(timeout=timeout) as client:
            directory = workspace
            if target != root:
                peer = await _bounded_direct_peer_proof(client, root, target, workspace)
                directory = peer['directory']
            query = urlencode({'rootSessionID': root, 'executionRevision': revision,
                               **({'messageBefore': message_before} if message_before else {}),
                               **({'receiptAfter': receipt_after} if receipt_after else {})})
            response = await client.get(session_http_path(target, f"actor-inspection/{quote(actor_id, safe='')}?{query}", directory))
            if target != root and await _bounded_direct_peer_proof(client, root, target, workspace) != peer:
                raise RuntimeError('inspection peer changed')
        if (await bridge._durable_session_map.async_lookup(host_chat_id) != entry or not self.is_alive()
                or self._bridge is not bridge or bridge.mapped_session_id(host_chat_id) != root
                or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError('inspection mapping changed')
        return response

    async def session_git_index_preview_request(self, host_chat_id: str, native_id: str, workspace: str, *, owner: str, timeout: float = 5.0) -> httpx.Response:
        """Read a positive private index preview on this exact mapped worker."""
        bridge = self._bridge
        if (not bridge or not self.is_alive() or bridge._owner != owner
                or bridge.mapped_session_id(host_chat_id) != native_id
                or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError('managed Git binding unavailable')
        entry = await bridge._durable_session_map.async_lookup(host_chat_id)
        if not entry or entry.get('current') != native_id:
            raise RuntimeError('managed Git binding changed')
        async with httpx.AsyncClient(base_url=self.http_base_url,
                auth=httpx.BasicAuth(self.__http_username, self.__http_password),
                transport=httpx.AsyncHTTPTransport(retries=0), follow_redirects=False,
                timeout=timeout, trust_env=False) as client:
            response = await client.post(session_http_path(native_id, 'git-index-preview', workspace),
                json={'version':1, 'stableChatID':host_chat_id})
        if (await bridge._durable_session_map.async_lookup(host_chat_id) != entry or not self.is_alive()
                or self._bridge is not bridge or bridge.mapped_session_id(host_chat_id) != native_id
                or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError('managed Git binding changed')
        return response

    async def _session_git_commit_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict,
            *, owner: str, apply: bool, timeout: float = 30.0, branch: bool = False, remote_mode: str | None = None) -> httpx.Response:
        """Use the original mapped worker for one closed private commit call."""
        bridge = self._bridge
        if (not bridge or not self.is_alive() or bridge._owner != owner
                or bridge.mapped_session_id(host_chat_id) != native_id
                or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError('managed Git binding unavailable')
        entry = await bridge._durable_session_map.async_lookup(host_chat_id)
        if not entry or entry.get('current') != native_id:
            raise RuntimeError('managed Git binding changed')
        if remote_mode not in (None, 'preview', 'fetch', 'push', 'checkout-preview', 'checkout', 'worktree-stage-preview', 'worktree-stage', 'worktree-register', 'worktree-publish', 'worktree-inverse', 'worktree-redo'): raise ValueError('invalid Git remote mode')
        suffix = 'git-worktree-redo' if remote_mode == 'worktree-redo' else 'git-worktree-inverse' if remote_mode == 'worktree-inverse' else 'git-worktree-publish' if remote_mode == 'worktree-publish' else 'git-worktree-register' if remote_mode == 'worktree-register' else 'git-worktree-stage-preview' if remote_mode == 'worktree-stage-preview' else 'git-worktree-stage' if remote_mode == 'worktree-stage' else 'git-checkout' if remote_mode == 'checkout' else 'git-checkout-preview' if remote_mode == 'checkout-preview' else 'git-remote-preview' if remote_mode == 'preview' else 'git-fetch' if remote_mode == 'fetch' else 'git-push' if remote_mode == 'push' else 'git-branch-create' if branch else 'git-commit' if apply else 'git-commit-preview'
        async with httpx.AsyncClient(base_url=self.http_base_url,
                auth=httpx.BasicAuth(self.__http_username, self.__http_password),
                transport=httpx.AsyncHTTPTransport(retries=0), follow_redirects=False,
                timeout=timeout, trust_env=False) as client:
            async with client.stream('POST',session_http_path(native_id,suffix,workspace),json=payload) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > 4*1024*1024 + 128*1024:
                        raise RuntimeError('managed Git response unavailable')
                    content.extend(chunk)
                result = httpx.Response(response.status_code,content=bytes(content))
        if (await bridge._durable_session_map.async_lookup(host_chat_id) != entry or not self.is_alive()
                or self._bridge is not bridge or bridge.mapped_session_id(host_chat_id) != native_id
                or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError('managed Git binding changed')
        return result

    async def session_git_commit_preview_request(self, host_chat_id: str, native_id: str, workspace: str, *, owner: str, timeout: float = 30.0) -> httpx.Response:
        return await self._session_git_commit_request(host_chat_id,native_id,workspace,{'version':1,'stableChatID':host_chat_id},owner=owner,apply=False,timeout=timeout)

    async def session_git_commit_apply_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 30.0) -> httpx.Response:
        return await self._session_git_commit_request(host_chat_id,native_id,workspace,payload,owner=owner,apply=True,timeout=timeout)

    async def session_git_branch_apply_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 30.0) -> httpx.Response:
        return await self._session_git_commit_request(host_chat_id,native_id,workspace,payload,owner=owner,apply=True,timeout=timeout,branch=True)

    async def session_git_remote_preview_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 55.0) -> httpx.Response:
        return await self._session_git_commit_request(host_chat_id,native_id,workspace,payload,owner=owner,apply=False,timeout=timeout,remote_mode='preview')

    async def session_git_fetch_apply_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 65.0) -> httpx.Response:
        return await self._session_git_commit_request(host_chat_id,native_id,workspace,payload,owner=owner,apply=True,timeout=timeout,remote_mode='fetch')

    async def session_git_push_apply_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 65.0) -> httpx.Response:
        return await self._session_git_commit_request(host_chat_id,native_id,workspace,payload,owner=owner,apply=True,timeout=timeout,remote_mode='push')

    async def session_git_worktree_redo_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 300.0) -> httpx.Response:
        return await self._session_git_commit_request(host_chat_id,native_id,workspace,payload,owner=owner,apply=True,timeout=timeout,remote_mode='worktree-redo')

    async def session_git_worktree_inverse_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 300.0) -> httpx.Response:
        return await self._session_git_commit_request(host_chat_id,native_id,workspace,payload,owner=owner,apply=True,timeout=timeout,remote_mode='worktree-inverse')

    async def session_git_worktree_publish_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 300.0) -> httpx.Response:
        return await self._session_git_commit_request(host_chat_id,native_id,workspace,payload,owner=owner,apply=True,timeout=timeout,remote_mode='worktree-publish')

    async def session_git_worktree_register_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 300.0) -> httpx.Response:
        return await self._session_git_commit_request(host_chat_id,native_id,workspace,payload,owner=owner,apply=True,timeout=timeout,remote_mode='worktree-register')

    async def session_git_worktree_stage_preview_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 300.0) -> httpx.Response:
        return await self._session_git_commit_request(host_chat_id,native_id,workspace,payload,owner=owner,apply=False,timeout=timeout,remote_mode='worktree-stage-preview')

    async def session_git_worktree_stage_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 300.0) -> httpx.Response:
        return await self._session_git_commit_request(host_chat_id,native_id,workspace,payload,owner=owner,apply=True,timeout=timeout,remote_mode='worktree-stage')

    async def session_git_checkout_preview_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 30.0) -> httpx.Response:
        return await self._session_git_commit_request(host_chat_id,native_id,workspace,payload,owner=owner,apply=False,timeout=timeout,remote_mode='checkout-preview')

    async def session_git_checkout_apply_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 65.0) -> httpx.Response:
        return await self._session_git_commit_request(host_chat_id,native_id,workspace,payload,owner=owner,apply=True,timeout=timeout,remote_mode='checkout')

    async def _session_git_cleanup_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict,
            *, owner: str, apply: bool, timeout: float = 30.0) -> httpx.Response:
        """Use the original mapped worker for one closed private commit call."""
        bridge = self._bridge
        if (not bridge or not self.is_alive() or bridge._owner != owner
                or bridge.mapped_session_id(host_chat_id) != native_id
                or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError('managed Git binding unavailable')
        entry = await bridge._durable_session_map.async_lookup(host_chat_id)
        if not entry or entry.get('current') != native_id:
            raise RuntimeError('managed Git binding changed')
        suffix = 'git-cleanup' if apply else 'git-cleanup-preview'
        async with httpx.AsyncClient(base_url=self.http_base_url,
                auth=httpx.BasicAuth(self.__http_username, self.__http_password),
                transport=httpx.AsyncHTTPTransport(retries=0), follow_redirects=False,
                timeout=timeout, trust_env=False) as client:
            async with client.stream('POST',session_http_path(native_id,suffix,workspace),json=payload) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > 4*1024*1024 + 128*1024:
                        raise RuntimeError('managed Git response unavailable')
                    content.extend(chunk)
                result = httpx.Response(response.status_code,content=bytes(content))
        if (await bridge._durable_session_map.async_lookup(host_chat_id) != entry or not self.is_alive()
                or self._bridge is not bridge or bridge.mapped_session_id(host_chat_id) != native_id
                or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError('managed Git binding changed')
        return result

    async def session_git_cleanup_preview_request(self, host_chat_id: str, native_id: str, workspace: str, *, owner: str, directory: str, timeout: float = 30.0) -> httpx.Response:
        return await self._session_git_cleanup_request(host_chat_id,native_id,workspace,{'version':1,'stableChatID':host_chat_id,'directory':directory},owner=owner,apply=False,timeout=timeout)

    async def session_git_cleanup_apply_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 30.0) -> httpx.Response:
        return await self._session_git_cleanup_request(host_chat_id,native_id,workspace,payload,owner=owner,apply=True,timeout=timeout)

    async def session_git_index_apply_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 30.0) -> httpx.Response:
        """Issue one private index action on the already retained worker."""
        bridge = self._bridge
        if (not bridge or not self.is_alive() or bridge._owner != owner
                or bridge.mapped_session_id(host_chat_id) != native_id
                or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError('managed Git binding unavailable')
        entry = await bridge._durable_session_map.async_lookup(host_chat_id)
        if not entry or entry.get('current') != native_id:
            raise RuntimeError('managed Git binding changed')
        async with httpx.AsyncClient(base_url=self.http_base_url,
                auth=httpx.BasicAuth(self.__http_username, self.__http_password),
                transport=httpx.AsyncHTTPTransport(retries=0), follow_redirects=False,
                timeout=timeout, trust_env=False) as client:
            response = await client.post(session_http_path(native_id, 'git-index', workspace), json=payload)
        if (await bridge._durable_session_map.async_lookup(host_chat_id) != entry or not self.is_alive()
                or self._bridge is not bridge or bridge.mapped_session_id(host_chat_id) != native_id
                or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError('managed Git binding changed')
        return response

    async def session_git_read_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 5.0) -> httpx.Response:
        """Read only from the same already-mapped native session, without retries."""
        bridge = self._bridge
        if (not bridge or not self.is_alive() or bridge._owner != owner
                or bridge.mapped_session_id(host_chat_id) != native_id
                or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError("managed git binding unavailable")
        entry = await bridge._durable_session_map.async_lookup(host_chat_id)
        if not entry or entry.get("current") != native_id:
            raise RuntimeError("managed git binding changed")
        if (not isinstance(payload, dict) or set(payload) - {"stableChatID", "operation", "path"}
                or payload.get("stableChatID") != host_chat_id
                or payload.get("operation") not in {"root", "head", "index", "tree", "indexEntry", "treeEntry", "blob", "others", "branches", "log", "worktrees"}
                or (payload.get("operation") in {"blob", "indexEntry", "treeEntry"}) != ("path" in payload)
                or ("path" in payload and (not isinstance(payload["path"], str) or not payload["path"] or len(payload["path"].encode('utf-8')) > 4096))):
            raise ValueError("invalid managed git read")
        async with httpx.AsyncClient(base_url=self.http_base_url,
                auth=httpx.BasicAuth(self.__http_username, self.__http_password),
                transport=httpx.AsyncHTTPTransport(retries=0), follow_redirects=False,
                timeout=timeout, trust_env=False) as client:
            response = await client.post(session_http_path(native_id, "git-review", workspace), json=payload)
        current = await bridge._durable_session_map.async_lookup(host_chat_id)
        if (current != entry or not self.is_alive() or self._bridge is not bridge
                or bridge.mapped_session_id(host_chat_id) != native_id
                or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError("managed git binding changed")
        return response

    async def session_background_start_source_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 5.0, _mode: str = 'start') -> httpx.Response:
        bridge = self._bridge
        keys = {'stableChatID','messageID','actorID','rootOperationID','executionRevision','innerTurnID','toolCallID','toolName','argsSHA256'}
        selector_modes = {'memory-inferred','media','frame','task-read','task-create','task-control','task-run','task-edit','start','read','terminate','write','retire','file-mutation'}
        selector_present = type(payload) is dict and 'sourceSessionID' in payload
        if _mode == 'shared-text' or (_mode in selector_modes and selector_present):
            keys = keys | {'sourceSessionID'}
        if (not bridge or not self.is_alive() or bridge._owner != owner or type(payload) is not dict
                or set(payload) != keys or (selector_present and (type(payload.get('sourceSessionID')) is not str or not payload['sourceSessionID']))
                or payload.get('stableChatID') != host_chat_id
                or bridge.mapped_session_id(host_chat_id) != native_id
                or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError('managed background source unavailable')
        entry = await bridge._durable_session_map.async_lookup(host_chat_id)
        if not entry or entry.get('current') != native_id:
            raise RuntimeError('managed background binding changed')
        async with httpx.AsyncClient(base_url=self.http_base_url,
                auth=httpx.BasicAuth(self.__http_username, self.__http_password),
                transport=httpx.AsyncHTTPTransport(retries=0), follow_redirects=False,
                timeout=timeout, trust_env=False) as client:
            if _mode not in ('start','read','terminate','write','retire','task-create','task-read','task-control','task-run','task-edit','media','frame','shared-text','memory-inferred','file-mutation'): raise ValueError('background source mode unavailable')
            suffix='background-start-source' if _mode=='start' and not selector_present else f'background-start-source?mode={_mode}'
            response = await client.post(session_http_path(native_id, suffix, workspace), json=payload)
        current = await bridge._durable_session_map.async_lookup(host_chat_id)
        if (current != entry or not self.is_alive() or self._bridge is not bridge
                or bridge.mapped_session_id(host_chat_id) != native_id
                or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError('managed background binding changed')
        return response

    async def session_background_read_source_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 5.0) -> httpx.Response:
        return await self.session_background_start_source_request(host_chat_id,native_id,workspace,payload,owner=owner,timeout=timeout,_mode='read')

    async def session_native_analysis_proof(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 5.0, _candidates: bool = False) -> httpx.Response:
        """Read exact original part from this ready worker; never start an engine."""
        bridge = self._bridge
        if (not bridge or not self.is_alive() or bridge._owner != owner or type(payload) is not dict
                or payload.get('stableChatID') != host_chat_id or bridge.mapped_session_id(host_chat_id) != native_id
                or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError('analysis proof unavailable')
        process, generation = self._proc, self.installed_generation
        if process is None: raise RuntimeError('analysis proof unavailable')
        entry = await bridge._durable_session_map.async_lookup(host_chat_id)
        if not entry or entry.get('current') != native_id: raise RuntimeError('analysis proof unavailable')
        async with httpx.AsyncClient(base_url=self.http_base_url,
                auth=httpx.BasicAuth(self.__http_username,self.__http_password),
                transport=httpx.AsyncHTTPTransport(retries=0),follow_redirects=False,
                timeout=timeout,trust_env=False) as client:
            response=await client.post(session_http_path(native_id,'native-analysis-candidates' if _candidates else 'native-analysis-proof',workspace),json=payload)
        current=await bridge._durable_session_map.async_lookup(host_chat_id)
        if (current != entry or not self.is_alive() or self._bridge is not bridge or self._proc is not process
                or self.installed_generation != generation or bridge.mapped_session_id(host_chat_id) != native_id
                or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError('analysis proof unavailable')
        return response

    async def session_native_analysis_candidates(self, host_chat_id, native_id, workspace, payload, *, owner, timeout=5.0):
        if type(payload) is not dict or set(payload)-{'stableChatID','cursor'} or payload.get('stableChatID') != host_chat_id:
            raise RuntimeError('analysis candidates unavailable')
        return await self.session_native_analysis_proof(host_chat_id,native_id,workspace,payload,owner=owner,timeout=timeout,_candidates=True)

    async def session_task_create_source_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 5.0) -> httpx.Response:
        return await self.session_background_start_source_request(host_chat_id,native_id,workspace,payload,owner=owner,timeout=timeout,_mode='task-create')

    async def session_frame_source_request(self, host_chat_id, native_id, workspace, payload, *, owner, timeout=5):
        return await self.session_background_start_source_request(host_chat_id,native_id,workspace,payload,owner=owner,timeout=timeout,_mode='frame')

    async def session_shared_text_source_request(self, host_chat_id, native_id, workspace, payload, *, owner, timeout=5):
        return await self.session_background_start_source_request(host_chat_id,native_id,workspace,payload,owner=owner,timeout=timeout,_mode='shared-text')

    async def session_media_source_request(self, host_chat_id, native_id, workspace, payload, *, owner, timeout=5):
        return await self.session_background_start_source_request(host_chat_id,native_id,workspace,payload,owner=owner,timeout=timeout,_mode='media')

    async def session_memory_inferred_source_request(self, host_chat_id, native_id, workspace, payload, *, owner, timeout=5):
        """Read only the exact current claimed memory invocation on this worker."""
        bridge=self._bridge; process=self._proc; generation=self.installed_generation
        holder=getattr(bridge,'_managed_provider_generation',None) if bridge else None
        if not bridge or not process or not holder or not self.is_alive():return {'state':'unavailable'}
        response=await self.session_background_start_source_request(host_chat_id,native_id,workspace,payload,
            owner=owner,timeout=timeout,_mode='memory-inferred')
        if (self._bridge is not bridge or self._proc is not process or self.installed_generation!=generation
                or getattr(bridge,'_managed_provider_generation',None)!=holder or not self.is_alive()
                or response.status_code!=200):return {'state':'unavailable'}
        try:
            result=response.json()
            if (type(result) is dict and set(result)=={'state','argsSHA256'} and result['state']=='current'
                    and result['argsSHA256']==payload.get('argsSHA256')):return result
        except Exception:pass
        return {'state':'unavailable'}

    async def session_workflow_source_request(self, host_chat_id: str, native_id: str, workspace: str, writer_session_id: str, source_token: str, message_id: str, tool_call_id: str, args_sha256: str, *, owner: str, timeout: float = 5.0, task_assignment: bool = False, root_self: bool = False) -> httpx.Response:
        """Reread a fixed child, same-session task or actual main-root source."""
        bridge=self._bridge; process=self._proc; generation=self.installed_generation
        holder=getattr(bridge,'_managed_provider_generation',None) if bridge else None
        if (not bridge or bridge._owner!=owner or not process or not holder or not self.is_alive()
                or type(source_token) is not str or not re.fullmatch(r'[0-9a-f]{64}',source_token)
                or any(type(value) is not str or not value or len(value)>256 for value in (message_id,tool_call_id))
                or type(args_sha256) is not str or not re.fullmatch(r'[0-9a-f]{64}',args_sha256)
                or type(writer_session_id) is not str or not writer_session_id or len(writer_session_id)>256
                or type(task_assignment) is not bool or type(root_self) is not bool or (task_assignment and root_self)
                or (writer_session_id==native_id) != (task_assignment or root_self)
                or bridge.mapped_session_id(host_chat_id)!=native_id
                or bridge.mapped_session_workspace(host_chat_id)!=workspace):
            raise RuntimeError('workflow source unavailable')
        entry=await bridge._durable_session_map.async_lookup(host_chat_id)
        if not entry or entry.get('current')!=native_id: raise RuntimeError('workflow source unavailable')
        async with self.internal_http_client(timeout=timeout) as client:
            peer=None if (task_assignment or root_self) else await _bounded_direct_peer_proof(client,native_id,writer_session_id,workspace)
            if not (task_assignment or root_self) and (not peer or peer.get('directory')!=workspace): raise RuntimeError('workflow source unavailable')
            response=await client.post(session_http_path(native_id,'workflow-source/read',workspace),json={'sourceToken':source_token,'messageID':message_id,'toolCallID':tool_call_id,'argsSHA256':args_sha256})
            if len(response.content)>65536 or (not (task_assignment or root_self) and await _bounded_direct_peer_proof(client,native_id,writer_session_id,workspace)!=peer):
                raise RuntimeError('workflow source changed')
            if task_assignment:
                try:
                    value=response.json(); source=value['source']; writer=value['writer']; task=source['source']
                    fields=('sessionID','actorID','executionRevision','rootOperationID','innerTurnID')
                    if (response.status_code!=200 or type(value) is not dict or set(value)!={'state','source','writer','argsSHA256'}
                            or value['state']!='current' or value['argsSHA256']!=args_sha256 or type(source) is not dict
                            or set(source)!={'version','sessionID','actorID','executionRevision','rootOperationID','innerTurnID','source'}
                            or source['version']!=1 or type(source['version']) is not int or type(writer) is not dict or set(writer)!=set(fields)
                            or any(source.get(field)!=writer.get(field) for field in fields) or writer['sessionID']!=native_id
                            or type(writer['executionRevision']) is not int or not 0<=writer['executionRevision']<=9007199254740991
                            or any(type(writer[field]) is not str or not writer[field] or len(writer[field].encode('utf-8'))>256 for field in fields if field!='executionRevision')
                            or type(task) is not dict or set(task)!={'kind','sessionID','taskID','assignmentEventID'} or task['kind']!='native-task'
                            or task['sessionID']!=native_id or type(task['taskID']) is not str or not re.fullmatch(r'T[0-9]+(?:\.[0-9]+)*',task['taskID']) or len(task['taskID'].encode('utf-8'))>256
                            or type(task['assignmentEventID']) is not int or not 0<task['assignmentEventID']<=9007199254740991):
                        raise ValueError('invalid task source')
                except Exception as exc:
                    raise RuntimeError('workflow task source unavailable') from exc
            if root_self:
                try:
                    value=response.json(); source=value['source']; writer=value['writer']; nested=source['source']
                    fields=('sessionID','actorID','executionRevision','rootOperationID','innerTurnID')
                    if (response.status_code!=200 or type(value) is not dict or set(value)!={'state','source','writer','argsSHA256'}
                            or value['state']!='current' or value['argsSHA256']!=args_sha256 or type(source) is not dict
                            or set(source)!={'version','sessionID','actorID','executionRevision','rootOperationID','innerTurnID','source'}
                            or type(source['version']) is not int or source['version']!=1 or type(writer) is not dict or set(writer)!=set(fields)
                            or any(source.get(field)!=writer.get(field) for field in fields) or writer['sessionID']!=native_id or writer['actorID']!='main'
                            or type(writer['executionRevision']) is not int or not 0<=writer['executionRevision']<=9007199254740991
                            or any(type(writer[field]) is not str or not writer[field] or len(writer[field].encode('utf-8'))>256 for field in fields if field!='executionRevision')
                            or type(nested) is not dict or set(nested)!={'kind','rootOperationID'} or nested['kind']!='root-operation'
                            or nested['rootOperationID']!=writer['rootOperationID']):
                        raise ValueError('invalid root source')
                except Exception as exc:
                    raise RuntimeError('workflow root source unavailable') from exc
        current=await bridge._durable_session_map.async_lookup(host_chat_id)
        if (current!=entry or self._bridge is not bridge or self._proc is not process or not self.is_alive()
                or self.installed_generation!=generation or getattr(bridge,'_managed_provider_generation',None)!=holder
                or bridge.mapped_session_id(host_chat_id)!=native_id or bridge.mapped_session_workspace(host_chat_id)!=workspace):
            raise RuntimeError('workflow source changed')
        return response

    async def session_task_read_source_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 5.0) -> httpx.Response:
        return await self.session_background_start_source_request(host_chat_id,native_id,workspace,payload,owner=owner,timeout=timeout,_mode='task-read')

    async def session_task_control_source_request(self, host_chat_id, native_id, workspace, payload, *, owner, timeout=5):
        return await self.session_background_start_source_request(host_chat_id,native_id,workspace,payload,owner=owner,timeout=timeout,_mode='task-control')

    async def session_task_run_source_request(self, host_chat_id, native_id, workspace, payload, *, owner, timeout=5):
        return await self.session_background_start_source_request(host_chat_id,native_id,workspace,payload,owner=owner,timeout=timeout,_mode='task-run')

    async def session_task_edit_source_request(self, host_chat_id, native_id, workspace, payload, *, owner, timeout=5):
        return await self.session_background_start_source_request(host_chat_id,native_id,workspace,payload,owner=owner,timeout=timeout,_mode='task-edit')

    async def session_background_terminate_source_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 5.0) -> httpx.Response:
        return await self.session_background_start_source_request(host_chat_id,native_id,workspace,payload,owner=owner,timeout=timeout,_mode='terminate')

    async def session_background_write_source_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 5.0) -> httpx.Response:
        return await self.session_background_start_source_request(host_chat_id,native_id,workspace,payload,owner=owner,timeout=timeout,_mode='write')

    async def session_background_retire_source_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 5.0) -> httpx.Response:
        return await self.session_background_start_source_request(host_chat_id,native_id,workspace,payload,owner=owner,timeout=timeout,_mode='retire')

    async def session_worker_naming_source_request(self, host_chat_id, native_id, workspace, payload, *, owner, timeout=5.0):
        """Capture one already-admitted child source on this exact worker."""
        bridge=self._bridge; process=self._proc; generation=self.installed_generation
        holder=getattr(bridge,'_managed_provider_generation',None) if bridge else None
        if (not bridge or bridge._owner!=owner or not process or not holder or not self.is_alive()
                or bridge.mapped_session_id(host_chat_id)!=native_id or bridge.mapped_session_workspace(host_chat_id)!=workspace
                or type(payload) is not dict or set(payload)!={'sourceToken','messageID','toolCallID'}):
            raise RuntimeError('worker naming source unavailable')
        original=await bridge._durable_session_map.async_lookup(host_chat_id)
        if not original or original.get('current')!=native_id: raise RuntimeError('worker naming mapping unavailable')
        async with self.internal_http_client(timeout=timeout) as client:
            response=await client.post(session_http_path(native_id,'worker-naming-source/read',workspace),json=payload)
        if (response.status_code!=200 or len(response.content)>32768 or not self.is_alive() or self._bridge is not bridge
                or self._proc is not process or self.installed_generation!=generation
                or getattr(bridge,'_managed_provider_generation',None)!=holder
                or bridge.mapped_session_id(host_chat_id)!=native_id or bridge.mapped_session_workspace(host_chat_id)!=workspace
                or await bridge._durable_session_map.async_lookup(host_chat_id)!=original):
            raise RuntimeError('worker naming source changed')
        return response.json()

    async def session_actor_display_request(self, host_chat_id, native_id, workspace, actor_id, payload, *, owner, timeout=5.0):
        """One no-retry rename on the already mapped worker, never a cold start."""
        bridge = self._bridge
        if not bridge or not self.is_alive() or bridge._owner != owner:
            raise RuntimeError('actor display worker unavailable')
        process = self._proc; generation = self.installed_generation
        if process is None: raise RuntimeError('actor display worker unavailable')
        root = bridge.mapped_session_id(host_chat_id)
        if not root or bridge.mapped_session_workspace(host_chat_id) != workspace:
            raise RuntimeError('actor display mapping unavailable')
        entry = await bridge._durable_session_map.async_lookup(host_chat_id)
        if not entry or entry.get('current') != root: raise RuntimeError('actor display binding unavailable')
        async with self.internal_http_client(timeout=timeout) as client:
            if native_id != root:
                children = await client.get(session_http_path(root,'children?visible=true',workspace))
                if children.status_code != 200: raise RuntimeError('actor child unavailable')
                child = find_direct_peer(children.json(),root,native_id)
                if not child or child.get('directory',workspace) != workspace: raise RuntimeError('actor child unavailable')
            if (await bridge._durable_session_map.async_lookup(host_chat_id) != entry or not self.is_alive()
                    or self._bridge is not bridge or self._proc is not process or self.installed_generation != generation
                    or bridge.mapped_session_id(host_chat_id) != root or bridge.mapped_session_workspace(host_chat_id) != workspace):
                raise RuntimeError('actor display binding changed')
            path = session_http_path(native_id, f"actors/{quote(actor_id,safe='')}/display-name", workspace)
            response = await client.post(path,json=payload)
        current = await bridge._durable_session_map.async_lookup(host_chat_id)
        if (current != entry or not self.is_alive() or self._bridge is not bridge
                or self._proc is not process or self.installed_generation != generation
                or bridge.mapped_session_id(host_chat_id) != root or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError('actor display binding changed')
        return response

    async def session_observed_step_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 5.0) -> httpx.Response:
        """Read an exact local step on this already-owned worker, never admit a session."""
        bridge = self._bridge
        if (not bridge or not self.is_alive() or bridge._owner != owner
                or bridge.mapped_session_id(host_chat_id) != native_id or bridge.mapped_session_workspace(host_chat_id) != workspace
                or type(payload) is not dict or set(payload) not in ({'version','stableChatID','messageID','stepID','membershipRevision'},
                    {'version','stableChatID','messageID','actionRef'}, {'version','stableChatID','head'}, {'version','stableChatID','rootOperationID','headMessageID','anchorMessageID'},
                    {'version','stableChatID','fromMessageID','toMessageID'}, {'version','stableChatID','fromMessageID','toMessageID','cursor'})
                or type(payload.get('version')) is not int or payload['version'] != 1 or payload.get('stableChatID') != host_chat_id
                or ('head' in payload and payload['head'] is not True)
                or any(not isinstance(payload.get(k), str) or not payload[k] or len(payload[k]) > 256 for k in ('fromMessageID','toMessageID','rootOperationID','headMessageID','anchorMessageID') if k in payload)
                or ('cursor' in payload and (not isinstance(payload['cursor'],str) or not payload['cursor'] or len(payload['cursor']) > 8192))):
            raise ValueError('observed step unavailable')
        original = await bridge._durable_session_map.async_lookup(host_chat_id)
        if not original or original.get('current') != native_id: raise RuntimeError('observed step mapping unavailable')
        process, generation = self._proc, self.installed_generation
        async with self.internal_http_client(timeout=timeout) as client:
            response = await client.post(session_http_path(native_id,'observed-step',workspace),json=payload)
        if (await bridge._durable_session_map.async_lookup(host_chat_id) != original or not self.is_alive()
                or self._proc is not process or self._bridge is not bridge or self.installed_generation != generation
                or bridge.mapped_session_id(host_chat_id) != native_id or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError('observed step mapping changed')
        return response

    async def session_rating_source_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 5.0) -> httpx.Response:
        """Read bounded rating sources from the exact mapped native session."""
        bridge = self._bridge
        if (not bridge or not self.is_alive() or bridge._owner != owner
                or bridge.mapped_session_id(host_chat_id) != native_id
                or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError("managed rating binding unavailable")
        entry = await bridge._durable_session_map.async_lookup(host_chat_id)
        if not entry or entry.get("current") != native_id:
            raise RuntimeError("managed rating binding changed")
        if not isinstance(payload, dict) or set(payload) != {"stableChatID"} or payload.get("stableChatID") != host_chat_id:
            raise ValueError("invalid managed rating read")
        async with httpx.AsyncClient(base_url=self.http_base_url,
                auth=httpx.BasicAuth(self.__http_username, self.__http_password),
                transport=httpx.AsyncHTTPTransport(retries=0), follow_redirects=False,
                timeout=timeout, trust_env=False) as client:
            response = await client.post(session_http_path(native_id, "rating-source", workspace), json=payload)
        current = await bridge._durable_session_map.async_lookup(host_chat_id)
        if (current != entry or not self.is_alive() or self._bridge is not bridge
                or bridge.mapped_session_id(host_chat_id) != native_id
                or bridge.mapped_session_workspace(host_chat_id) != workspace):
            raise RuntimeError("managed rating binding changed")
        return response

    async def session_context_selection_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 20.0) -> httpx.Response:
        """Call only the currently mapped native session on this existing worker."""
        bridge = self._bridge
        if not bridge or not self.is_alive() or bridge._owner != owner:
            raise RuntimeError("managed context owner unavailable")
        entry = await bridge._durable_session_map.async_lookup(host_chat_id)
        if not entry or entry.get("current") != native_id:
            raise RuntimeError("managed context binding changed")
        async with httpx.AsyncClient(base_url=self.http_base_url,
                auth=httpx.BasicAuth(self.__http_username, self.__http_password),
                transport=httpx.AsyncHTTPTransport(retries=0), follow_redirects=False,
                timeout=timeout, trust_env=False) as client:
            return await client.post(session_http_path(native_id, "context-selection", workspace), json=payload)

    async def session_child_http_request(
        self,
        root_session_id: str,
        child_native_session_id: str,
        suffix: str,
        *,
        owner: str,
        timeout: float = 20.0,
    ) -> httpx.Response:
        """Read a direct peer child only after the engine proves its parent edge."""
        if suffix.split("?", 1)[0].lstrip("/") not in {"actors", "task", "message"}:
            raise ValueError("unsupported orchestration child read endpoint")
        orchestration_read_suffix(suffix)
        if not self._bridge or not self.is_alive():
            raise RuntimeError("mimo ACP is unavailable")
        if root_session_id not in self._bridge.mapped_sessions():
            await self._bridge.ensure_session(root_session_id, owner=owner)
        root_native_id = self._bridge.mapped_session_id(root_session_id)
        root_workspace = self._bridge.mapped_session_workspace(root_session_id)
        children_path = session_http_path(root_native_id, "children?visible=true", root_workspace)
        async with self.internal_http_client(timeout=timeout) as client:
            children_response = await client.get(children_path)
            if children_response.status_code >= 400:
                return children_response
            try:
                children = children_response.json()
            except ValueError as exc:
                raise RuntimeError("managed engine returned invalid child membership data") from exc
            if not isinstance(children, list):
                raise RuntimeError("managed engine returned invalid child membership data")
            child = find_direct_peer(children, root_native_id, child_native_session_id)
            if child is None:
                raise PermissionError("managed child session is not a direct visible child of this chat")
            workspace = child.get("directory")
            if not isinstance(workspace, str) or not workspace:
                workspace = root_workspace
            path = session_http_path(child_native_session_id, suffix, workspace)
            return await client.get(path)

    async def session_child_input_request(
        self,
        root_session_id: str,
        child_native_session_id: str,
        method: str,
        suffix: str,
        payload: dict | None,
        *,
        owner: str,
        timeout: float = 20.0,
        request_id: str | None = None,
    ) -> httpx.Response:
        """Post or cancel one actor receipt only after proving direct child membership."""
        import re

        if method not in {"POST", "DELETE", "GET"} or not isinstance(suffix, str) or "?" in suffix or "#" in suffix:
            raise ValueError("unsupported child input operation")
        path = suffix.strip("/")
        allowed = (
            method == "POST" and (re.fullmatch(r"actors/[^/]+/input", path) or
                                  re.fullmatch(r"actors/[^/]+/input/[^/]+/resume", path))
        ) or (method == "DELETE" and re.fullmatch(r"actors/[^/]+/input/[^/]+", path)) or (
            method == "GET" and re.fullmatch(r"actors/[^/]+/input", path)
            and isinstance(request_id,str) and 0 < len(request_id) <= 200 and payload is None)
        if not allowed or ".." in path.split("/"):
            raise ValueError("unsupported child input endpoint")
        if not self._bridge or not self.is_alive():
            raise RuntimeError("mimo ACP is unavailable")
        if root_session_id not in self._bridge.mapped_sessions():
            await self._bridge.ensure_session(root_session_id, owner=owner)
        root_native_id = self._bridge.mapped_session_id(root_session_id)
        root_workspace = self._bridge.mapped_session_workspace(root_session_id)
        children_path = session_http_path(root_native_id, "children?visible=true", root_workspace)
        async with self.internal_http_client(timeout=timeout) as client:
            children_response = await client.get(children_path)
            if children_response.status_code >= 400:
                return children_response
            try:
                children = children_response.json()
            except ValueError as exc:
                raise RuntimeError("managed engine returned invalid child membership data") from exc
            if not isinstance(children, list):
                raise RuntimeError("managed engine returned invalid child membership data")
            child = find_direct_peer(children, root_native_id, child_native_session_id)
            if child is None:
                raise PermissionError("managed child session is not a direct visible child of this chat")
            workspace = child.get("directory")
            if not isinstance(workspace, str) or not workspace:
                workspace = root_workspace
            if method == "GET":
                from urllib.parse import quote
                path += "?requestID=" + quote(request_id, safe="")
            child_path = session_http_path(child_native_session_id, path, workspace)
            kwargs = {"json": payload} if payload is not None else {}
            return await client.request(method, child_path, **kwargs)

    async def workflow_availability_request(
        self,
        root_session_id: str,
        target_native_session_id: str,
        *,
        owner: str,
        timeout: float = 20.0,
    ) -> httpx.Response:
        """Read workflow state for the mapped root or a proven direct peer child."""
        if not self._bridge or not self.is_alive():
            raise RuntimeError("mimo ACP is unavailable")
        if root_session_id not in self._bridge.mapped_sessions():
            await self._bridge.ensure_session(root_session_id, owner=owner)
        root_native_id = self._bridge.mapped_session_id(root_session_id)
        workspace = self._bridge.mapped_session_workspace(root_session_id)
        if target_native_session_id != root_native_id:
            children_path = session_http_path(root_native_id, "children?visible=true", workspace)
            async with self.internal_http_client(timeout=timeout) as client:
                children_response = await client.get(children_path)
                if children_response.status_code >= 400:
                    return children_response
                try:
                    children = children_response.json()
                except ValueError as exc:
                    raise RuntimeError("managed engine returned invalid child membership data") from exc
                child = find_direct_peer(children, root_native_id, target_native_session_id)
                if child is None:
                    raise PermissionError("managed child session is not a direct visible child of this chat")
                if isinstance(child.get("directory"), str) and child["directory"]:
                    workspace = child["directory"]
        params = httpx.QueryParams({"sessionID": target_native_session_id, "directory": workspace})
        async with self.internal_http_client(timeout=timeout) as client:
            return await client.get(httpx.URL("/workflows/availability").copy_merge_params(params))


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
    idle_timer: asyncio.TimerHandle | None = None
    unleased_access: bool = False
    idle_fence: str = ""


@dataclass(frozen=True)
class _QueuedRetirementLease:
    owner: str
    worker: object
    bridge: object
    holder: str
    generation: int
    epoch: int


_QUEUED_RESET_LEASE_SEAL = object()


@dataclass(eq=False)
class _QueuedResetLease:
    """Host-private reservation of one original current producer."""
    owner: str
    account_id: str
    worker: object
    bridge: object
    holder: str
    generation: int
    epoch: int
    process: object
    client: object
    claim: object
    inventory: object
    provider: object
    profile: object
    barrier: object
    authority: object
    target: object
    foreground_epoch: int
    preview_fingerprint: str
    source_hashes: frozenset
    witness: object
    local_epoch: int
    seal: object = field(repr=False)
    touched: bool = False
    waited_exit_code: object = None
    checkpoint_sha256: object = None
    closure: object = None
    finalization_epoch: object = None
    pending: object = None
    rebinding: bool = False


@dataclass(eq=False)
class _QueuedResetPending:
    method: str
    request: dict
    client: object
    task: object = None
    request_id: object = None
    issued: bool = False
    response_seen: bool = False
    response_error: bool = False


_QUEUED_RESET_CLOSURE_SEAL = object()


@dataclass(frozen=True)
class _QueuedResetClosure:
    lease: object
    process: object
    checkpoint_sha256: str
    seal: object = field(repr=False)


_ZERO_SOURCE_LEASE_SEAL = object()
_ZERO_SOURCE_CLOSURE_SEAL = object()

@dataclass(eq=False)
class _ZeroQueueSourceLease:
    """Source-only reset authority; it does not represent a worker retirement."""
    owner: str
    account_id: str
    claim: object
    inventory: object
    provider: object
    profile: object
    barrier: object
    authority: object
    target: object
    witness: object
    reset_transport_witness: object
    preview_fingerprint: str
    seal: object = field(repr=False)
    touched: bool = False
    reset_invocation_remaining: int = 256

@dataclass(frozen=True)
class _ZeroQueueSourceClosure:
    lease: object
    manifest_sha256: str
    seal: object = field(repr=False)


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
        gcf_preference=None,
        browser_snapshot_preference=None,
    ):
        self._pool = pool
        self.owner = owner
        self.worker = worker
        self._owner_epoch = owner_epoch
        self.projection_pending = projection_pending
        self._gcf_preference = gcf_preference
        self._browser_snapshot_preference = browser_snapshot_preference
        self._directory_token = None
        self._directory_process = None
        self._directory_issued = False
        self._directory_settled = False
        self.generation = getattr(worker, "installed_generation", 0)
        self.fingerprint = getattr(worker, "installed_fingerprint", "")
        self._released = False

    async def attach_direct_foreground(self, barrier, admission, account, runtime, handle, *, account_only=False):
        bridge = getattr(self.worker, "_bridge", None)
        if bridge is None or self._released:
            raise ValueError("direct foreground worker unavailable")
        return await bridge._attach_direct_foreground(self, barrier, admission, account, runtime, handle, account_only=account_only)

    def bind_directory(self, directory: str) -> None:
        if self._released or self._directory_token is not None:
            raise ValueError('worker directory admission unavailable')
        process = getattr(self.worker,'_proc',None)
        if process is None:
            raise ValueError('worker directory admission unavailable')
        self._directory_token = self._pool.try_enter_host_directory(directory,self)
        self._directory_process = process

    def bind_unknown_directory(self) -> None:
        if self._released or self._directory_token is not None:
            raise ValueError('worker directory admission unavailable')
        process = getattr(self.worker,'_proc',None)
        if process is None:
            raise ValueError('worker directory admission unavailable')
        self._directory_token = self._pool.try_enter_unknown_host_directory(self)
        self._directory_process = process

    def mark_directory_issued(self) -> None:
        if self._released or self._directory_token is None or self._directory_issued:
            raise ValueError('worker directory admission unavailable')
        self._directory_issued = True

    def mark_directory_settled(self) -> None:
        if self._released or self._directory_token is None or not self._directory_issued:
            raise ValueError('worker directory admission unavailable')
        self._directory_settled = True

    async def release(self, *, successful_terminal: bool = False) -> None:
        if self._released:
            return
        if getattr(self.worker, "_private_runtime", False):
            await self.worker.stop()
            if self.worker.is_alive():
                raise RuntimeError("private agent cleanup unavailable")
        if self._directory_token is not None and self._directory_issued and not self._directory_settled:
            process = self._directory_process
            if process is None or process.returncode is None:
                raise RuntimeError('worker directory settlement unavailable')
            code = await process.wait()
            if type(code) is not int or process.returncode != code:
                raise RuntimeError('worker directory settlement unavailable')
        await self._pool._release_lease(self.owner, self.worker, successful_terminal, self._owner_epoch)
        if self._directory_token is not None:
            self._pool.release_host_directory(self._directory_token)
            self._directory_token = None
        self._released = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.release(successful_terminal=exc_type is None)


@dataclass(eq=False)
class _HostDirectoryClose:
    """Host-admission snapshot only; never a native-idle or cleanup proof."""
    directory: str
    token: object
    owner_lease: AgentWorkerLease
    workers: tuple
    tasks: tuple
    holder: object


class MimoSupervisorPool:
    """Lazy authenticated owner-keyed Open Clank agent runtimes."""

    def __init__(
        self,
        *,
        memory_provider=None,
        canonical_source_provider=None,
        safe_dirs: list[str] | None = None,
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
        if canonical_source_provider is not None:
            from src.memery_provider import MemeryProvider
            if not isinstance(canonical_source_provider, MemeryProvider) or not canonical_source_provider._guarded_v17:
                raise ValueError("canonical source provider unavailable")
        self._canonical_source_provider = canonical_source_provider
        self._safe_dirs = safe_dirs
        self._initial_owner = self._key(initial_owner) if initial_owner else ""
        self._host_provider_owner = self._key(host_provider_owner) if host_provider_owner else ""
        self._workers: dict[str, MimoSupervisor] = {}
        self._managed_policy_installers: dict[str, object] = {}
        self._history_leases: dict[tuple[str, str], AgentWorkerLease] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._states: dict[str, _OwnerLifecycle] = {}
        self._host_directory_closes: dict[object, _HostDirectoryClose] = {}
        self._host_directory_entries: dict[object, tuple[str | None, AgentWorkerLease]] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._background_task_owners: dict[asyncio.Task, str] = {}
        self._owner_lifecycle_epochs: dict[str, int] = {}
        self._owner_lifecycle_blocked: set[str] = set()
        self._queued_retirement_leases: dict[object, _QueuedRetirementLease] = {}
        self._queued_retirement_handler = None
        self._readiness_budget = max(0.0, float(readiness_budget))
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._local_executor_broker = local_executor_broker
        from src.constants import DATA_DIR
        from src.openclank.permission_grants import GrantStore

        root = Path(data_dir) if data_dir is not None else Path(DATA_DIR)
        self._agent_runtime_root = root / "runtime" / "agent-engine"
        if (root / "mimocode").exists():
            raise RuntimeError("Agent runtime data does not match this release. Stop writers and keep a complete backup; restore the matching release or prepare an offline conversion.")
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

    def _rewrite_session_map_owner(
        self,
        owner: str,
        new_owner: str,
        *,
        path: Path | None = None,
    ) -> None:
        """Rewrite the owner-qualified map after an owner partition move.

        The lifecycle guards are held by the caller.  A rename moves the
        directory atomically, but the map entries carry the old owner inside
        the JSON envelope, so the destination must be rewritten before it can
        be admitted by a bridge for the new owner.  This is retry-safe: a
        crash after the directory move leaves the destination in place and a
        later reconciliation rewrites it again.
        """
        map_path = path or (self._runtime_home(owner) / "session-map.json")
        if not map_path.exists():
            return
        mapping = __import__(
            "src.openclank.session_map", fromlist=["OwnerSessionMap"]
        ).OwnerSessionMap(map_path, owner)
        try:
            data = mapping._read_unlocked()
            try:
                raw_data = json.loads(map_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw_data = None
            if isinstance(raw_data, dict) and "version" not in raw_data:
                # Flat v1 maps have no embedded owner; validation above is
                # sufficient and their bytes must remain unchanged.
                return
            if (
                isinstance(raw_data, dict)
                and raw_data.get("version") == 2
                and isinstance(raw_data.get("chats"), dict)
            ):
                # Preserve optional envelope fields exactly so compensation
                # can restore the pre-move fingerprint byte-for-byte.
                data = raw_data
        except Exception:
            if owner == new_owner:
                raise
            # A crash after the directory move and before the metadata
            # rewrite leaves a valid destination map carrying the source
            # owner. Accept that exact transitional state for recovery.
            mapping = __import__(
                "src.openclank.session_map", fromlist=["OwnerSessionMap"]
            ).OwnerSessionMap(map_path, new_owner)
            data = mapping._read_unlocked()
            try:
                raw_data = json.loads(map_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw_data = None
            if isinstance(raw_data, dict) and "version" not in raw_data:
                return
            if (
                isinstance(raw_data, dict)
                and raw_data.get("version") == 2
                and isinstance(raw_data.get("chats"), dict)
            ):
                data = raw_data
        for item in data.get("chats", {}).values():
            item["owner"] = new_owner
        mapping.owner = new_owner
        mapping._write(data)

    def _owner_memory_data_dirs(self, owner: str) -> list[Path]:
        """Return only this owner's managed-engine data roots.

        Authenticated installs are confined to the exact owner's private
        runtime root. Auth-disabled installs have exactly one local runtime,
        so their unpartitioned engine data is that local user's data.
        """
        pass
        root = self._runtime_home(owner)
        if root.is_symlink():
            raise RuntimeError("agent owner runtime root cannot be a symlink")
        if not root.exists():
            return []
        candidates = [root / "openclank-engine" / "data"]
        generations = root / "generations"
        if generations.is_symlink():
            raise RuntimeError("agent generation root cannot be a symlink")
        if generations.is_dir():
            for generation in sorted(generations.iterdir(), key=lambda item: item.name):
                if generation.is_symlink():
                    raise RuntimeError("agent generation cannot be a symlink")
                if generation.is_dir():
                    candidates.append(generation / "openclank-engine" / "data")
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
            database = data_dir / "openclank.db"
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
        if not key:
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
        if not key:
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
                        database = data_dir / "openclank.db"
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
                self._unblock_owner_lifecycle(lifecycle_key)

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

    @staticmethod
    def _host_directory_path(directory: str) -> str:
        if (type(directory) is not str or not directory or '\0' in directory
                or not os.path.isabs(directory) or os.path.normpath(directory) != directory):
            raise ValueError('canonical directory unavailable')
        return directory

    @staticmethod
    def _host_directory_intersects(left: str, right: str) -> bool:
        try:
            common = os.path.commonpath((left,right))
            return common == left or common == right
        except ValueError:
            return True

    def _host_directory_loop(self) -> None:
        if self._event_loop is None or asyncio.get_running_loop() is not self._event_loop:
            raise RuntimeError('host directory gate requires the pool owner loop')

    def _assert_host_directory_admission(self, directory: str | None) -> None:
        if not self._host_directory_closes:
            return
        self._host_directory_loop()
        if directory is None:
            raise SupervisorAdmissionError('DIRECTORY_CLOSING','Managed directory is unavailable',phase='directory',retryable=True)
        value = self._host_directory_path(directory)
        if any(self._host_directory_intersects(value,close.directory) for close in self._host_directory_closes.values()):
            raise SupervisorAdmissionError('DIRECTORY_CLOSING','Managed directory is unavailable',phase='directory',retryable=True)

    def try_enter_host_directory(self, directory: str, lease: AgentWorkerLease) -> object:
        """Hold a host entry only; native directory ownership is separate."""
        self._host_directory_loop()
        value = self._host_directory_path(directory)
        if type(lease) is not AgentWorkerLease or lease._pool is not self or lease._released:
            raise ValueError('original worker lease unavailable')
        self._assert_owner_lifecycle(lease.owner,lease._owner_epoch)
        state = self._states.get(lease.owner)
        if state is None or state.in_flight.get(lease.worker,0) < 1 or not lease.worker.is_alive():
            raise ValueError('original worker lease unavailable')
        self._assert_host_directory_admission(value)
        token = object(); self._host_directory_entries[token] = (value,lease)
        return token

    def try_enter_unknown_host_directory(self, lease: AgentWorkerLease) -> object:
        """Track a host use whose final native directory is not classified."""
        self._host_directory_loop()
        if type(lease) is not AgentWorkerLease or lease._pool is not self or lease._released:
            raise ValueError('original worker lease unavailable')
        self._assert_owner_lifecycle(lease.owner,lease._owner_epoch)
        state = self._states.get(lease.owner)
        if state is None or state.in_flight.get(lease.worker,0) < 1 or not lease.worker.is_alive():
            raise ValueError('original worker lease unavailable')
        self._assert_host_directory_admission(None)
        token = object(); self._host_directory_entries[token] = (None,lease)
        return token

    def release_host_directory(self, token: object) -> bool:
        self._host_directory_loop()
        return self._host_directory_entries.pop(token,None) is not None

    def try_close_host_directory(self, directory: str, lease: AgentWorkerLease) -> _HostDirectoryClose:
        """Capture a closing fence; the result proves HOST ADMISSION ONLY."""
        self._host_directory_loop()
        value = self._host_directory_path(directory)
        if type(lease) is not AgentWorkerLease or lease._pool is not self or lease._released:
            raise ValueError('original worker lease unavailable')
        self._assert_owner_lifecycle(lease.owner,lease._owner_epoch)
        if any(entry[0] is None or self._host_directory_intersects(value,entry[0]) for entry in self._host_directory_entries.values()):
            raise ValueError('managed host directory is busy')
        self._assert_host_directory_admission(value)
        workers = []
        owners = set(self._states) | set(self._workers)
        if self._owner_lifecycle_blocked or self._queued_retirement_leases:
            raise ValueError('managed worker transition unavailable')
        for owner in sorted(owners):
            state = self._states.get(owner)
            if state is None or state.status not in ('ready','stopped'):
                raise ValueError('managed worker transition unavailable')
            known = set(state.in_flight) | ({state.active} if state.active is not None else set())
            if owner in self._workers: known.add(self._workers[owner])
            for worker in known:
                count = state.in_flight.get(worker,0)
                process,bridge = getattr(worker,'_proc',None),getattr(worker,'_bridge',None)
                if (type(count) is not int or count < 0 or not worker.is_alive() or process is None or bridge is None
                        or getattr(worker,'_owner',None) != owner):
                    raise ValueError('managed worker inventory unavailable')
                workers.append((owner,state,worker,process,bridge,getattr(worker,'installed_generation',None),
                    getattr(worker,'_lifecycle_epoch',None),count,worker is state.active,worker is self._workers.get(owner),
                    self._owner_lifecycle_epoch(owner)))
        owner_state = self._states.get(lease.owner)
        if owner_state is None or owner_state.in_flight.get(lease.worker,0) < 1:
            raise ValueError('original worker lease unavailable')
        tasks = tuple((task,owner) for task,owner in self._background_task_owners.items() if not task.done())
        if (any(not owner for _,owner in tasks)
                or any(not task.done() and task not in self._background_task_owners for task in self._background_tasks)):
            raise ValueError('managed task inventory unavailable')
        holder = getattr(getattr(lease.worker, '_bridge', None), '_managed_provider_generation', None)
        token = object(); close = _HostDirectoryClose(value,token,lease,tuple(workers),tasks,holder)
        self._host_directory_closes[token] = close
        return close

    def host_directory_close_current(self, close: _HostDirectoryClose) -> bool:
        """Check original pool inventory; never report native actors idle."""
        self._host_directory_loop()
        if (type(close) is not _HostDirectoryClose or self._host_directory_closes.get(close.token) is not close
                or close.owner_lease._released or close.owner_lease._pool is not self): return False
        try:
            self._assert_owner_lifecycle(close.owner_lease.owner,close.owner_lease._owner_epoch)
            current = []
            for owner in sorted(set(self._states) | set(self._workers)):
                state = self._states.get(owner)
                if state is None or state.status not in ('ready','stopped'): return False
                known = set(state.in_flight) | ({state.active} if state.active is not None else set())
                if owner in self._workers: known.add(self._workers[owner])
                for worker in known:
                    current.append((owner,state,worker,getattr(worker,'_proc',None),getattr(worker,'_bridge',None),
                        getattr(worker,'installed_generation',None),getattr(worker,'_lifecycle_epoch',None),
                        state.in_flight.get(worker,0),worker is state.active,worker is self._workers.get(owner),
                        self._owner_lifecycle_epoch(owner)))
            tasks = tuple((task,owner) for task,owner in self._background_task_owners.items() if not task.done())
            if (any(not owner for _,owner in tasks)
                    or any(not task.done() and task not in self._background_task_owners for task in self._background_tasks)):
                return False
            stamp = lambda row: (row[0],id(row[1]),id(row[2]),id(row[3]),id(row[4]),*row[5:7],*row[8:])
            original = {stamp(row):row[7] for row in close.workers}
            counts_ok = all(stamp(row) in original and type(row[7]) is int and 0 <= row[7] <= original[stamp(row)] for row in current)
            owner_row = next((row for row in current if row[2] is close.owner_lease.worker and row[0] == close.owner_lease.owner),None)
            return (not self._owner_lifecycle_blocked and not self._queued_retirement_leases
                and len(current) == len(close.workers) and counts_ok and owner_row is not None and owner_row[7] >= 1
                and {(id(task),owner) for task,owner in tasks} <= {(id(task),owner) for task,owner in close.tasks}
                and all(row[2].is_alive() and row[3] is not None and row[4] is not None for row in current)
                and not any(p[0] is None or self._host_directory_intersects(close.directory,p[0]) for p in self._host_directory_entries.values()))
        except Exception:
            return False

    def release_host_directory_close(self, close: _HostDirectoryClose) -> bool:
        self._host_directory_loop()
        if type(close) is not _HostDirectoryClose or self._host_directory_closes.get(close.token) is not close:
            return False
        del self._host_directory_closes[close.token]
        return True

    def single_normal_directory_close_current(self, close: _HostDirectoryClose) -> bool:
        """First cleanup profile: one original normal worker, host admission only."""
        if not self.host_directory_close_current(close) or len(close.workers) != 1 or close.tasks:
            return False
        lease = close.owner_lease
        owner, state, worker, process, bridge, generation, epoch, count, active, published, owner_epoch = close.workers[0]
        return bool(
            owner == lease.owner and worker is lease.worker and active and published
            and state.status == 'ready' and state.active is worker
            and getattr(worker, '_private_runtime', None) is False
            and process is getattr(worker, '_proc', None) and process.returncode is None
            and bridge is getattr(worker, '_bridge', None)
            and close.holder and getattr(bridge, '_managed_provider_generation', None) == close.holder
            and close.holder == getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None)
            and generation == lease.generation == state.generation
            and epoch == getattr(worker, '_lifecycle_epoch', None)
            and owner_epoch == lease._owner_epoch and count == 1
            and state.in_flight.get(worker, 0) == 1
        )

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

    def _unblock_owner_lifecycle(self, owner: str) -> None:
        # A reset reservation owns its exclusion until its exact release.
        if any(type(lease) is _QueuedResetLease and lease.owner == owner
               for lease in self._queued_retirement_leases.values()):
            return
        self._owner_lifecycle_blocked.discard(owner)

    def canonical_source_provider(self):
        """Return the host-injected source owner without changing recall routing."""
        return self._canonical_source_provider

    def _new_worker(self, owner: str, snapshot, generation: int, fence: str, lifecycle_epoch: str | None = None, *, private_runtime: bool = False) -> MimoSupervisor:
        runtime_home = None
        runtime_home = self._runtime_home(owner) / "generations" / f"{generation}-{fence}"
        worker = MimoSupervisor(
            owner=owner,
            memory_provider=self._memory_provider,
            safe_dirs=self._safe_dirs,
            runtime_home=runtime_home,
            partitioned=True,
            grant_store=self._grant_store,
            projection_snapshot=snapshot,
            projection_generation=generation,
            crash_callback=self._worker_crashed,
            local_executor_broker=self._local_executor_broker,
            lifecycle_epoch=lifecycle_epoch if lifecycle_epoch is not None else self._durable_owner_epoch(owner),
            private_runtime=private_runtime,
        )
        worker._s05_foreground_runtime = getattr(self, '_s05_foreground_runtime', None)
        worker._s05_profile = getattr(self, '_s05_profile', None)
        worker._managed_policy_installer = self._managed_policy_installers.get(self._key(owner))
        return worker

    def set_managed_policy_installer(self, owner: str, installer) -> None:
        if not callable(installer):
            raise ValueError("managed policy installer is required")
        key = self._key(owner)
        self._managed_policy_installers[key] = installer
        worker = self._workers.get(key)
        if worker is not None:
            worker._managed_policy_installer = installer
            if worker._bridge is not None:
                installer(worker._bridge)

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

    async def _ensure_worker(self, owner: str, *, unleased: bool = False) -> MimoSupervisor:
        from src.openclank.mimo_projection import (
            build_projection_snapshot,
            mark_projection,
            reconcile_projection,
            safe_additive_delta,
        )

        self._assert_host_directory_admission(None)
        epoch = self._owner_lifecycle_epoch(owner)
        self._assert_owner_lifecycle(owner, epoch)
        lock = self._owner_lock(owner)
        async with lock:
            self._assert_host_directory_admission(None)
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
                    state.unleased_access |= unleased
                    self._arm_idle_timer(owner, state, active)
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
                reuse_home = (state.idle_fence and old_snapshot is not None
                              and old_snapshot.fingerprint == snapshot.fingerprint and state.generation == generation)
                fence = state.idle_fence if reuse_home else uuid.uuid4().hex[:12]
                state.idle_fence = ""
                state.status = "swapping" if old else "starting"
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

                if self._host_directory_closes:
                    await candidate.stop()
                    raise SupervisorAdmissionError('DIRECTORY_CLOSING','Managed directory is unavailable',phase='directory',retryable=True)
                candidate.installed_fingerprint = current.fingerprint
                candidate.installed_generation = int(current_state["generation"])
                state.active = candidate
                state.snapshot = current
                state.generation = candidate.installed_generation
                state.status = "ready"
                state.last_failure = None
                self._workers[owner] = candidate
                state.unleased_access = unleased
                self._arm_idle_timer(owner, state, candidate)
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
        if not key:
            raise RuntimeError("authenticated Open Clank agent execution requires an owner")
        return await self._ensure_worker(key, unleased=True)

    async def execute_operation(self, owner: str | None, payload: dict, *, utility=None) -> dict:
        """Run one typed operation on a generation-pinned owner worker."""
        lease = await self.admit_provider_control(owner)
        successful = False
        try:
            if utility is not None:
                worker = lease.worker
                if getattr(utility,'owned_send',None) is not None:
                    from src.openclank.acp_client import ACPClient
                    if type(worker) is not MimoSupervisor or type(getattr(worker,'_client',None)) is not ACPClient:
                        raise RuntimeError('owned utility writer unavailable')
                bridge = getattr(worker, '_bridge', None)
                holder = getattr(bridge, '_managed_provider_generation', None)
                if (utility.owner != lease.owner or not utility.account_current()
                    or not await utility.barrier.is_live(utility.owner, utility.admission)
                    or utility.cancel_requested or not bridge or not holder
                    or getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) != holder
                    or getattr(worker, '_owner', None) != lease.owner or getattr(bridge, '_owner', None) != lease.owner
                    or getattr(worker, '_proc', None) is None or worker._proc.returncode is not None
                    or type(getattr(worker, 'installed_generation', None)) is not int or worker.installed_generation < 1
                    or lease.generation != worker.installed_generation or type(lease._owner_epoch) is not int or lease._owner_epoch < 0
                    or getattr(worker, '_client', None) is None or getattr(worker, '_private_runtime', False)
                    or getattr(worker, '_utility_barrier', utility.barrier) is not utility.barrier):
                    raise RuntimeError('utility lifetime authority unavailable')
                utility.worker, utility.bridge, utility.holder = worker, bridge, holder
                utility.generation, utility.epoch, utility.process = lease.generation, lease._owner_epoch, worker._proc
                utility.lease, utility.client = lease, worker._client
                worker._utility_barrier = utility.barrier
            if "modelRequestOptions" in (payload.get("options") or {}):
                result = await (lease.worker.execute_prepared_option_operation(payload, utility=utility) if utility is not None
                                else lease.worker.execute_prepared_option_operation(payload))
            else:
                result = await (lease.worker.execute_cancellable_operation(payload, utility=utility) if utility is not None
                                else lease.worker.execute_cancellable_operation(payload))
            successful = True
            return result
        finally:
            if utility is None:
                await lease.release(successful_terminal=successful)
            elif utility.lease is None:
                # This branch did not cross a send boundary.
                await lease.release(successful_terminal=False)

    async def admit_provider_control(self, owner: str | None) -> AgentWorkerLease:
        """Pin one owner worker generation for a provider mutation/OAuth flow."""

        self._assert_host_directory_admission(None)
        key = self._key(owner)
        if not key:
            raise RuntimeError("authenticated provider control requires an owner")
        pass
        epoch = self._owner_lifecycle_epoch(key)
        self._assert_owner_lifecycle(key, epoch)
        while True:
            worker = await self._ensure_worker(key)
            async with self._owner_lock(key):
                self._assert_host_directory_admission(None)
                self._assert_owner_lifecycle(key, epoch)
                state = self._owner_state(key)
                if state.active is not worker or not worker.is_alive():
                    continue
                state.in_flight[worker] = state.in_flight.get(worker, 0) + 1
                state.drain_events.setdefault(worker, asyncio.Event()).clear()
                lease = AgentWorkerLease(self,key,worker,owner_epoch=epoch)
                try:
                    lease.bind_unknown_directory()
                except BaseException:
                    state.in_flight[worker] -= 1
                    if state.in_flight[worker] == 0: state.drain_events[worker].set()
                    raise
                return lease

    async def effective_agent_settings(self, owner: str, provider_id: str, model_id: str, *, directory: str, session_id: str | None = None) -> dict:
        """Read nonsecret effective settings from the admitted native worker."""
        lease = await self.admit_agent(owner, provider_id, model_id)
        try:
            return await lease.worker.managed_engine_call(
                "_openclank/session/v1/settings/effective",
                {"providerID": provider_id, "modelID": model_id, "directory": directory, **({"sessionID": session_id} if session_id else {})},
            )
        finally:
            await lease.release()

    async def ready_refresh_commands(self, owner: str, *, session_id: str, reload_skills: bool = False) -> dict:
        """Refresh a ready target's catalog without creating an owner worker."""
        key = self._key(owner)
        async with self._owner_lock(key):
            epoch = self._owner_lifecycle_epoch(key)
            self._assert_owner_lifecycle(key, epoch)
            state = self._owner_state(key)
            worker = state.active
            if worker is None or not worker.is_alive():
                raise SupervisorAdmissionError("WORKER_NOT_READY", "owner worker is not ready", phase="catalog", retryable=True)
            bridge = getattr(worker, "_bridge", None)
            if bridge is None or session_id not in bridge.mapped_sessions(owner=key):
                raise SupervisorAdmissionError("SESSION_NOT_READY", "chat is not admitted", phase="catalog", retryable=True)
            directory = bridge.mapped_session_workspace(session_id)
            native_id = bridge.mapped_session_id(session_id)
            state.in_flight[worker] = state.in_flight.get(worker, 0) + 1
            state.drain_events.setdefault(worker, asyncio.Event()).clear()
        lease = AgentWorkerLease(self, key, worker, owner_epoch=epoch)
        try:
            lease.bind_directory(os.path.realpath(directory,strict=True))
            result = await worker.managed_engine_call("_openclank/session/v1/commands/refresh", {"directory": directory, "sessionID": native_id, "reloadSkills": reload_skills})
            self._assert_owner_lifecycle(key, epoch)
            if bridge.mapped_session_id(session_id) != native_id or bridge.mapped_session_workspace(session_id) != directory:
                raise SupervisorAdmissionError("SESSION_NOT_READY", "chat changed during refresh", phase="catalog", retryable=True)
            return {"commands": result.get("availableCommands", []), "generation": lease.generation}
        finally:
            await lease.release()

    async def ready_effective_agent_settings(self, owner: str, provider_id: str, model_id: str, *, session_id: str) -> dict:
        """Read settings only from an already-ready generation; never cold-start."""
        key = self._key(owner)
        async with self._owner_lock(key):
            # Capture the lifecycle epoch while the owner lock is held.  The
            # lease must release against this admission, even if account
            # fencing advances the durable epoch immediately after publish.
            owner_epoch = self._owner_lifecycle_epoch(key)
            self._assert_owner_lifecycle(key, owner_epoch)
            state = self._owner_state(key)
            worker = state.active
            if worker is None or not worker.is_alive():
                raise SupervisorAdmissionError("SUPERVISOR_UNAVAILABLE", "owner worker is not ready", phase="readback", retryable=True)
            if state.snapshot is None or state.snapshot.run_closure(provider_id, model_id) is None:
                raise SupervisorAdmissionError("MODEL_NOT_PROJECTED", "selected model is not ready", phase="readback", retryable=False)
            bridge = getattr(worker, "_bridge", None)
            if bridge is None or session_id not in bridge.mapped_sessions(owner=key):
                raise SupervisorAdmissionError("SESSION_NOT_READY", "selected chat is not admitted on this generation", phase="readback", retryable=True)
            # This is observational: the mapping validates its durable owner,
            # workspace and revisions and cannot create a replacement session.
            directory = bridge.mapped_session_workspace(session_id)
            native_id = bridge.mapped_session_id(session_id)
            state.in_flight[worker] = state.in_flight.get(worker, 0) + 1
            state.drain_events.setdefault(worker, asyncio.Event()).clear()
        lease = AgentWorkerLease(self, key, worker, owner_epoch=owner_epoch)
        try:
            lease.bind_directory(os.path.realpath(directory,strict=True))
            result = await worker.managed_engine_call("_openclank/session/v1/settings/effective", {"providerID": provider_id, "modelID": model_id, "directory": directory, "sessionID": native_id})
            self._assert_owner_lifecycle(key, owner_epoch)
            if bridge.mapped_session_id(session_id) != native_id or bridge.mapped_session_workspace(session_id) != directory:
                raise SupervisorAdmissionError("SESSION_NOT_READY", "selected chat changed during readback", phase="readback", retryable=True)
            # Physical cwd and engine IDs stay private; stable host target
            # identities are added by the authenticated route.
            result = dict(result)
            result.pop("target", None)
            from src.agent_settings import agent_settings_revision, validate_agent_settings
            from routes.prefs_routes import _load_for_user
            projected = ((state.snapshot.providers.get(provider_id) or {}).get("options") or {}).get("_openclankAgentSettings", {})
            projected_revision = agent_settings_revision(projected)
            result["generation"] = lease.generation
            result["projected_revision"] = projected_revision
            result["projection_pending"] = projected_revision != agent_settings_revision(validate_agent_settings((_load_for_user(key) or {}).get("agent_settings", {})))
            result["apply"] = "next_admission"
            return result
        finally:
            await lease.release()

    async def admit_agent(self, owner: str | None, provider_id: str, model_id: str) -> AgentWorkerLease:
        """Reconcile projection and acquire one generation-scoped Agent lease."""
        from src.openclank.mimo_projection import build_projection_snapshot, safe_additive_delta

        self._assert_host_directory_admission(None)
        key = self._key(owner)
        if not key:
            raise SupervisorAdmissionError(
                "SUPERVISOR_UNAVAILABLE", "Authenticated Agent execution requires an owner",
                phase="identity", retryable=False,
            )
        pass
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
            self._assert_host_directory_admission(None)
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
            from src.agent_settings import admitted_gcf_preference, admitted_browser_snapshot_preference
            lease = AgentWorkerLease(
                self,
                key,
                worker,
                owner_epoch=epoch,
                projection_pending=pending,
                gcf_preference=admitted_gcf_preference(snapshot,provider_id,model_id),
                browser_snapshot_preference=admitted_browser_snapshot_preference(snapshot,provider_id,model_id),
            )
        return lease

    async def admit_private_agent(self, owner: str, ghost_id: str, provider_id: str, model_id: str) -> AgentWorkerLease:
        """Prepare one private process; C2 and public chat routing are separate gates."""
        from core.models import get_session_manager_instance
        from core.database import SessionLocal, Session as DBSession
        from src.openclank.mimo_projection import build_projection_snapshot, reconcile_projection

        self._assert_host_directory_admission(None)
        key = self._key(owner)
        manager = get_session_manager_instance()
        ghost = getattr(manager, "sessions", {}).get(ghost_id)
        if not key or ghost is None or getattr(ghost, "owner", None) != key or not getattr(ghost, "incognito", False):
            raise SupervisorAdmissionError("SESSION_NOT_READY", "private session is unavailable", phase="identity", retryable=False)
        db = SessionLocal()
        try:
            if db.query(DBSession.id).filter(DBSession.id == ghost_id).first() is not None:
                raise SupervisorAdmissionError("SESSION_NOT_READY", "private session is unavailable", phase="identity", retryable=False)
        finally:
            db.close()
        epoch = self._owner_lifecycle_epoch(key)
        async with self._owner_lock(key):
            self._assert_host_directory_admission(None)
            self._assert_owner_lifecycle(key, epoch)
            state = self._owner_state(key)
            snapshot = build_projection_snapshot(key)
            desired = reconcile_projection(snapshot, materializing=True)
            if snapshot.run_closure(provider_id, model_id) is None:
                raise SupervisorAdmissionError("MODEL_NOT_PROJECTED", "private model is unavailable", phase="routing", retryable=False)
            if getattr(manager, "sessions", {}).get(ghost_id) is not ghost:
                raise SupervisorAdmissionError("SESSION_NOT_READY", "private session changed", phase="identity", retryable=False)
            if any(getattr(worker, "_private_runtime", False) and count for worker, count in state.in_flight.items()):
                raise SupervisorAdmissionError("SUPERVISOR_UNAVAILABLE", "private worker is already in use", phase="admission", retryable=True)
            worker = self._new_worker(key, snapshot, int(desired["generation"]), uuid.uuid4().hex[:12], private_runtime=True)
            worker._private_ghost = ghost
            worker._private_ghost_id = ghost_id
            state.in_flight[worker] = 1
            state.drain_events.setdefault(worker, asyncio.Event()).clear()
            try:
                await worker.start()
                self._assert_host_directory_admission(None)
                self._assert_owner_lifecycle(key, epoch)
                latest = build_projection_snapshot(key)
                if latest.fingerprint != snapshot.fingerprint or latest.run_closure(provider_id, model_id) != snapshot.run_closure(provider_id, model_id):
                    raise SupervisorAdmissionError("MODEL_NOT_PROJECTED", "private model projection changed", phase="routing", retryable=True)
                if getattr(manager, "sessions", {}).get(ghost_id) is not ghost:
                    raise SupervisorAdmissionError("SESSION_NOT_READY", "private session changed", phase="identity", retryable=False)
                available = {str(item.get("modelId")) for item in (worker.available_models() or []) if item.get("modelId")}
                if f"{provider_id}/{model_id}" not in available:
                    raise SupervisorAdmissionError("MODEL_NOT_PROJECTED", "private model is unavailable", phase="routing", retryable=False)
                from src.agent_settings import admitted_gcf_preference, admitted_browser_snapshot_preference
                return AgentWorkerLease(self, key, worker, owner_epoch=epoch,
                    gcf_preference=admitted_gcf_preference(snapshot,provider_id,model_id),
                    browser_snapshot_preference=admitted_browser_snapshot_preference(snapshot,provider_id,model_id))
            except BaseException:
                try:
                    await asyncio.shield(worker.stop())
                    if worker.is_alive():
                        raise RuntimeError("private agent cleanup unavailable")
                except BaseException:
                    logger.warning("private agent cleanup unavailable")
                else:
                    state.in_flight.pop(worker, None)
                    state.drain_events.pop(worker, None)
                raise

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
                if getattr(worker, "_private_runtime", False):
                    state.in_flight.pop(worker, None)
                    state.drain_events.pop(worker, None)
                elif state.active is worker:
                    self._arm_idle_timer(owner, state, worker)
            if successful_terminal:
                state.breaker_open_until.pop(getattr(worker, "installed_fingerprint", ""), None)

    def _arm_idle_timer(self, owner, state, worker) -> None:
        if state.idle_timer is not None:
            state.idle_timer.cancel()
            state.idle_timer = None
        if state.unleased_access or state.in_flight.get(worker, 0):
            return

        def expired():
            state.idle_timer = None
            self._schedule_background(self._retire_idle(owner, state, worker), owner=owner)

        state.idle_timer = asyncio.get_running_loop().call_later(_IDLE_WORKER_GRACE, expired)

    async def _retire_idle(self, owner, state, worker) -> None:
        # Serialize retirement with admission, projection replacement and account changes.
        async with self._owner_lock(owner):
            if (self._states.get(owner) is not state or state.active is not worker
                or state.idle_timer is not None or state.in_flight.get(worker, 0)
                or state.unleased_access or state.status != "ready"
                or owner in self._owner_lifecycle_blocked or self._host_directory_closes
                or worker in self._queued_retirement_leases):
                return
            bridge = worker._bridge
            client = worker._client
            if (bridge is None or client is None or not worker.is_alive()
                or client._pending or client._callback_tasks or bridge._session_context
                or bridge._turns or bridge._pending_session_results
                or bridge._prepared_managed_handoffs or bridge._managed_actor_turns
                or bridge._managed_actor_executions or bridge._kernel_scopes
                or bridge.terminal_manager._terminals):
                return
            # A bound native session can own queued memory, terminals, or an
            # actor after its host lease ends. Keep its generation until the
            # normal session lifecycle has released those obligations.
            async def retire():
                await worker.stop()
                await worker._teardown_child()

            retirement = asyncio.create_task(retire())
            try:
                await asyncio.shield(retirement)
            except asyncio.CancelledError:
                await retirement
                raise
            except Exception:
                logger.exception("Idle engine cleanup failed")
                return
            self._workers.pop(owner, None)
            state.active = None
            state.in_flight.pop(worker, None)
            state.drain_events.pop(worker, None)
            state.idle_fence = state.fence
            state.status = "stopped"

    def set_queued_retirement_handler(self, handler) -> None:
        """Install or clear the app-owned closure handler; never starts work."""
        if handler is not None and not callable(handler):
            raise ValueError('queued retirement handler unavailable')
        self._queued_retirement_handler = handler

    @staticmethod
    def _queued_reset_foreground(profile):
        from src.openclank.memory_job_admission import _conn
        profile.assert_current()
        with _conn(profile.path) as db:
            row=db.execute('SELECT verified,coverage_generation,clock_generation,foreground_epoch FROM fm_v2_s05_occupancy WHERE singleton=1').fetchone()
            if (not row or tuple(row[:3])!=(1,profile.coverage_generation,profile.clock_generation)
                or type(row[3]) is not int or row[3]<0
                or db.execute("SELECT 1 FROM fm_v2_s05_foreground WHERE state IN ('active','unknown') LIMIT 1").fetchone()
                or db.execute("SELECT 1 FROM fm_v2_s05_reservations WHERE state IN ('reserved','dispatched','uncertain') LIMIT 1").fetchone()):
                raise ValueError('queued reset occupancy unavailable')
            return row[3]

    def assert_queued_reset_lease(self, lease, *, claim, source_hash=None, _rebinding=False):
        if type(lease) is _ZeroQueueSourceLease:
            if not self.queued_reset_lease_current(lease,claim=claim,source_hash=source_hash):
                raise ValueError('zero queue source authority unavailable')
            claim.assert_current(owner=lease.owner,account_id=lease.account_id,provider=lease.provider,operation_id=claim.operation_id)
            lease.inventory.assert_current();lease.profile.assert_current()
            lease.provider._assert_reset_retirement_ready(lease.reset_transport_witness)
            if (not lease.barrier.reset_witness_current(lease.witness) or lease.authority.resolve(lease.account_id)!=lease.target
                    or getattr(claim._coordinator._reset_state,'s05_profile',None) is not lease.profile
                    or getattr(claim._coordinator._reset_state,'account_request_barrier',None) is not lease.barrier
                    or not self.queued_reset_lease_current(lease,claim=claim,source_hash=source_hash)):
                raise ValueError('zero queue source authority changed')
            return lease
        from services.memory.nuke_coordinator import MemoryResetClaim
        from services.memory.skill_lifecycle import locked
        from src.openclank.conversation_archive import QueuedProducerInventory
        from src.openclank.memory_job_admission import SingleHostProfile
        if (type(lease) is not _QueuedResetLease or lease.seal is not _QUEUED_RESET_LEASE_SEAL
            or type(claim) is not MemoryResetClaim or lease.claim is not claim or (lease.rebinding and not _rebinding)
            or type(lease.inventory) is not QueuedProducerInventory or lease.inventory.claim is not claim
            or type(lease.profile) is not SingleHostProfile or lease.profile.provider is not lease.provider
            or getattr(claim._coordinator._reset_state,'s05_profile',None) is not lease.profile
            or getattr(claim._coordinator._reset_state,'account_request_barrier',None) is not lease.barrier
            or not lease.barrier.reset_witness_current(lease.witness)
            or self._queued_retirement_leases.get(lease.worker) is not lease
            or lease.owner not in self._owner_lifecycle_blocked
            or self._owner_lifecycle_epoch(lease.owner)!=lease.epoch
            or self._durable_owner_epoch(lease.owner)!=lease.target.durable_epoch
            or (source_hash is not None and source_hash not in lease.source_hashes)):
            raise ValueError('queued reset lease unavailable')
        state=self._states.get(lease.owner)
        if (state is None or state.active is not lease.worker or self._workers.get(lease.owner) is not lease.worker
            or set(state.in_flight)!={lease.worker} or state.in_flight.get(lease.worker)!=0
            or getattr(lease.worker,'_proc',None) is not lease.process or not lease.worker.is_alive()
            or getattr(lease.worker,'_client',None) is not lease.client
            or getattr(lease.worker,'_bridge',None) is not lease.bridge
            or getattr(lease.bridge,'_managed_provider_generation',None)!=lease.holder
            or getattr(getattr(lease.worker,'_managed_callbacks',None),'holder_id',None)!=lease.holder
            or getattr(lease.worker,'installed_generation',None)!=lease.generation):
            raise ValueError('queued reset worker changed')
        claim.assert_current(owner=lease.owner,account_id=lease.account_id,provider=lease.provider,operation_id=claim.operation_id)
        lease.inventory.assert_current()
        with locked(os.fspath(claim._coordinator.lock_target)):
            row=claim._coordinator._load_journal().get(claim.operation_id)
            if (type(row) is not dict or row.get('queued_expected')!=json.loads(lease.inventory.expected_json)
                or row.get('queued_profile')!={'version':1,'fingerprint':lease.preview_fingerprint}):
                raise ValueError('queued reset preview changed')
        if self._queued_reset_foreground(lease.profile)!=lease.foreground_epoch:
            raise ValueError('queued reset occupancy changed')
        if not lease.barrier.reset_witness_current(lease.witness):
            raise ValueError('queued reset drain changed')
        return lease

    def queued_reset_lease_current(self, lease, *, claim, source_hash=None, _rebinding=False):
        """Cheap exact local identity check, never a substitute for full validation."""
        if type(lease) is _ZeroQueueSourceLease:
            return bool(lease.seal is _ZERO_SOURCE_LEASE_SEAL and claim is lease.claim and source_hash is None
                and getattr(lease.witness,'owner',None)==lease.owner
                and lease.barrier.reset_witness_revision_current(lease.witness)
                and lease.provider._reset_retirement_witness is lease.reset_transport_witness
                and lease.provider._reset_retirement_ready_local())
        if (type(lease) is not _QueuedResetLease or lease.seal is not _QUEUED_RESET_LEASE_SEAL
            or lease.claim is not claim or (lease.rebinding and not _rebinding) or self._queued_retirement_leases.get(lease.worker) is not lease
            or lease.owner not in self._owner_lifecycle_blocked
            or self._owner_lifecycle_epochs.get(lease.owner,0)!=lease.local_epoch
            or not lease.barrier.reset_witness_revision_current(lease.witness)
            or (source_hash is not None and source_hash not in lease.source_hashes)):
            return False
        state=self._states.get(lease.owner)
        return bool(state is not None and state.active is lease.worker and self._workers.get(lease.owner) is lease.worker
            and set(state.in_flight)=={lease.worker} and state.in_flight.get(lease.worker)==0
            and getattr(lease.worker,'_proc',None) is lease.process and lease.worker.is_alive()
            and getattr(lease.worker,'_client',None) is lease.client
            and getattr(lease.worker,'_bridge',None) is lease.bridge
            and getattr(lease.bridge,'_managed_provider_generation',None)==lease.holder
            and getattr(getattr(lease.worker,'_managed_callbacks',None),'holder_id',None)==lease.holder
            and getattr(lease.worker,'installed_generation',None)==lease.generation)

    async def validate_queued_reset_lease(self, lease, *, claim, source_hash=None, _rebinding=False):
        """Drain the exact full validator even if the caller is cancelled."""
        if not self.queued_reset_lease_current(lease,claim=claim,source_hash=source_hash,_rebinding=_rebinding):
            raise ValueError('queued reset lease unavailable')
        result=await _queued_owned_thread(self.assert_queued_reset_lease,lease,claim=claim,source_hash=source_hash,_rebinding=_rebinding)
        if not self.queued_reset_lease_current(lease,claim=claim,source_hash=source_hash,_rebinding=_rebinding):
            raise ValueError('queued reset lease changed')
        return result

    async def issue_zero_queue_source_lease(self, *, claim, inventory, target_authority, profile, barrier, witness):
        from services.memory.nuke_coordinator import MemoryResetClaim
        from src.openclank.account_request_barrier import AccountRequestBarrier
        from src.openclank.conversation_archive import QueuedProducerInventory
        from src.openclank.memory_job_admission import SingleHostProfile, TargetAuthority
        if (type(claim) is not MemoryResetClaim or type(inventory) is not QueuedProducerInventory or inventory.claim is not claim
                or type(profile) is not SingleHostProfile or profile.provider is not inventory.provider
                or type(target_authority) is not TargetAuthority or target_authority._supervisor is not self
                or type(barrier) is not AccountRequestBarrier):
            raise ValueError('zero queue source authority unavailable')
        expected=json.loads(inventory.expected_json);payload=json.loads(inventory.payload_json)
        if (expected.get('count')!=0 or payload!={'version':1,'eventCount':0,'producers':[],'sources':[]}
                or type(expected.get('fingerprint')) is not str):
            raise ValueError('zero queue source inventory unavailable')
        provider=inventory.provider;transport_witness=provider._reset_retirement_witness
        if transport_witness is None: raise ValueError('zero queue provider unavailable')
        target=target_authority.resolve(claim.account_id)
        if (getattr(witness,'owner',None)!=claim.owner or target.source_account_id!=claim.account_id
                or target.memory_owner!=claim.owner):
            raise ValueError('zero queue source scope unavailable')
        lease=_ZeroQueueSourceLease(claim.owner,claim.account_id,claim,inventory,provider,profile,barrier,target_authority,
            target,witness,transport_witness,expected['fingerprint'],_ZERO_SOURCE_LEASE_SEAL)
        await _queued_owned_thread(self.assert_queued_reset_lease,lease,claim=claim)
        return lease

    async def close_zero_queue_source_lease(self, lease, *, claim):
        from services.memory.skill_lifecycle import locked
        await _queued_owned_thread(self.assert_queued_reset_lease,lease,claim=claim)
        with locked(os.fspath(claim._coordinator.lock_target)):
            row=claim._coordinator._load_journal().get(claim.operation_id)
            manifest=row.get('reset_source_manifest') if type(row) is dict else None
        if (type(manifest) is not dict or manifest.get('version')!=2 or manifest.get('kind')!='zero_queue'
                or manifest.get('accountID')!=lease.account_id or manifest.get('operationID')!=claim.operation_id
                or manifest.get('queuedSHA256')!=lease.inventory.payload_sha256 or manifest.get('previewSHA256')!=lease.preview_fingerprint):
            raise ValueError('zero queue source manifest unavailable')
        digest=hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        return _ZeroQueueSourceClosure(lease,digest,_ZERO_SOURCE_CLOSURE_SEAL)

    async def issue_queued_reset_lease(self, *, claim, inventory, target_authority, profile, barrier):
        """Reserve only the current verified producer after claim-bound drain."""
        from services.memory.nuke_coordinator import MemoryResetClaim
        from services.memory.skill_lifecycle import locked
        from src.openclank.account_request_barrier import AccountRequestBarrier
        from src.openclank.conversation_archive import QueuedProducerInventory
        from src.openclank.memory_job_admission import SingleHostProfile, TargetAuthority
        if (type(claim) is not MemoryResetClaim or type(inventory) is not QueuedProducerInventory
            or inventory.claim is not claim or type(profile) is not SingleHostProfile
            or profile.provider is not inventory.provider or type(target_authority) is not TargetAuthority
            or target_authority._supervisor is not self or type(barrier) is not AccountRequestBarrier
            or getattr(claim._coordinator._reset_state,'s05_profile',None) is not profile
            or getattr(claim._coordinator._reset_state,'account_request_barrier',None) is not barrier):
            raise ValueError('queued reset authority unavailable')
        owner=claim.owner; account=claim.account_id; provider=inventory.provider
        def check():
            claim.assert_current(owner=owner,account_id=account,provider=provider,operation_id=claim.operation_id)
            inventory.assert_current(); profile.assert_current()
            with locked(os.fspath(claim._coordinator.lock_target)):
                row=claim._coordinator._load_journal().get(claim.operation_id)
                if (type(row) is not dict or row.get('queued_expected')!=json.loads(inventory.expected_json)
                    or row.get('queued_account_id')!=account or type(row.get('queued_profile')) is not dict
                    or set(row['queued_profile'])!={'version','fingerprint'} or row['queued_profile'].get('version')!=1):
                    raise ValueError('queued reset preview unavailable')
                return row['queued_profile']
        frozen=await _queued_owned_thread(check)
        data=json.loads(inventory.payload_json); sources=data.get('sources'); producers=data.get('producers')
        if (type(sources) is not list or not 1<=len(sources)<=128 or type(producers) is not list or len(producers)!=1
            or any(type(s) is not dict or s.get('association')!='verified' or s.get('producer')!=producers[0] for s in sources)):
            raise ValueError('queued reset producer unavailable')
        hashes=frozenset(s['sourceIdentitySHA256'] for s in sources)
        if len(hashes)!=len(sources): raise ValueError('queued reset producer unavailable')
        target=target_authority.resolve(account)
        callback=getattr(claim._coordinator._reset_state,'queued_reset_preview',None)
        if not callable(callback): raise ValueError('queued reset preview unavailable')
        preview=await callback(owner=owner,account_id=account,operation_id=claim.operation_id,
                               expected=json.loads(inventory.expected_json),provider=provider)
        if preview!=frozen or await _queued_owned_thread(check)!=frozen: raise ValueError('queued reset preview changed')
        foreground=await _queued_owned_thread(self._queued_reset_foreground,profile)
        preview=await callback(owner=owner,account_id=account,operation_id=claim.operation_id,
                               expected=json.loads(inventory.expected_json),provider=provider)
        if (preview!=frozen or await _queued_owned_thread(check)!=frozen
            or await _queued_owned_thread(self._queued_reset_foreground,profile)!=foreground):
            raise ValueError('queued reset preview changed')
        witness=await barrier.reset_drain_witness(owner)
        async with self._owner_lock(owner):
            with self._owner_map_guards([owner]):
                state=self._states.get(owner); worker=state.active if state is not None else None
                bridge=getattr(worker,'_bridge',None); holder=getattr(bridge,'_managed_provider_generation',None)
                generation=getattr(worker,'installed_generation',None); epoch=self._owner_lifecycle_epoch(owner)
                process=getattr(worker,'_proc',None)
                client=getattr(worker,'_client',None)
                if (owner in self._owner_lifecycle_blocked or worker is None or self._workers.get(owner) is not worker
                    or set(state.in_flight)!={worker} or state.in_flight.get(worker)!=0 or not worker.is_alive()
                    or getattr(worker,'_private_runtime',False) or bridge is None or process is None or client is None
                    or not isinstance(holder,str) or not holder or type(generation) is not int
                    or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder
                    or any(l.owner==owner for l in self._queued_retirement_leases.values())
                    or any(o==owner and not t.done() for t,o in self._background_task_owners.items())
                    or any(k[0]==owner for k in self._history_leases)
                    or barrier._owner_revisions.get(owner,0)!=witness.revision or witness.caller.released
                    or producers[0]!={'version':1,'holderSHA256':hashlib.sha256(holder.encode()).hexdigest(),'generation':generation,'ownerEpoch':epoch}
                    or target.owner_epoch!=epoch or target.durable_epoch!=self._durable_owner_epoch(owner)):
                    raise ValueError('queued reset worker unavailable')
                lease=_QueuedResetLease(owner,account,worker,bridge,holder,generation,epoch,process,client,claim,inventory,
                    provider,profile,barrier,target_authority,target,foreground,frozen['fingerprint'],hashes,witness,
                    self._owner_lifecycle_epochs.get(owner,0),_QUEUED_RESET_LEASE_SEAL)
                self._owner_lifecycle_blocked.add(owner)
                self._queued_retirement_leases[worker]=lease
                return lease

    async def refresh_queued_reset_witness(self, lease, *, claim):
        """Rebind an exact same-operation caller after another actual drain."""
        from services.memory.nuke_coordinator import MemoryResetClaim
        if (type(lease) is not _QueuedResetLease or lease.seal is not _QUEUED_RESET_LEASE_SEAL
            or type(claim) is not MemoryResetClaim or lease.rebinding
            or claim._coordinator is not lease.claim._coordinator or claim.owner!=lease.owner
            or claim.account_id!=lease.account_id or claim.operation_id!=lease.claim.operation_id
            or self._queued_retirement_leases.get(lease.worker) is not lease):
            raise ValueError('queued reset lease unavailable')
        lease.rebinding=True; old=lease.witness
        try:
            for current_claim in (lease.claim,claim):
                await _queued_owned_thread(current_claim.assert_current,owner=lease.owner,account_id=lease.account_id,
                    provider=lease.provider,operation_id=claim.operation_id)
            await _queued_owned_thread(lease.inventory.assert_current)
            witness=await lease.barrier.reset_drain_witness(lease.owner)
            if (not await _queued_owned_thread(lease.barrier.reset_witness_current,witness)
                or self._queued_retirement_leases.get(lease.worker) is not lease):
                raise ValueError('queued reset drain changed')
            lease.witness=witness
            await self.validate_queued_reset_lease(lease,claim=lease.claim,_rebinding=True)
            await _queued_owned_thread(claim.assert_current,owner=lease.owner,account_id=lease.account_id,
                provider=lease.provider,operation_id=claim.operation_id)
            return witness
        except BaseException:
            lease.witness=old
            raise
        finally:
            lease.rebinding=False

    async def release_queued_reset_lease(self, lease):
        """Release only an untouched exact reservation; uncertainty stays fenced."""
        if type(lease) is not _QueuedResetLease or lease.seal is not _QUEUED_RESET_LEASE_SEAL:
            return False
        try:
            await self.validate_queued_reset_lease(lease,claim=lease.claim)
        except Exception:
            return False
        async with self._owner_lock(lease.owner):
            with self._owner_map_guards([lease.owner]):
                state=self._states.get(lease.owner)
                if (lease.touched or self._queued_retirement_leases.get(lease.worker) is not lease
                    or lease.barrier._owner_revisions.get(lease.owner,0)!=lease.witness.revision
                    or lease.witness.caller.released
                    or self._owner_lifecycle_epoch(lease.owner)!=lease.epoch
                    or state is None or state.active is not lease.worker or self._workers.get(lease.owner) is not lease.worker
                    or set(state.in_flight)!={lease.worker} or state.in_flight.get(lease.worker)!=0
                    or getattr(lease.worker,'_proc',None) is not lease.process
                    or getattr(lease.worker,'_client',None) is not lease.client
                    or getattr(lease.worker,'_bridge',None) is not lease.bridge
                    or getattr(lease.bridge,'_managed_provider_generation',None)!=lease.holder
                    or getattr(lease.worker,'installed_generation',None)!=lease.generation): return False
                self._queued_retirement_leases.pop(lease.worker)
                self._owner_lifecycle_blocked.discard(lease.owner)
                return True

    async def resume_queued_reset_lease(self, *, claim, inventory):
        """Find only the same in-process operation and refresh its caller witness."""
        from services.memory.nuke_coordinator import MemoryResetClaim
        from src.openclank.conversation_archive import QueuedProducerInventory
        if type(claim) is not MemoryResetClaim or type(inventory) is not QueuedProducerInventory or inventory.claim is not claim:
            raise ValueError('queued reset resume unavailable')
        matches=[lease for lease in tuple(self._queued_retirement_leases.values())
            if type(lease) is _QueuedResetLease and lease.owner==claim.owner and lease.account_id==claim.account_id
            and lease.claim._coordinator is claim._coordinator and lease.claim.operation_id==claim.operation_id]
        if len(matches)!=1: raise ValueError('queued reset resume unavailable')
        lease=matches[0]; original=lease.inventory
        if (inventory.provider is not lease.provider or inventory.archive is not original.archive
            or inventory.archive_path!=original.archive_path or inventory.expected_json!=original.expected_json
            or inventory.payload_json!=original.payload_json or inventory.payload_sha256!=original.payload_sha256):
            raise ValueError('queued reset inventory changed')
        await _queued_owned_thread(inventory.assert_current)
        await self.refresh_queued_reset_witness(lease,claim=claim)
        return lease

    def _settle_queued_reset_pending(self, lease):
        """Consume only a finished, exact native response (or no-send failure)."""
        from src.openclank.acp_client import RPCError
        pending=lease.pending
        if pending is None: return None
        if type(pending) is not _QueuedResetPending or pending.task is None or not pending.task.done():
            raise ValueError('queued native mutation unresolved')
        if pending.issued and not pending.response_seen:
            raise ValueError('queued native mutation unresolved')
        try:
            result=pending.task.result()
        except RPCError:
            if not pending.response_seen or not pending.response_error: raise ValueError('queued native mutation unresolved')
            lease.pending=None
            raise
        except BaseException:
            if not pending.issued: lease.pending=None
            raise ValueError('queued native mutation unresolved')
        lease.pending=None
        return result

    async def queued_reset_mutate(self, lease, method, request):
        """Own one native call on the lease; never replay an unresolved send."""
        if method!='_openclank/session/v1/queued-source/retire-marker' or type(request) is not dict:
            raise ValueError('queued native mutation unavailable')
        await self.validate_queued_reset_lease(lease,claim=lease.claim)
        if lease.pending is not None:
            raise ValueError('queued native mutation unresolved')
        client=lease.client
        pending=_QueuedResetPending(method,dict(request),client)
        lease.pending=pending
        lease.touched=True
        pending.task=asyncio.create_task(lease.worker.managed_engine_call(method,request,queued_pending=pending))
        try:
            await asyncio.wait_for(asyncio.shield(pending.task),timeout=5)
        except (asyncio.TimeoutError,asyncio.CancelledError):
            raise
        except Exception:
            pass
        return self._settle_queued_reset_pending(lease)

    async def record_queued_reset_progress(self, lease, *, source_hash, state):
        """Checkpoint only exact observed per-source retirement progress."""
        from services.memory.skill_lifecycle import locked
        if source_hash not in lease.source_hashes or state not in ('fenced','completed'):
            raise ValueError('queued reset progress unavailable')
        await self.validate_queued_reset_lease(lease,claim=lease.claim,source_hash=source_hash)
        def write():
            self.assert_queued_reset_lease(lease,claim=lease.claim,source_hash=source_hash)
            claim=lease.claim
            with locked(os.fspath(claim._coordinator.lock_target)):
                journal=claim._coordinator._load_journal();row=journal.get(claim.operation_id)
                commit=row.get('commit_claim') if type(row) is dict else None
                if (type(row) is not dict or row.get('owner')!=lease.owner or row.get('account_id')!=lease.account_id
                    or row.get('state')!='committing' or row.get('guarded_reset') is not True or row.get('result') is not None
                    or type(commit) is not dict or commit.get('coordinator_id')!=claim._coordinator._commit_claim_id
                    or commit.get('host')!=socket.gethostname() or commit.get('pid')!=os.getpid()
                    or row.get('queued_expected')!=json.loads(lease.inventory.expected_json)
                    or row.get('queued_profile')!={'version':1,'fingerprint':lease.preview_fingerprint}):
                    raise ValueError('queued reset progress changed')
                progress=row.get('queued_holder_progress',{})
                if (type(progress) is not dict or any(k not in lease.source_hashes or v not in ('fenced','completed')
                    for k,v in progress.items())): raise ValueError('queued reset progress unavailable')
                progress=dict(progress);old=progress.get(source_hash)
                if old!='completed': progress[source_hash]=state
                row['queued_holder_progress']=dict(sorted(progress.items()));claim._coordinator._save_journal(journal)
            return True
        await _queued_owned_thread(write)
        await self.validate_queued_reset_lease(lease,claim=lease.claim,source_hash=source_hash)

    async def checkpoint_queued_reset_retirement(self, lease, *, marker_sha256, fence_sha256, preflight_sha256):
        """Wait this exact child, then journal content-free closure; never reclaim."""
        from services.memory.skill_lifecycle import locked
        if any(type(v) is not str or re.fullmatch(r'[0-9a-f]{64}',v) is None
               for v in (marker_sha256,fence_sha256,preflight_sha256)):
            raise ValueError('queued reset closure unavailable')
        await self.validate_queued_reset_lease(lease,claim=lease.claim)
        if not lease.touched: raise ValueError('queued reset closure unavailable')
        process=lease.process
        async def drain(task):
            cancelled=False
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: cancelled=True
            return task.result(),cancelled
        stopped,cancelled=await drain(asyncio.create_task(self._stop_exact_worker(lease.worker,process)))
        if stopped is not True or type(process.returncode) is not int:
            raise ValueError('queued reset child settlement unavailable')
        lease.waited_exit_code=process.returncode
        def checkpoint():
            claim=lease.claim; inventory=lease.inventory
            claim.assert_current(owner=lease.owner,account_id=lease.account_id,provider=lease.provider,operation_id=claim.operation_id)
            inventory.assert_current();lease.profile.assert_current()
            state=self._states.get(lease.owner)
            if (self._queued_retirement_leases.get(lease.worker) is not lease or not lease.touched
                or lease.owner not in self._owner_lifecycle_blocked or self._owner_lifecycle_epoch(lease.owner)!=lease.epoch
                or self._durable_owner_epoch(lease.owner)!=lease.target.durable_epoch
                or state is None or state.active is not lease.worker or self._workers.get(lease.owner) is not lease.worker
                or getattr(lease.worker,'_proc',None) not in (None,process)
                or not lease.barrier.reset_witness_current(lease.witness)
                or self._queued_reset_foreground(lease.profile)!=lease.foreground_epoch):
                raise ValueError('queued reset stopped authority changed')
            record={'version':1,'accountSHA256':hashlib.sha256(lease.account_id.encode()).hexdigest(),
                'holderSHA256':hashlib.sha256(lease.holder.encode()).hexdigest(),'generation':lease.generation,
                'ownerEpoch':lease.epoch,'foregroundEpoch':lease.foreground_epoch,'inventorySHA256':inventory.payload_sha256,
                'previewSHA256':lease.preview_fingerprint,'markersSHA256':marker_sha256,'fencesSHA256':fence_sha256,
                'preflightSHA256':preflight_sha256,'exitCode':process.returncode}
            encoded=json.dumps(record,sort_keys=True,separators=(',',':')).encode()
            with locked(os.fspath(claim._coordinator.lock_target)):
                journal=claim._coordinator._load_journal();row=journal.get(claim.operation_id)
                commit=row.get('commit_claim') if type(row) is dict else None
                if (type(row) is not dict or row.get('owner')!=lease.owner or row.get('account_id')!=lease.account_id
                    or row.get('state')!='committing' or row.get('guarded_reset') is not True or row.get('result') is not None
                    or type(commit) is not dict or commit.get('coordinator_id')!=claim._coordinator._commit_claim_id
                    or commit.get('host')!=socket.gethostname() or commit.get('pid')!=os.getpid()
                    or row.get('queued_expected')!=json.loads(inventory.expected_json)
                    or row.get('queued_profile')!={'version':1,'fingerprint':lease.preview_fingerprint}
                    or row.get('queued_holder_retirement') not in (None,record)):
                    raise ValueError('queued reset checkpoint changed')
                row['queued_holder_retirement']=record;claim._coordinator._save_journal(journal)
            claim.assert_current(owner=lease.owner,account_id=lease.account_id,provider=lease.provider,operation_id=claim.operation_id)
            if not lease.barrier.reset_witness_current(lease.witness): raise ValueError('queued reset drain changed')
            return _QueuedResetClosure(lease,process,hashlib.sha256(encoded).hexdigest(),_QUEUED_RESET_CLOSURE_SEAL)
        proof,interrupted=await drain(asyncio.create_task(asyncio.to_thread(checkpoint)))
        lease.checkpoint_sha256=proof.checkpoint_sha256
        lease.closure=proof
        if cancelled or interrupted: raise asyncio.CancelledError
        return proof

    async def resume_queued_reset_closure(self, lease, *, claim):
        """Revalidate an already-journaled same-process closure in this host."""
        from services.memory.skill_lifecycle import locked
        if (type(lease) is not _QueuedResetLease or lease.seal is not _QUEUED_RESET_LEASE_SEAL
            or lease.claim is not claim or type(lease.waited_exit_code) is not int
            or type(lease.checkpoint_sha256) is not str):
            raise ValueError('queued reset closure unavailable')
        def verify():
            claim.assert_current(owner=lease.owner,account_id=lease.account_id,provider=lease.provider,operation_id=claim.operation_id)
            lease.inventory.assert_current();lease.profile.assert_current()
            state=self._states.get(lease.owner)
            if (self._queued_retirement_leases.get(lease.worker) is not lease or not lease.touched
                or lease.owner not in self._owner_lifecycle_blocked or self._owner_lifecycle_epoch(lease.owner)!=lease.epoch
                or self._durable_owner_epoch(lease.owner)!=lease.target.durable_epoch or state is None
                or state.active is not lease.worker or self._workers.get(lease.owner) is not lease.worker
                or getattr(lease.worker,'_proc',None) not in (None,lease.process)
                or lease.process.returncode!=lease.waited_exit_code
                or not lease.barrier.reset_witness_current(lease.witness)
                or self._queued_reset_foreground(lease.profile)!=lease.foreground_epoch):
                raise ValueError('queued reset closure changed')
            with locked(os.fspath(claim._coordinator.lock_target)):
                row=claim._coordinator._load_journal().get(claim.operation_id)
                record=row.get('queued_holder_retirement') if type(row) is dict else None
                if type(record) is not dict: raise ValueError('queued reset checkpoint unavailable')
                digest=hashlib.sha256(json.dumps(record,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                if digest!=lease.checkpoint_sha256: raise ValueError('queued reset checkpoint changed')
            claim.assert_current(owner=lease.owner,account_id=lease.account_id,provider=lease.provider,operation_id=claim.operation_id)
            if not lease.barrier.reset_witness_current(lease.witness): raise ValueError('queued reset drain changed')
            return _QueuedResetClosure(lease,lease.process,digest,_QUEUED_RESET_CLOSURE_SEAL)
        return await _queued_owned_thread(verify)

    def queued_reset_closure_current(self, closure, *, claim):
        """Cheap same-process stopped identity; full proof is checked separately."""
        if type(closure) is _ZeroQueueSourceClosure:
            return bool(closure.seal is _ZERO_SOURCE_CLOSURE_SEAL and self.queued_reset_lease_current(closure.lease,claim=claim))
        from services.memory.nuke_coordinator import MemoryResetClaim
        if type(closure) is not _QueuedResetClosure or closure.seal is not _QUEUED_RESET_CLOSURE_SEAL: return False
        lease=closure.lease
        if (type(claim) is not MemoryResetClaim or type(lease) is not _QueuedResetLease or lease.seal is not _QUEUED_RESET_LEASE_SEAL or lease.rebinding
            or getattr(claim,'_coordinator',None) is not lease.claim._coordinator
            or getattr(claim,'owner',None)!=lease.owner or getattr(claim,'account_id',None)!=lease.account_id
            or getattr(claim,'operation_id',None)!=lease.claim.operation_id
            or self._queued_retirement_leases.get(lease.worker) is not lease or not lease.touched
            or lease.owner not in self._owner_lifecycle_blocked or self._owner_lifecycle_epochs.get(lease.owner,0)!=lease.local_epoch
            or not lease.barrier.reset_witness_revision_current(lease.witness)
            or closure.process is not lease.process or closure.checkpoint_sha256!=lease.checkpoint_sha256
            or type(lease.waited_exit_code) is not int or lease.process.returncode!=lease.waited_exit_code): return False
        state=self._states.get(lease.owner)
        return bool(state is not None and state.active is lease.worker and self._workers.get(lease.owner) is lease.worker
            and getattr(lease.worker,'_proc',None) in (None,lease.process))

    async def refresh_stopped_queued_reset_closure(self, closure, *, claim, inventory):
        """Rebind only an exact same-operation caller to an already stopped holder."""
        from services.memory.nuke_coordinator import MemoryResetClaim
        from src.openclank.conversation_archive import QueuedProducerInventory
        if type(closure) is not _QueuedResetClosure or closure.seal is not _QUEUED_RESET_CLOSURE_SEAL:
            raise ValueError('queued stopped closure unavailable')
        lease=closure.lease
        if (type(claim) is not MemoryResetClaim or type(inventory) is not QueuedProducerInventory or inventory.claim is not claim
            or lease.rebinding or claim._coordinator is not lease.claim._coordinator or claim.owner!=lease.owner
            or claim.account_id!=lease.account_id or claim.operation_id!=lease.claim.operation_id
            or inventory.provider is not lease.provider or inventory.archive is not lease.inventory.archive
            or inventory.expected_json!=lease.inventory.expected_json or inventory.payload_json!=lease.inventory.payload_json
            or inventory.payload_sha256!=lease.inventory.payload_sha256 or inventory.archive_path!=lease.inventory.archive_path
            or self._queued_retirement_leases.get(lease.worker) is not lease):
            raise ValueError('queued stopped closure unavailable')
        lease.rebinding=True;old=lease.witness
        try:
            for c in (lease.claim,claim):
                await _queued_owned_thread(c.assert_current,owner=lease.owner,account_id=lease.account_id,provider=lease.provider,operation_id=claim.operation_id)
            await _queued_owned_thread(inventory.assert_current)
            witness=await lease.barrier.reset_drain_witness(lease.owner)
            if not await _queued_owned_thread(lease.barrier.reset_witness_current,witness): raise ValueError('queued reset drain changed')
            lease.witness=witness
            result=await self.resume_queued_reset_closure(lease,claim=lease.claim)
            await _queued_owned_thread(claim.assert_current,owner=lease.owner,account_id=lease.account_id,provider=lease.provider,operation_id=claim.operation_id)
            return result
        except BaseException:
            lease.witness=old
            raise
        finally: lease.rebinding=False

    def assert_queued_reset_closure(self, closure, *, claim):
        """Full stopped proof for an owned worker thread or guarded transaction."""
        from services.memory.skill_lifecycle import locked
        if type(closure) is _ZeroQueueSourceClosure:
            if not self.queued_reset_closure_current(closure,claim=claim): raise ValueError('zero queue source closure unavailable')
            lease=closure.lease;self.assert_queued_reset_lease(lease,claim=claim)
            with locked(os.fspath(claim._coordinator.lock_target)):
                row=claim._coordinator._load_journal().get(claim.operation_id)
                manifest=row.get('reset_source_manifest') if type(row) is dict else None
            if (type(manifest) is not dict or manifest.get('version')!=2 or manifest.get('kind')!='zero_queue'
                    or hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()!=closure.manifest_sha256):
                raise ValueError('zero queue source manifest changed')
            return closure
        if not self.queued_reset_closure_current(closure,claim=claim): raise ValueError('queued stopped closure unavailable')
        lease=closure.lease
        for c in (lease.claim,claim):
            c.assert_current(owner=lease.owner,account_id=lease.account_id,provider=lease.provider,operation_id=claim.operation_id)
        lease.inventory.assert_current();lease.profile.assert_current()
        if (self._owner_lifecycle_epoch(lease.owner)!=lease.epoch
            or self._durable_owner_epoch(lease.owner)!=lease.target.durable_epoch
            or not lease.barrier.reset_witness_current(lease.witness)
            or self._queued_reset_foreground(lease.profile)!=lease.foreground_epoch):
            raise ValueError('queued stopped closure changed')
        with locked(os.fspath(claim._coordinator.lock_target)):
            row=claim._coordinator._load_journal().get(claim.operation_id)
            record=row.get('queued_holder_retirement') if type(row) is dict else None
            if type(record) is not dict or hashlib.sha256(json.dumps(record,sort_keys=True,separators=(',',':')).encode()).hexdigest()!=closure.checkpoint_sha256:
                raise ValueError('queued stopped checkpoint changed')
        if not self.queued_reset_closure_current(closure,claim=claim) or not lease.barrier.reset_witness_current(lease.witness):
            raise ValueError('queued stopped closure changed')
        return closure

    async def validate_queued_reset_closure(self, closure, *, claim):
        return await _queued_owned_thread(self.assert_queued_reset_closure,closure,claim=claim)

    def assert_post_dispatch_queued_reset(self, closure, *, claim, frozen):
        """Validate the original stopped owner after native source erasure."""
        from services.memory.skill_lifecycle import locked
        if type(frozen) is not dict or not self.queued_reset_closure_current(closure,claim=claim): raise ValueError('post-reset closure unavailable')
        lease=closure.lease
        for current in (lease.claim,claim):
            current.assert_current(owner=lease.owner,account_id=lease.account_id,provider=lease.provider,operation_id=claim.operation_id)
        lease.profile.assert_current()
        state=claim._coordinator._reset_state
        prepared=getattr(lease.provider,'_guarded_preparation',None)
        db=frozen.get('database')
        if (getattr(state,'s05_profile',None) is not lease.profile
            or getattr(state,'account_request_barrier',None) is not lease.barrier or type(db) is not dict
            or prepared is None or db.get('path')!=lease.profile.path or db.get('device')!=lease.profile.device
            or db.get('inode')!=lease.profile.inode or db.get('database_id')!=getattr(prepared,'database_id',None)
            or db.get('schema_version')!=getattr(prepared,'schema_version',None) or db.get('schema_version') not in (17,18)):
            raise ValueError('post-reset profile changed')
        if (self._owner_lifecycle_epoch(lease.owner)!=lease.epoch or self._durable_owner_epoch(lease.owner)!=lease.target.durable_epoch
            or not lease.barrier.reset_witness_current(lease.witness)
            or self._queued_reset_foreground(lease.profile)!=lease.foreground_epoch):
            raise ValueError('post-reset authority changed')
        with locked(os.fspath(claim._coordinator.lock_target)):
            row=claim._coordinator._load_journal().get(claim.operation_id)
            checkpoint=row.get('queued_holder_retirement') if type(row) is dict else None
            manifest=row.get('reset_source_manifest') if type(row) is dict else None
            if (type(checkpoint) is not dict or type(manifest) is not dict or row.get('reset_native_started') is not True
                or getattr(lease,'reset_native_frozen',None)!=frozen
                or row.get('reset_native_frozen')!=frozen or row.get('reset_origins_complete') is not True
                or row.get('reset_queued_complete') is not True
                or manifest.get('queuedSHA256')!=lease.inventory.payload_sha256
                or manifest.get('previewSHA256')!=lease.preview_fingerprint
                or row.get('reset_source_manifest_sha256')!=getattr(lease,'reset_manifest_sha256',None)
                or hashlib.sha256(json.dumps(checkpoint,sort_keys=True,separators=(',',':')).encode()).hexdigest()!=closure.checkpoint_sha256
                or hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()!=row.get('reset_source_manifest_sha256')):
                raise ValueError('post-reset checkpoint changed')
        if not self.queued_reset_closure_current(closure,claim=claim) or not lease.barrier.reset_witness_current(lease.witness):
            raise ValueError('post-reset authority changed')
        return closure

    async def validate_post_dispatch_queued_reset(self, closure, *, claim, frozen):
        return await _queued_owned_thread(self.assert_post_dispatch_queued_reset,closure,claim=claim,frozen=frozen)

    async def refresh_post_dispatch_queued_reset(self, *, claim, frozen):
        """Refresh only the caller witness, never rebuild erased source authority."""
        matches=[lease for lease in tuple(self._queued_retirement_leases.values())
            if type(lease) is _QueuedResetLease and lease.owner==claim.owner and lease.account_id==claim.account_id
            and lease.claim._coordinator is claim._coordinator and lease.claim.operation_id==claim.operation_id]
        if len(matches)!=1 or type(matches[0].closure) is not _QueuedResetClosure: raise ValueError('post-reset closure unavailable')
        lease=matches[0]
        if lease.rebinding: raise ValueError('post-reset closure unavailable')
        lease.rebinding=True;old=lease.witness
        try:
            for current in (lease.claim,claim):
                await _queued_owned_thread(current.assert_current,owner=lease.owner,account_id=lease.account_id,provider=lease.provider,operation_id=claim.operation_id)
            lease.witness=await lease.barrier.reset_drain_witness(lease.owner)
            lease.rebinding=False
            return await self.validate_post_dispatch_queued_reset(lease.closure,claim=claim,frozen=frozen)
        except BaseException:
            lease.witness=old;raise
        finally: lease.rebinding=False

    async def resume_stopped_queued_reset(self, *, claim, inventory):
        matches=[lease for lease in tuple(self._queued_retirement_leases.values())
            if type(lease) is _QueuedResetLease and lease.owner==claim.owner and lease.account_id==claim.account_id
            and lease.claim._coordinator is claim._coordinator and lease.claim.operation_id==claim.operation_id]
        if len(matches)!=1 or type(matches[0].closure) is not _QueuedResetClosure:
            raise ValueError('queued stopped closure unavailable')
        return await self.refresh_stopped_queued_reset_closure(matches[0].closure,claim=claim,inventory=inventory)

    async def finalize_queued_reset_closure(self, closure, *, claim, result, accumulated_counts):
        """Journal intent, advance exact epoch once, then publish and clean up."""
        from services.memory.skill_lifecycle import locked
        if (type(closure) is not _QueuedResetClosure or type(result) is not dict or result.get('complete') is not True
            or result.get('operation_id')!=claim.operation_id or type(result.get('receipt')) is not dict
            or type(result['receipt'].get('attempt')) is not int or result['receipt']['attempt']<1
            or type(accumulated_counts) is not dict):
            raise ValueError('queued reset finalization unavailable')
        lease=closure.lease
        frozen=getattr(lease,'reset_native_frozen',None)
        if frozen is None: await self.validate_queued_reset_closure(closure,claim=claim)
        else: await self.validate_post_dispatch_queued_reset(closure,claim=claim,frozen=frozen)
        raw=json.dumps(result,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
        intent={'version':1,'checkpointSHA256':closure.checkpoint_sha256,'resultSHA256':hashlib.sha256(raw).hexdigest(),
                'oldEpoch':lease.epoch,'nextEpoch':lease.epoch+1}
        def prepare():
            if frozen is None: self.assert_queued_reset_closure(closure,claim=claim)
            else: self.assert_post_dispatch_queued_reset(closure,claim=claim,frozen=frozen)
            with locked(os.fspath(claim._coordinator.lock_target)):
                journal=claim._coordinator._load_journal();row=journal.get(claim.operation_id)
                if frozen is not None and (type(getattr(lease,'reset_native_result',None)) is not dict
                    or row.get('reset_native_result')!=lease.reset_native_result):
                    raise ValueError('reset cleanup proof unavailable')
                current_claim=row.get('commit_claim') if type(row) is dict else None
                if (type(row) is not dict or row.get('owner')!=lease.owner or row.get('account_id')!=lease.account_id
                    or row.get('state')!='committing' or row.get('guarded_reset') is not True or row.get('result') is not None
                    or type(current_claim) is not dict or current_claim.get('coordinator_id')!=claim._coordinator._commit_claim_id
                    or current_claim.get('host')!=socket.gethostname() or current_claim.get('pid')!=os.getpid()
                    or row.get('queued_finalization_intent') not in (None,intent)
                    or row.get('queued_finalization_result') not in (None,result)
                    or row.get('queued_finalization_counts') not in (None,accumulated_counts)):
                    raise ValueError('queued reset finalization changed')
                row['queued_finalization_intent']=intent;row['queued_finalization_result']=result
                row['queued_finalization_counts']=accumulated_counts;claim._coordinator._save_journal(journal)
        await _queued_owned_thread(prepare)
        return await self._finish_queued_reset_finalization(lease,claim=claim,intent=intent,result=result,counts=accumulated_counts)

    async def _finish_queued_reset_finalization(self, lease, *, claim, intent, result, counts):
        from services.memory.skill_lifecycle import locked
        async with self._owner_lock(lease.owner):
            with self._owner_map_guards([lease.owner]):
                state=self._states.get(lease.owner)
                if (self._queued_retirement_leases.get(lease.worker) is not lease or lease.owner not in self._owner_lifecycle_blocked
                    or state is None or state.active is not lease.worker or self._workers.get(lease.owner) is not lease.worker
                    or getattr(lease.worker,'_proc',None) not in (None,lease.process)
                    or type(lease.waited_exit_code) is not int or lease.process.returncode!=lease.waited_exit_code
                    or lease.checkpoint_sha256!=intent.get('checkpointSHA256')
                    or lease.barrier._owner_revisions.get(lease.owner,0)!=lease.witness.revision or lease.witness.caller.released):
                    raise ValueError('queued reset finalization unavailable')
                with locked(os.fspath(claim._coordinator.lock_target)):
                    journal=claim._coordinator._load_journal();row=journal.get(claim.operation_id)
                    current_claim=row.get('commit_claim') if type(row) is dict else None
                    checkpoint=row.get('queued_holder_retirement') if type(row) is dict else None
                    if (type(row) is not dict or row.get('owner')!=lease.owner or row.get('account_id')!=lease.account_id
                        or row.get('state')!='committing' or row.get('guarded_reset') is not True or row.get('result') is not None
                        or type(current_claim) is not dict or current_claim.get('coordinator_id')!=claim._coordinator._commit_claim_id
                        or current_claim.get('host')!=socket.gethostname() or current_claim.get('pid')!=os.getpid()
                        or row.get('queued_finalization_intent')!=intent or row.get('queued_finalization_result')!=result
                        or row.get('queued_finalization_counts')!=counts
                        or type(checkpoint) is not dict or hashlib.sha256(json.dumps(checkpoint,sort_keys=True,separators=(',',':')).encode()).hexdigest()!=lease.checkpoint_sha256
                        or hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()!=intent.get('resultSHA256')):
                        raise ValueError('queued reset finalization changed')
                    durable=self._durable_owner_epoch(lease.owner)
                    if lease.finalization_epoch is None:
                        local=self._owner_lifecycle_epochs.get(lease.owner,0)
                        if durable==lease.target.durable_epoch and local==lease.local_epoch:
                            self._advance_durable_owner_epoch(lease.owner,intent['nextEpoch'])
                        elif durable!=str(intent['nextEpoch']) or local not in (lease.local_epoch,intent['nextEpoch']):
                            raise ValueError('queued reset epoch changed')
                        self._owner_lifecycle_epochs[lease.owner]=intent['nextEpoch']
                        lease.finalization_epoch=intent['nextEpoch']
                    elif (lease.finalization_epoch!=intent['nextEpoch'] or durable!=str(intent['nextEpoch'])
                          or self._owner_lifecycle_epochs.get(lease.owner)!=intent['nextEpoch']):
                        raise ValueError('queued reset epoch changed')
                    row['state']='complete';row.pop('commit_claim',None)
                    row['attempt_count']=result['receipt']['attempt'];row['accumulated_counts']=counts
                    row['result']=result;row['updated_at']=claim._coordinator.clock()
                    claim._coordinator._save_journal(journal)
                    # Keep the lease and exclusion until the cleanup marker is
                    # durable, even though the stopped worker entry is removed.
                    self._workers.pop(lease.owner,None);state.active=None;state.in_flight.pop(lease.worker,None)
                    state.drain_events.pop(lease.worker,None);state.status='stopped'
                    row['queued_finalization_closed']=True
                    claim._coordinator._save_journal(journal)
                    # No await/yield between durable result and exact cleanup.
                    self._queued_retirement_leases.pop(lease.worker,None)
                    self._owner_lifecycle_blocked.discard(lease.owner)
        return result

    async def resume_queued_reset_finalization(self, *, claim, inventory, record):
        """Resume only a same-process finalization intent, without FM replay."""
        from services.memory.nuke_coordinator import MemoryResetClaim
        from src.openclank.conversation_archive import QueuedProducerInventory
        if type(claim) is not MemoryResetClaim or (inventory is not None and type(inventory) is not QueuedProducerInventory) or type(record) is not dict:
            raise ValueError('queued reset finalization unavailable')
        matches=[lease for lease in tuple(self._queued_retirement_leases.values())
            if type(lease) is _QueuedResetLease and lease.owner==claim.owner and lease.account_id==claim.account_id
            and lease.claim._coordinator is claim._coordinator and lease.claim.operation_id==claim.operation_id]
        if len(matches)!=1: raise ValueError('queued reset finalization unavailable')
        lease=matches[0];intent=record.get('queued_finalization_intent');result=record.get('queued_finalization_result');counts=record.get('queued_finalization_counts')
        frozen=record.get('reset_native_frozen') if inventory is None else None
        if frozen is not None:
            await _queued_owned_thread(lease.provider._assert_reset_retirement_ready,lease.reset_transport_witness)
        if inventory is None and (type(frozen) is not dict or getattr(lease,'reset_native_frozen',None)!=frozen
            or type(getattr(lease,'reset_native_result',None)) is not dict
            or record.get('reset_native_result')!=getattr(lease,'reset_native_result',None)
            or type(record.get('reset_source_manifest')) is not dict
            or record['reset_source_manifest'].get('queuedSHA256')!=lease.inventory.payload_sha256
            or record['reset_source_manifest'].get('previewSHA256')!=lease.preview_fingerprint
            or record.get('reset_source_manifest_sha256')!=getattr(lease,'reset_manifest_sha256',None)
            or hashlib.sha256(json.dumps(record['reset_source_manifest'],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()!=record.get('reset_source_manifest_sha256')):
            raise ValueError('queued reset finalization changed')
        if (type(intent) is not dict or set(intent)!={'version','checkpointSHA256','resultSHA256','oldEpoch','nextEpoch'}
            or type(intent.get('version')) is not int or intent.get('version')!=1
            or type(intent.get('oldEpoch')) is not int or type(intent.get('nextEpoch')) is not int
            or intent.get('checkpointSHA256')!=lease.checkpoint_sha256
            or intent.get('oldEpoch')!=lease.epoch or intent.get('nextEpoch')!=lease.epoch+1
            or type(result) is not dict or result.get('complete') is not True or result.get('operation_id')!=claim.operation_id
            or type(result.get('receipt')) is not dict or type(result['receipt'].get('attempt')) is not int or result['receipt']['attempt']<1
            or type(counts) is not dict
            or hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()!=intent.get('resultSHA256')
            or (inventory is not None and (inventory.claim is not claim or inventory.provider is not lease.provider or inventory.archive is not lease.inventory.archive
                or inventory.expected_json!=lease.inventory.expected_json or inventory.payload_json!=lease.inventory.payload_json
                or inventory.payload_sha256!=lease.inventory.payload_sha256 or inventory.archive_path!=lease.inventory.archive_path))):
            raise ValueError('queued reset finalization changed')
        durable=await _queued_owned_thread(self._durable_owner_epoch,lease.owner)
        if lease.finalization_epoch is None and durable==lease.target.durable_epoch:
            if frozen is None: await self.refresh_stopped_queued_reset_closure(lease.closure,claim=claim,inventory=inventory)
            else: await self.refresh_post_dispatch_queued_reset(claim=claim,frozen=frozen)
        else:
            if lease.rebinding: raise ValueError('queued reset finalization unavailable')
            lease.rebinding=True;old=lease.witness
            try:
                for c in (lease.claim,claim):
                    await _queued_owned_thread(c.assert_current,owner=lease.owner,account_id=lease.account_id,provider=lease.provider,operation_id=claim.operation_id)
                if inventory is not None: await _queued_owned_thread(inventory.assert_current)
                await _queued_owned_thread(lease.profile.assert_current)
                witness=await lease.barrier.reset_drain_witness(lease.owner)
                if not await _queued_owned_thread(lease.barrier.reset_witness_current,witness): raise ValueError('queued reset drain changed')
                checkpoint=record.get('queued_holder_retirement')
                if (self._queued_retirement_leases.get(lease.worker) is not lease
                    or lease.finalization_epoch not in (None,intent['nextEpoch'])
                    or self._durable_owner_epoch(lease.owner)!=str(intent['nextEpoch'])
                    or self._owner_lifecycle_epochs.get(lease.owner,0) not in (lease.local_epoch,intent['nextEpoch'])
                    or lease.process.returncode!=lease.waited_exit_code or type(lease.waited_exit_code) is not int
                    or type(checkpoint) is not dict or hashlib.sha256(json.dumps(checkpoint,sort_keys=True,separators=(',',':')).encode()).hexdigest()!=lease.checkpoint_sha256
                    or await _queued_owned_thread(self._queued_reset_foreground,lease.profile)!=lease.foreground_epoch):
                    raise ValueError('queued reset finalization changed')
                lease.witness=witness
            except BaseException:
                lease.witness=old;raise
            finally:lease.rebinding=False
        return await self._finish_queued_reset_finalization(lease,claim=claim,intent=intent,result=result,counts=counts)

    async def cleanup_completed_queued_reset(self, *, coordinator, owner, account_id, operation_id, record, barrier):
        """Finish a same-process cleanup if result persistence beat cleanup."""
        from services.memory.skill_lifecycle import locked
        from src.openclank.account_request_barrier import _current_admission
        intent=record.get('queued_finalization_intent');result=record.get('result')
        if type(intent) is not dict or type(result) is not dict: raise ValueError('queued reset result unavailable')
        matches=[l for l in tuple(self._queued_retirement_leases.values()) if type(l) is _QueuedResetLease
            and l.owner==owner and l.account_id==account_id and l.claim._coordinator is coordinator and l.claim.operation_id==operation_id]
        if not matches:
            if record.get('queued_finalization_closed') is True:return result
            raise ValueError('queued reset cleanup unavailable')
        if len(matches)!=1:raise ValueError('queued reset cleanup unavailable')
        lease=matches[0]
        await _queued_owned_thread(lease.provider._assert_reset_retirement_ready,lease.reset_transport_witness)
        async with barrier._lock:
            caller=_current_admission.get()
            if caller is None or caller.owner!=owner or caller.released or caller not in barrier._active.get(owner,()):
                raise ValueError('queued reset caller unavailable')
        async with self._owner_lock(owner):
            with self._owner_map_guards([owner]):
                state=self._states.get(owner)
                if (self._queued_retirement_leases.get(lease.worker) is not lease or owner not in self._owner_lifecycle_blocked
                    or lease.barrier is not barrier or lease.finalization_epoch!=intent.get('nextEpoch')
                    or self._durable_owner_epoch(owner)!=str(lease.finalization_epoch)
                    or lease.checkpoint_sha256!=intent.get('checkpointSHA256')
                    or type(lease.waited_exit_code) is not int or lease.process.returncode!=lease.waited_exit_code
                    or state is None or state.active not in (None,lease.worker)
                    or self._workers.get(owner) not in (None,lease.worker)):
                    raise ValueError('queued reset cleanup changed')
                with locked(os.fspath(coordinator.lock_target)):
                    journal=coordinator._load_journal();row=journal.get(operation_id)
                    if (type(row) is not dict or row.get('owner')!=owner or row.get('account_id')!=account_id
                        or row.get('result')!=result or row.get('queued_finalization_intent')!=intent
                        or hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()!=intent.get('resultSHA256')):
                        raise ValueError('queued reset cleanup changed')
                    if state.active is lease.worker:state.active=None
                    if self._workers.get(owner) is lease.worker:self._workers.pop(owner,None)
                    state.in_flight.pop(lease.worker,None);state.drain_events.pop(lease.worker,None);state.status='stopped'
                    row['queued_finalization_closed']=True;coordinator._save_journal(journal)
                    self._queued_retirement_leases.pop(lease.worker,None);self._owner_lifecycle_blocked.discard(owner)
        return result

    @staticmethod
    async def _stop_exact_worker(worker, process) -> bool:
        """Stop this worker and positively wait its captured child; unknown is false."""
        if process is None or getattr(worker, '_proc', None) is not process:
            return False
        bridge = getattr(worker, '_bridge', None)
        holder = getattr(bridge, '_managed_provider_generation', None)
        try:
            await worker.stop()
            if getattr(worker, '_proc', None) not in (None, process):
                return False
            code = await process.wait()
            if type(code) is int and process.returncode == code:
                release = getattr(bridge, '_release_owner_admissions_after_child_exit', None)
                if callable(release):
                    await release(holder)
                return True
        except BaseException:
            if getattr(worker, '_proc', None) is None:
                worker._proc = process
            raise
        if getattr(worker, '_proc', None) is None:
            worker._proc = process
        return False

    async def _retire_when_drained(self, owner: str, worker) -> None:
        async with self._owner_lock(owner):
            state = self._states.get(owner)
            if state is None:
                state = self._owner_state(owner)
                state.in_flight[worker] = 1
                state.status = "stopping"
                return
            event = state.drain_events.setdefault(worker, asyncio.Event())
            if state.in_flight.get(worker, 0) == 0:
                event.set()
        await event.wait()
        lease = None
        closed = False
        process = getattr(worker, '_proc', None)
        stopped = False
        try:
            async with self._owner_lock(owner):
                state = self._states.get(owner)
                bridge = getattr(worker, '_bridge', None)
                holder = getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None)
                generation = getattr(worker, 'installed_generation', None)
                epoch = self._owner_lifecycle_epoch(owner)
                if (state is not None and state.active is not worker and state.in_flight.get(worker, 0) == 0
                    and bridge is not None and isinstance(holder, str) and holder
                    and getattr(bridge, '_managed_provider_generation', None) == holder
                    and type(generation) is int and owner not in self._owner_lifecycle_blocked):
                    lease = _QueuedRetirementLease(owner, worker, bridge, holder, generation, epoch)
                    self._queued_retirement_leases[worker] = lease
            handler = self._queued_retirement_handler
            if lease is not None and callable(handler):
                closed = await handler(lease) is True and self.queued_retirement_worker(owner, lease.bridge) is worker
        finally:
            self._queued_retirement_leases.pop(worker, None)
            # This finalizer can be cancelled by quiescence while that caller
            # owns the async owner lock. Mutate only on this event-loop turn;
            # a replaced/stopping state owns cleanup and must not be awaited here.
            retained = self._states.get(owner)
            same_state = retained is state
            if retained is None:
                retained = self._owner_state(owner)
            retained.in_flight[worker] = max(1, retained.in_flight.get(worker, 0))
            if same_state and retained.status != "stopping":
                stopped = await self._stop_exact_worker(worker, process)
                if (stopped and self._states.get(owner) is retained and retained.status != "stopping"
                    and retained.in_flight.get(worker) == 1):
                    retained.in_flight[worker] = 0
        if closed and lease is not None and stopped:
            # Owner rename/reset can begin while the worker is stopping. Hold
            # the existing owner lifecycle guard for the final epoch check and
            # removal so a stale retirement cannot reclaim under that change.
            with self._owner_map_guards([owner]):
                state = self._states.get(owner)
                if (owner not in self._owner_lifecycle_blocked and self._owner_lifecycle_epoch(owner) == lease.epoch
                    and state is not None and state.active is not worker and state.in_flight.get(worker, 0) == 0
                    and lease.worker is worker and getattr(worker, '_bridge', None) is lease.bridge
                    and getattr(lease.bridge, '_managed_provider_generation', None) == lease.holder
                    and getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) == lease.holder
                    and getattr(worker, 'installed_generation', None) == lease.generation):
                    self._reclaim_generation_dir(owner, worker)

    def queued_reset_lease_for_source(self, owner, bridge, source):
        """Derive the reset lease from its registered holder and exact source."""
        from src.openclank.queued_source import QueuedSourceRef
        if type(source) is not QueuedSourceRef: return None
        raw=json.dumps(['queued.producer-source.v1',source.account_id,source.chat_id,source.native_session_id,
            source.request_id,source.message_id,source.part_id],ensure_ascii=False,separators=(',',':')).encode()
        source_hash=hashlib.sha256(raw).hexdigest()
        for lease in tuple(self._queued_retirement_leases.values()):
            if type(lease) is _QueuedResetLease and lease.owner==self._key(owner) and lease.bridge is bridge:
                if self.queued_retirement_worker(owner,bridge,claim=lease.claim,source_hash=source_hash) is lease.worker:
                    return lease
        return None

    def queued_retirement_worker(self, owner: str, bridge, *, claim=None, source_hash=None):
        """Resolve only a pool-issued, drained holder for queued withdrawal."""
        key = self._key(owner)
        for worker, lease in tuple(self._queued_retirement_leases.items()):
            if lease.owner != key or lease.bridge is not bridge:
                continue
            if type(lease) is _QueuedResetLease:
                if type(source_hash) is not str or re.fullmatch(r'[0-9a-f]{64}',source_hash) is None:
                    return None
                return worker if self.queued_reset_lease_current(lease,claim=claim,source_hash=source_hash) else None
            state = self._states.get(key)
            if (state is None or state.active is worker or state.in_flight.get(worker, 0) != 0
                or key in self._owner_lifecycle_blocked or self._owner_lifecycle_epoch(key) != lease.epoch
                or getattr(worker, '_bridge', None) is not bridge
                or getattr(bridge, '_managed_provider_generation', None) != lease.holder
                or getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) != lease.holder
                or getattr(worker, 'installed_generation', None) != lease.generation
                or not worker.is_alive()):
                return None
            return worker
        return None

    def queued_retirement_evidence(self, owner: str, bridge, *, claim=None, source_hash=None):
        worker = self.queued_retirement_worker(owner, bridge, claim=claim, source_hash=source_hash)
        lease = self._queued_retirement_leases.get(worker) if worker is not None else None
        if lease is None:
            return None
        return (hashlib.sha256(lease.holder.encode('utf-8')).hexdigest(), lease.generation, lease.epoch)

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
        """Retain unverified generations until source closure is established."""
        # A stopped runtime may still contain an enrolled source. Until a
        # read-only legacy inventory or a durable closure proves otherwise,
        # absence of a live process is not permission to remove its directory.
        return

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
        # An uncertain turn retains its original entry and lease. Only the
        # captured child's positive wait can retire that host admission.
        leases = [lease for _,lease in self._host_directory_entries.values()
            if lease.worker is worker and not lease._released and lease._directory_issued
            and not lease._directory_settled and lease._directory_process is not None
            and lease._directory_process.returncode is not None]
        for lease in leases:
            try:
                await lease.release()
            except Exception:
                logger.warning('managed directory admission remains unsettled')

    def worker_for_owner(self, owner: str | None) -> MimoSupervisor | None:
        key = self._key(owner)
        return self._workers.get(key)

    def _default_worker(self) -> MimoSupervisor | None:
        if self._initial_owner in self._workers:
            return self._workers[self._initial_owner]
        return next(iter(self._workers.values()), None)

    def available_models(self, owner: str | None = None) -> list:
        # A catalogue is an owner capability boundary, not a process-wide
        # inventory.  In authenticated mode an absent/unknown owner must not
        # fall back to the initial worker or merge models from other owners.
        key = self._key(owner)
        if not key:
            return []
        worker = self._workers.get(key)
        return worker.available_models() if worker else []

    def provider_apis(self, owner: str | None = None) -> dict[str, str]:
        key = self._key(owner)
        if not key:
            return {}
        worker = self._workers.get(key)
        return worker.provider_apis() if worker else {}

    async def refresh_model_catalog(self, *, owner: str | None = None) -> list:
        lease = await self.admit_provider_control(owner)
        try:
            return await lease.worker.refresh_model_catalog()
        finally:
            await lease.release()

    async def _session_control_worker(
        self,
        session_id: str,
        *,
        owner: str,
    ) -> tuple[MimoSupervisor, AgentWorkerLease | None]:
        """Route every session control through its recipient/owner worker."""
        del session_id
        lease = await self.admit_provider_control(owner)
        try:
            if lease._directory_token is None: lease.bind_unknown_directory()
        except BaseException:
            await lease.release()
            raise
        return lease.worker, lease

    async def prepare_managed_handoff(self, **identity) -> dict:
        owner = str(identity.get("owner") or "").strip()
        session_id = str(identity.get("engine_session_id") or "").strip()
        if not owner or not session_id:
            raise ValueError("managed handoff owner and engine session are required")
        worker, lease = await self._session_control_worker(session_id, owner=owner)
        successful = False
        try:
            if lease is not None:
                self._assert_owner_lifecycle(lease.owner, lease._owner_epoch)
            if identity.get('human_ingress') is not None:
                identity['human_runtime'] = (lease, worker) if lease is not None else None
            result = await worker.prepare_managed_handoff(**identity)
            if lease is not None:
                self._assert_owner_lifecycle(lease.owner, lease._owner_epoch)
            successful = True
            return result
        finally:
            if lease is not None:
                await lease.release(successful_terminal=successful)

    async def clear_managed_handoff(self, **identity) -> bool:
        owner = str(identity.get("owner") or "").strip()
        session_id = str(identity.get("engine_session_id") or "").strip()
        worker = self.worker_for_owner(owner)
        if worker is None or not session_id:
            return False
        return await worker.clear_managed_handoff(**identity)

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

    async def session_terminal_diagnostic_source(self, host_chat_id, native_session_id, message_id, *, owner, timeout=5.0):
        """Lease only the original ready owner worker, without starting a session."""
        key=self._key(owner)
        async with self._owner_lock(key):
            epoch=self._owner_lifecycle_epoch(key); self._assert_owner_lifecycle(key,epoch)
            state=self._states.get(key); worker=state.active if state else None
            if worker is None or not worker.is_alive() or not getattr(worker,'_bridge',None) or host_chat_id not in worker._bridge.mapped_sessions(owner=key):
                raise SupervisorAdmissionError('WORKER_NOT_READY','terminal source unavailable',phase='observation',retryable=True)
            generation=state.generation
            state.in_flight[worker]=state.in_flight.get(worker,0)+1; state.drain_events.setdefault(worker,asyncio.Event()).clear()
        lease=AgentWorkerLease(self,key,worker,owner_epoch=epoch)
        try:
            lease.bind_unknown_directory()
            result=await worker.session_terminal_diagnostic_source(host_chat_id,native_session_id,message_id,owner=key,timeout=timeout)
            self._assert_owner_lifecycle(key,epoch)
            if self._states.get(key) is not state or state.active is not worker or state.generation!=generation or not worker.is_alive(): raise RuntimeError('terminal source changed')
            return result
        finally:
            await lease.release()

    async def session_observation_request(self, host_chat_id: str, target_session_id: str | None,
                                          source: str, limit: int, after: str | None, *, owner: str,
                                          timeout: float = 20.0) -> httpx.Response:
        """Lease only a ready owner generation; never create or reconcile a worker."""
        key = self._key(owner)
        async with self._owner_lock(key):
            epoch = self._owner_lifecycle_epoch(key)
            self._assert_owner_lifecycle(key, epoch)
            state = self._states.get(key)
            worker = state.active if state else None
            if worker is None or not worker.is_alive():
                raise SupervisorAdmissionError("WORKER_NOT_READY", "owner worker is not ready", phase="observation", retryable=True)
            bridge = getattr(worker, "_bridge", None)
            if bridge is None or host_chat_id not in bridge.mapped_sessions(owner=key):
                raise SupervisorAdmissionError("SESSION_NOT_READY", "chat is not admitted", phase="observation", retryable=True)
            state.in_flight[worker] = state.in_flight.get(worker, 0) + 1
            state.drain_events.setdefault(worker, asyncio.Event()).clear()
        lease = AgentWorkerLease(self, key, worker, owner_epoch=epoch)
        try:
            lease.bind_unknown_directory()
            response = await worker.session_observation_request(host_chat_id, target_session_id, source, limit, after, owner=key, timeout=timeout)
            self._assert_owner_lifecycle(key, epoch)
            return response
        finally:
            await lease.release()

    async def session_direct_peer_observation(self, host_chat_id, root, workspace, *, owner, account, workspace_id, mapping_revision, finish, cursor=None, timeout=5.0):
        """One leased ready generation; cursor locates rows but never grants access."""
        import base64
        key=self._key(owner)
        async with self._owner_lock(key):
            epoch=self._owner_lifecycle_epoch(key);self._assert_owner_lifecycle(key,epoch)
            state=self._states.get(key);worker=state.active if state else None
            bridge=getattr(worker,'_bridge',None);process=getattr(worker,'_proc',None)
            holder=getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)
            if (worker is None or state.status!='ready' or not worker.is_alive() or not process or bridge is None
                or bridge._owner!=key or not holder or holder!=getattr(bridge,'_managed_provider_generation',None)
                or getattr(worker,'installed_generation',None)!=state.generation):raise RuntimeError('peer worker unavailable')
            generation=state.generation
            scope=hashlib.sha256(json.dumps(['o04.peer-page.v1',key,account,host_chat_id,root,workspace_id,mapping_revision,epoch,generation,holder],separators=(',',':')).encode()).hexdigest()
            after=None
            if cursor is not None:
                if type(cursor) is not str or len(cursor)>1024:raise ValueError('peer cursor unavailable')
                try: parsed=json.loads(base64.b64decode(cursor+'='*(-len(cursor)%4),altchars=b'-_',validate=True))
                except Exception as exc:raise ValueError('peer cursor unavailable') from exc
                if (type(parsed) is not dict or set(parsed)!={'v','scope','after'} or parsed['v']!=1 or parsed['scope']!=scope
                    or type(parsed['after']) is not str or not re.fullmatch(r'[1-9][0-9]{0,15}',parsed['after'])
                    or int(parsed['after'])>9007199254740991):raise ValueError('peer cursor changed')
                after=parsed['after']
            state.in_flight[worker]=state.in_flight.get(worker,0)+1;state.drain_events.setdefault(worker,asyncio.Event()).clear()
        lease=AgentWorkerLease(self,key,worker,owner_epoch=epoch)
        try:
            lease.bind_unknown_directory()
            async def read_and_finish():
                page=await worker.session_direct_peer_observation(host_chat_id,root,workspace,after,owner=key,timeout=timeout)
                await finish()
                return page
            page=await asyncio.wait_for(read_and_finish(),timeout=timeout)
            self._assert_owner_lifecycle(key,epoch)
            if (self._states.get(key) is not state or state.active is not worker or state.status!='ready' or state.generation!=generation
                or not worker.is_alive() or worker._proc is not process or worker._bridge is not bridge
                or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder
                or getattr(bridge,'_managed_provider_generation',None)!=holder):raise RuntimeError('peer worker changed')
            next_after=page.pop('nextCursor',None)
            if next_after is not None:
                if not re.fullmatch(r'[1-9][0-9]{0,15}',next_after) or int(next_after)>9007199254740991:raise RuntimeError('peer cursor invalid')
                raw=json.dumps({'v':1,'scope':scope,'after':next_after},separators=(',',':')).encode()
                page['nextCursor']=base64.urlsafe_b64encode(raw).decode().rstrip('=')
            output={'generation':generation,'page':page}
        finally:
            await lease.release()
        self._assert_owner_lifecycle(key,epoch)
        if (self._states.get(key) is not state or state.active is not worker or state.status!='ready' or state.generation!=generation
            or not worker.is_alive() or worker._proc is not process or worker._bridge is not bridge
            or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder
            or getattr(bridge,'_managed_provider_generation',None)!=holder):raise RuntimeError('peer worker changed')
        return output

    async def session_workflow_observation(self, host_chat_id, root, workspace, *, owner, account, workspace_id, mapping_revision, finish, cursor=None, timeout=5.0, run_id=None):
        """One leased ready generation; selectors never grant access."""
        import base64
        if run_id is not None and (cursor is not None or not re.fullmatch(r'wf_(?:[0-9A-Za-z-]{26}|[0-9a-f]{64})',run_id)):
            raise ValueError('workflow structure target changed')
        key=self._key(owner)
        async with self._owner_lock(key):
            epoch=self._owner_lifecycle_epoch(key);self._assert_owner_lifecycle(key,epoch)
            state=self._states.get(key);worker=state.active if state else None
            bridge=getattr(worker,'_bridge',None);process=getattr(worker,'_proc',None)
            holder=getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)
            if (worker is None or state.status!='ready' or not worker.is_alive() or not process or bridge is None
                or bridge._owner!=key or not holder or holder!=getattr(bridge,'_managed_provider_generation',None)
                or getattr(worker,'installed_generation',None)!=state.generation):raise RuntimeError('workflow worker unavailable')
            generation=state.generation
            scope=hashlib.sha256(json.dumps(['o05.workflow-page.v1',key,account,host_chat_id,root,workspace_id,mapping_revision,epoch,generation,holder],separators=(',',':')).encode()).hexdigest()
            after=None
            if cursor is not None:
                if type(cursor) is not str or len(cursor)>1024:raise ValueError('workflow cursor unavailable')
                try: parsed=json.loads(base64.b64decode(cursor+'='*(-len(cursor)%4),altchars=b'-_',validate=True))
                except Exception as exc:raise ValueError('workflow cursor unavailable') from exc
                if (type(parsed) is not dict or set(parsed)!={'v','scope','after'} or parsed['v']!=1 or parsed['scope']!=scope
                    or type(parsed['after']) is not str or not re.fullmatch(r'[1-9][0-9]{0,15}',parsed['after'])
                    or int(parsed['after'])>9007199254740991):raise ValueError('workflow cursor changed')
                after=parsed['after']
            state.in_flight[worker]=state.in_flight.get(worker,0)+1;state.drain_events.setdefault(worker,asyncio.Event()).clear()
        lease=AgentWorkerLease(self,key,worker,owner_epoch=epoch)
        try:
            lease.bind_unknown_directory()
            async def read_and_finish():
                extra={'run_id':run_id} if run_id is not None else {}
                page=await worker.session_workflow_observation(host_chat_id,root,workspace,after,owner=key,timeout=timeout,**extra)
                await finish()
                return page
            page=await asyncio.wait_for(read_and_finish(),timeout=timeout)
            self._assert_owner_lifecycle(key,epoch)
            if (self._states.get(key) is not state or state.active is not worker or state.status!='ready' or state.generation!=generation
                or not worker.is_alive() or worker._proc is not process or worker._bridge is not bridge
                or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder
                or getattr(bridge,'_managed_provider_generation',None)!=holder):raise RuntimeError('workflow worker changed')
            next_after=page.pop('nextCursor',None)
            if next_after is not None:
                if not re.fullmatch(r'[1-9][0-9]{0,15}',next_after) or int(next_after)>9007199254740991:raise RuntimeError('workflow cursor invalid')
                raw=json.dumps({'v':1,'scope':scope,'after':next_after},separators=(',',':')).encode()
                page['nextCursor']=base64.urlsafe_b64encode(raw).decode().rstrip('=')
            output={'generation':generation,'page':page}
        finally:
            await lease.release()
        self._assert_owner_lifecycle(key,epoch)
        if (self._states.get(key) is not state or state.active is not worker or state.status!='ready' or state.generation!=generation
            or not worker.is_alive() or worker._proc is not process or worker._bridge is not bridge
            or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder
            or getattr(bridge,'_managed_provider_generation',None)!=holder):raise RuntimeError('workflow worker changed')
        return output

    async def session_goal_observation(self, host_chat_id, native_id, workspace, *, owner, timeout=5.0, finish=None):
        key = self._key(owner)
        async with self._owner_lock(key):
            epoch = self._owner_lifecycle_epoch(key)
            self._assert_owner_lifecycle(key, epoch)
            state = self._states.get(key)
            worker = state.active if state else None
            bridge = getattr(worker, '_bridge', None)
            holder = getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None)
            process = getattr(worker, '_proc', None)
            if (worker is None or state.status != 'ready' or not worker.is_alive() or not process or bridge is None
                    or bridge._owner != key or not holder or holder != getattr(bridge, '_managed_provider_generation', None)
                    or getattr(worker, 'installed_generation', None) != state.generation):
                raise SupervisorAdmissionError('WORKER_NOT_READY', 'owner worker is not ready', phase='observation', retryable=True)
            generation = state.generation
            state.in_flight[worker] = state.in_flight.get(worker, 0) + 1
            state.drain_events.setdefault(worker, asyncio.Event()).clear()
        lease = AgentWorkerLease(self, key, worker, owner_epoch=epoch)
        try:
            lease.bind_unknown_directory()
            result = await worker.session_goal_observation(host_chat_id, native_id, workspace, owner=key, timeout=timeout)
            if finish is not None: await finish()
            self._assert_owner_lifecycle(key, epoch)
            if (self._states.get(key) is not state or state.active is not worker or state.status != 'ready'
                    or state.generation != generation or not worker.is_alive() or worker._proc is not process
                    or worker._bridge is not bridge or getattr(worker,'installed_generation',None)!=generation
                    or getattr(bridge,'_managed_provider_generation',None)!=holder
                    or getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) != holder):
                raise RuntimeError('goal observation holder changed')
        finally:
            await lease.release()
        self._assert_owner_lifecycle(key,epoch)
        if (self._states.get(key) is not state or state.active is not worker or state.status!='ready' or state.generation!=generation
            or not worker.is_alive() or worker._proc is not process or worker._bridge is not bridge
            or getattr(worker,'installed_generation',None)!=generation or getattr(bridge,'_managed_provider_generation',None)!=holder
            or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder):raise RuntimeError('goal observation holder changed')
        return result

    async def session_native_analysis_proof(self, host_chat_id, native_id, workspace, payload, *, owner, timeout=5.0, _candidates=False):
        """Lease the exact ready owner generation for one read-only part proof."""
        key=self._key(owner)
        async with self._owner_lock(key):
            epoch=self._owner_lifecycle_epoch(key); self._assert_owner_lifecycle(key,epoch)
            state=self._states.get(key); worker=state.active if state else None
            bridge=getattr(worker,'_bridge',None); holder=getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)
            process=getattr(worker,'_proc',None)
            if (worker is None or state.status!='ready' or not worker.is_alive() or not process or bridge is None
                or bridge._owner!=key or not holder or holder!=getattr(bridge,'_managed_provider_generation',None)
                or getattr(worker,'installed_generation',None)!=state.generation):
                raise SupervisorAdmissionError('WORKER_NOT_READY','owner worker is not ready',phase='observation',retryable=True)
            generation=state.generation
            state.in_flight[worker]=state.in_flight.get(worker,0)+1
            state.drain_events.setdefault(worker,asyncio.Event()).clear()
        lease=AgentWorkerLease(self,key,worker,owner_epoch=epoch)
        try:
            lease.bind_directory(os.path.realpath(workspace,strict=True))
            reader=worker.session_native_analysis_candidates if _candidates else worker.session_native_analysis_proof
            result=await reader(host_chat_id,native_id,workspace,payload,owner=key,timeout=timeout)
            self._assert_owner_lifecycle(key,epoch)
            if (self._states.get(key) is not state or state.active is not worker or state.status!='ready'
                or state.generation!=generation or not worker.is_alive() or worker._proc is not process
                or worker._bridge is not bridge or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder):
                raise RuntimeError('analysis proof holder changed')
            return result
        finally:
            await lease.release()

    async def session_native_analysis_candidates(self, host_chat_id, native_id, workspace, payload, *, owner, timeout=5.0):
        return await self.session_native_analysis_proof(host_chat_id,native_id,workspace,payload,owner=owner,timeout=timeout,_candidates=True)

    async def session_actor_inspection(self, host_chat_id, target, actor_id, revision, *, owner, message_before=None, receipt_after=None, timeout=5.0):
        key = self._key(owner)
        async with self._owner_lock(key):
            epoch = self._owner_lifecycle_epoch(key)
            self._assert_owner_lifecycle(key, epoch)
            state = self._states.get(key)
            worker = state.active if state else None
            bridge = getattr(worker, '_bridge', None)
            holder = getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None)
            process = getattr(worker, '_proc', None)
            if (worker is None or state.status != 'ready' or not worker.is_alive() or not process or bridge is None
                    or bridge._owner != key or not holder or holder != getattr(bridge, '_managed_provider_generation', None)
                    or getattr(worker, 'installed_generation', None) != state.generation):
                raise SupervisorAdmissionError('WORKER_NOT_READY', 'owner worker is not ready', phase='observation', retryable=True)
            generation = state.generation
            state.in_flight[worker] = state.in_flight.get(worker, 0) + 1
            state.drain_events.setdefault(worker, asyncio.Event()).clear()
        lease = AgentWorkerLease(self, key, worker, owner_epoch=epoch)
        try:
            lease.bind_unknown_directory()
            result = await worker.session_actor_inspection(host_chat_id, target, actor_id, revision, owner=key,
                message_before=message_before, receipt_after=receipt_after, timeout=timeout)
            self._assert_owner_lifecycle(key, epoch)
            if (self._states.get(key) is not state or state.active is not worker or state.status != 'ready'
                    or state.generation != generation or not worker.is_alive() or worker._proc is not process
                    or worker._bridge is not bridge or getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) != holder):
                raise RuntimeError('inspection holder changed')
            return result
        finally:
            await lease.release()

    async def admit_git_index_operation(self, *, owner: str):
        """Retain the exact ready owner worker for one private Git operation.

        The caller owns the returned lease until the original operation has
        settled or its captured child has positively exited. This is holder
        lifetime only; it does not authorize any Files or Git operation.
        """
        self._assert_host_directory_admission(None)
        key = self._key(owner)
        async with self._owner_lock(key):
            self._assert_host_directory_admission(None)
            epoch = self._owner_lifecycle_epoch(key)
            self._assert_owner_lifecycle(key, epoch)
            state = self._states.get(key)
            worker = state.active if state else None
            bridge = getattr(worker, '_bridge', None)
            process = getattr(worker, '_proc', None)
            holder = getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None)
            if (state is None or state.status != 'ready' or worker is None or not worker.is_alive()
                    or bridge is None or process is None or bridge._owner != key or not holder
                    or holder != getattr(bridge, '_managed_provider_generation', None)
                    or getattr(worker, 'installed_generation', None) != state.generation):
                raise SupervisorAdmissionError('WORKER_NOT_READY', 'owner worker is not ready', phase='git', retryable=True)
            generation = state.generation
            state.in_flight[worker] = state.in_flight.get(worker, 0) + 1
            state.drain_events.setdefault(worker, asyncio.Event()).clear()
        lease = AgentWorkerLease(self, key, worker, owner_epoch=epoch)
        return lease, state, worker, bridge, process, holder, generation, epoch

    async def session_git_read_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 5.0) -> httpx.Response:
        """Lease the exact ready owner holder for one private Git read."""
        self._assert_host_directory_admission(workspace)
        key = self._key(owner)
        async with self._owner_lock(key):
            epoch = self._owner_lifecycle_epoch(key)
            self._assert_owner_lifecycle(key, epoch)
            state = self._states.get(key)
            worker = state.active if state else None
            bridge = getattr(worker, '_bridge', None)
            holder = getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None)
            if (worker is None or state.status != 'ready' or not worker.is_alive() or bridge is None
                    or bridge._owner != key or not holder or holder != getattr(bridge, '_managed_provider_generation', None)
                    or getattr(worker, 'installed_generation', None) != state.generation):
                raise SupervisorAdmissionError('WORKER_NOT_READY', 'owner worker is not ready', phase='git', retryable=True)
            generation = state.generation
            state.in_flight[worker] = state.in_flight.get(worker, 0) + 1
            state.drain_events.setdefault(worker, asyncio.Event()).clear()
        lease = AgentWorkerLease(self, key, worker, owner_epoch=epoch)
        directory_token = None
        try:
            directory_token = self.try_enter_host_directory(workspace,lease)
            response = await worker.session_git_read_request(host_chat_id, native_id, workspace, payload, owner=key, timeout=timeout)
            self._assert_owner_lifecycle(key, epoch)
            if (self._states.get(key) is not state or state.active is not worker or state.status != 'ready' or state.generation != generation
                    or not worker.is_alive() or getattr(worker, '_bridge', None) is not bridge
                    or getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) != holder):
                raise RuntimeError('managed git holder changed')
            return response
        finally:
            if directory_token is not None: self.release_host_directory(directory_token)
            await lease.release()

    async def session_actor_display_request(self, host_chat_id, native_id, workspace, actor_id, payload, *, owner, timeout=5.0):
        """Lease one ready holder for its existing no-retry actor display call."""
        key = self._key(owner)
        async with self._owner_lock(key):
            epoch = self._owner_lifecycle_epoch(key)
            self._assert_owner_lifecycle(key,epoch)
            state = self._states.get(key); worker = state.active if state else None
            bridge = getattr(worker,'_bridge',None); process = getattr(worker,'_proc',None)
            holder = getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)
            if (state is None or state.status != 'ready' or worker is None or not worker.is_alive()
                    or bridge is None or process is None or bridge._owner != key or not holder
                    or holder != getattr(bridge,'_managed_provider_generation',None)
                    or getattr(worker,'installed_generation',None) != state.generation
                    or bridge.mapped_session_workspace(host_chat_id) != workspace):
                raise RuntimeError('actor display worker unavailable')
            generation = state.generation
            state.in_flight[worker] = state.in_flight.get(worker,0) + 1
            state.drain_events.setdefault(worker,asyncio.Event()).clear()
        lease = AgentWorkerLease(self,key,worker,owner_epoch=epoch)
        try:
            lease.bind_directory(os.path.realpath(workspace,strict=True))
            response = await worker.session_actor_display_request(host_chat_id,native_id,workspace,actor_id,payload,owner=key,timeout=timeout)
            self._assert_owner_lifecycle(key,epoch)
            if (self._states.get(key) is not state or state.active is not worker or state.status != 'ready'
                    or state.generation != generation or not worker.is_alive() or worker._proc is not process
                    or worker._bridge is not bridge or getattr(worker,'installed_generation',None) != generation
                    or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) != holder
                    or getattr(bridge,'_managed_provider_generation',None) != holder
                    or bridge.mapped_session_workspace(host_chat_id) != workspace):
                raise RuntimeError('actor display worker changed')
            return response
        finally:
            await lease.release()

    async def acquire_rating_operation(self, *, owner: str):
        """Retain the exact ready holder through one guarded rating operation."""
        self._assert_host_directory_admission(None)
        key = self._key(owner)
        async with self._owner_lock(key):
            self._assert_host_directory_admission(None)
            epoch = self._owner_lifecycle_epoch(key)
            self._assert_owner_lifecycle(key, epoch)
            state = self._states.get(key); worker = state.active if state else None
            bridge = getattr(worker, '_bridge', None)
            holder = getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None)
            if (worker is None or state.status != 'ready' or not worker.is_alive() or bridge is None
                    or bridge._owner != key or not holder or holder != getattr(bridge, '_managed_provider_generation', None)
                    or getattr(worker, 'installed_generation', None) != state.generation):
                raise SupervisorAdmissionError('WORKER_NOT_READY', 'owner worker is not ready', phase='rating', retryable=True)
            generation = state.generation
            state.in_flight[worker] = state.in_flight.get(worker, 0) + 1
            state.drain_events.setdefault(worker, asyncio.Event()).clear()
        lease = AgentWorkerLease(self, key, worker, owner_epoch=epoch)
        try:
            lease.bind_unknown_directory()
        except BaseException:
            await lease.release()
            raise
        return lease, state, bridge, holder, generation

    async def session_rating_source_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 5.0) -> httpx.Response:
        """Lease the exact ready owner holder for one private rating-source read."""
        key = self._key(owner)
        async with self._owner_lock(key):
            epoch = self._owner_lifecycle_epoch(key)
            self._assert_owner_lifecycle(key, epoch)
            state = self._states.get(key)
            worker = state.active if state else None
            bridge = getattr(worker, '_bridge', None)
            holder = getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None)
            if (worker is None or state.status != 'ready' or not worker.is_alive() or bridge is None
                    or bridge._owner != key or not holder or holder != getattr(bridge, '_managed_provider_generation', None)
                    or getattr(worker, 'installed_generation', None) != state.generation):
                raise SupervisorAdmissionError('WORKER_NOT_READY', 'owner worker is not ready', phase='rating', retryable=True)
            generation = state.generation
            state.in_flight[worker] = state.in_flight.get(worker, 0) + 1
            state.drain_events.setdefault(worker, asyncio.Event()).clear()
        lease = AgentWorkerLease(self, key, worker, owner_epoch=epoch)
        try:
            lease.bind_directory(os.path.realpath(workspace,strict=True))
            response = await worker.session_rating_source_request(host_chat_id, native_id, workspace, payload, owner=key, timeout=timeout)
            self._assert_owner_lifecycle(key, epoch)
            if (self._states.get(key) is not state or state.active is not worker or state.status != 'ready' or state.generation != generation
                    or not worker.is_alive() or getattr(worker, '_bridge', None) is not bridge
                    or getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) != holder):
                raise RuntimeError('managed rating holder changed')
            return response
        finally:
            await lease.release()

    async def session_context_selection_request(self, host_chat_id: str, native_id: str, workspace: str, payload: dict, *, owner: str, timeout: float = 20.0) -> httpx.Response:
        """Lease a ready owner generation without creating a provider or session."""
        key = self._key(owner)
        async with self._owner_lock(key):
            epoch = self._owner_lifecycle_epoch(key)
            self._assert_owner_lifecycle(key, epoch)
            state = self._states.get(key)
            worker = state.active if state else None
            if worker is None or not worker.is_alive():
                raise SupervisorAdmissionError("WORKER_NOT_READY", "owner worker is not ready", phase="context", retryable=True)
            bridge = getattr(worker, "_bridge", None)
            if bridge is None or bridge._owner != key:
                raise SupervisorAdmissionError("SESSION_NOT_READY", "chat is not admitted", phase="context", retryable=True)
            state.in_flight[worker] = state.in_flight.get(worker, 0) + 1
            state.drain_events.setdefault(worker, asyncio.Event()).clear()
        lease = AgentWorkerLease(self, key, worker, owner_epoch=epoch)
        try:
            lease.bind_directory(os.path.realpath(workspace,strict=True))
            response = await worker.session_context_selection_request(host_chat_id, native_id, workspace, payload, owner=key, timeout=timeout)
            self._assert_owner_lifecycle(key, epoch)
            return response
        finally:
            await lease.release()

    async def session_child_http_request(
        self,
        root_session_id: str,
        child_native_session_id: str,
        suffix: str,
        *,
        owner: str,
        timeout: float = 20.0,
    ) -> httpx.Response:
        worker, lease = await self._session_control_worker(root_session_id, owner=owner)
        successful = False
        try:
            if lease is not None:
                self._assert_owner_lifecycle(lease.owner, lease._owner_epoch)
            response = await worker.session_child_http_request(
                root_session_id, child_native_session_id, suffix, owner=owner, timeout=timeout,
            )
            if lease is not None:
                self._assert_owner_lifecycle(lease.owner, lease._owner_epoch)
            successful = True
            return response
        finally:
            if lease is not None:
                await lease.release(successful_terminal=successful)

    async def session_child_input_request(
        self,
        root_session_id: str,
        child_native_session_id: str,
        method: str,
        suffix: str,
        payload: dict | None,
        *,
        owner: str,
        timeout: float = 20.0,
    ) -> httpx.Response:
        worker, lease = await self._session_control_worker(root_session_id, owner=owner)
        successful = False
        try:
            if lease is not None:
                self._assert_owner_lifecycle(lease.owner, lease._owner_epoch)
            response = await worker.session_child_input_request(
                root_session_id, child_native_session_id, method, suffix, payload, owner=owner, timeout=timeout,
            )
            if lease is not None:
                self._assert_owner_lifecycle(lease.owner, lease._owner_epoch)
            successful = True
            return response
        finally:
            if lease is not None:
                await lease.release(successful_terminal=successful)

    async def workflow_availability_request(
        self,
        root_session_id: str,
        target_native_session_id: str,
        *,
        owner: str,
        timeout: float = 20.0,
    ) -> httpx.Response:
        worker, lease = await self._session_control_worker(root_session_id, owner=owner)
        successful = False
        try:
            if lease is not None:
                self._assert_owner_lifecycle(lease.owner, lease._owner_epoch)
            response = await worker.workflow_availability_request(
                root_session_id, target_native_session_id, owner=owner, timeout=timeout,
            )
            if lease is not None:
                self._assert_owner_lifecycle(lease.owner, lease._owner_epoch)
            successful = True
            return response
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
        before_issue=None,
    ) -> httpx.Response:
        worker, lease = await self._session_control_worker(
            session_id,
            owner=owner,
        )
        successful = False
        try:
            if lease is not None: self._assert_owner_lifecycle(lease.owner, lease._owner_epoch)
            response = await worker.session_http_request(
                session_id,
                method,
                suffix,
                owner=owner,
                payload=payload,
                timeout=timeout,
                **({'before_issue':before_issue} if before_issue is not None else {}),
            )
            if lease is not None: self._assert_owner_lifecycle(lease.owner, lease._owner_epoch)
            successful = True
            return response
        finally:
            if lease is not None:
                await lease.release(successful_terminal=successful)

    async def _resume_exact_history_source(self, worker, lease, session_id: str, owner: str):
        """Resume only the currently authorized original Native history source."""
        from core.models import get_session_manager_instance
        from src.openclank.transcript_projection import get_managed_binding
        from src.openclank.session_file_history import canonical_session_current
        from src.openclank.workspace_policy_service import resolve_owned_workspace
        manager = get_session_manager_instance()
        lifecycle = getattr(manager, 'lifecycle_context', None) if manager else None
        auth = getattr(lifecycle, 'auth_manager', None)
        repository = getattr(lifecycle, 'files_policy_repository', None)
        bridge = getattr(worker, '_bridge', None)
        try:
            session = manager.get_session(session_id, allow_pending_history=True)
            binding = get_managed_binding(session_id, owner=owner)
            workspace_id = binding.get('authorityWorkspaceID')
            native_id = binding.get('engineSessionID')
            directory = binding.get('physicalCwd')
            account = auth.account_id(owner) if auth else None
            if (bridge is None or repository is None or not account or auth.username_for_account_id(account) != owner
                    or session.owner != owner or not isinstance(workspace_id, str) or not workspace_id
                    or getattr(session, 'workspace_id', None) != workspace_id
                    or not isinstance(native_id, str) or not native_id
                    or not isinstance(directory, str) or not directory):
                raise ValueError('history binding unavailable')
            files_generation = repository.generation()
            selected = resolve_owned_workspace(repository, workspace_id=workspace_id, owner_username=owner,
                auth_manager=auth, purpose='agent_workspace', observation_only=True)
            process = worker._proc; generation = worker.installed_generation
            def check_current():
                self._assert_owner_lifecycle(lease.owner, lease._owner_epoch)
                if (get_session_manager_instance() is not manager or getattr(manager, 'lifecycle_context', None) is not lifecycle
                        or getattr(lifecycle, 'auth_manager', None) is not auth
                        or getattr(lifecycle, 'files_policy_repository', None) is not repository
                        or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                        or not worker.is_alive() or worker._proc is not process or worker._bridge is not bridge
                        or worker.installed_generation != generation
                        or get_managed_binding(session_id, owner=owner) != binding
                        or not canonical_session_current(manager, sid=session_id, owner=owner, workspace_id=workspace_id)
                        or repository.generation() != files_generation
                        or resolve_owned_workspace(repository, workspace_id=workspace_id, owner_username=owner,
                            auth_manager=auth, purpose='agent_workspace', observation_only=True) != selected
                        or selected.path != directory or bridge.mapped_sessions(owner=owner).get(session_id) != native_id
                        or bridge.mapped_session_workspace(session_id) != directory):
                    raise ValueError('history binding changed')
            check_current()
            if lease._directory_token is None: lease.bind_directory(directory)
            # This existing direct resume validates the exact durable map and
            # binding, and has no fresh-session or remapping fallback.
            await bridge.resume_session(session_id, native_id, with_memory=bool(binding['memoryEnabled']),
                copal_workspace=str(binding['copalWorkspace']), authority_workspace_id=workspace_id)
            check_current()
            return check_current
        except Exception as exc:
            raise SupervisorAdmissionError('SESSION_NOT_READY', 'original native history source is unavailable',
                phase='history', retryable=True) from exc

    async def session_history_operation(self, session_id: str, *, owner: str, operation: str, payload: dict, file_authority=None) -> dict:
        """Pin native ownership and retain interrupted mutation evidence."""
        from src.openclank.session_operations import execute, _read, reconcile_file_only
        from src.openclank.transcript_projection import get_mimo_state
        state = get_mimo_state(session_id, owner=owner)
        def pending_receipt(value):
            operation_state = value.get("history_operation") or {}
            files = operation_state.get("file_effects") if isinstance(operation_state, dict) else None
            members = files.get("members", []) if isinstance(files, dict) else []
            return (operation_state.get("status") == "pending" or any(
                isinstance(member, dict) and member.get("state") in {"issuing", "pending", "unknown"} for member in members))
        pending = pending_receipt(state)
        key = (self._key(owner), session_id)
        operation_state = state.get("history_operation") or {}
        if operation == "history-reconcile" and operation_state.get("status") != "pending" and pending:
            # File receipts belong to the current human/History owner; a dead
            # native engine must not hide an already issued original ID.
            result = await reconcile_file_only(sid=session_id, owner=owner, payload=payload, file_authority=file_authority)
            latest, _, _ = _read(session_id, owner)
            if not pending_receipt(latest):
                retained = self._history_leases.pop(key, None)
                if retained is not None: await retained.release()
            return result
        lease = self._history_leases.get(key)
        if pending:
            # A retained lease keeps the exact mutation generation alive while
            # normal preference candidates advance. No restart/retry machinery
            # is involved. After a process crash unavailable evidence stays fenced.
            if lease is None or not lease.worker.is_alive():
                raise SupervisorAdmissionError("SUPERVISOR_UNAVAILABLE", "pending history mutation requires its native owner generation", phase="history", retryable=True)
        else:
            lease = await self.admit_provider_control(owner)
        worker = lease.worker
        try:
            self._assert_owner_lifecycle(lease.owner, lease._owner_epoch)
            bridge = worker._bridge
            if not pending:
                source_current = await self._resume_exact_history_source(worker, lease, session_id, owner)
            elif lease._directory_token is None:
                lease.bind_unknown_directory()
            process = worker._proc; generation = worker.installed_generation
            holder = getattr(bridge, '_managed_provider_generation', None)
            original_native = bridge.mapped_session_id(session_id) if bridge is not None else None
            original_directory = bridge.mapped_session_workspace(session_id) if bridge is not None else None
            if file_authority is not None and (not holder or not original_native or not original_directory):
                raise SupervisorAdmissionError("SUPERVISOR_UNAVAILABLE", "file history owner is unavailable", phase="history", retryable=True)
            if process is None or bridge is None or not worker.is_alive():
                raise SupervisorAdmissionError("SUPERVISOR_UNAVAILABLE", "history owner is unavailable", phase="history", retryable=True)
            async with bridge._session_admission_lock(session_id, lease.owner):
                async with bridge._durable_session_map.async_chat_admission_lock(session_id):
                    def check_current():
                        self._assert_owner_lifecycle(lease.owner, lease._owner_epoch)
                        if not pending:
                            try: source_current()
                            except Exception as exc:
                                raise SupervisorAdmissionError('SESSION_NOT_READY', 'original history owner changed', phase='history', retryable=True) from exc
                        if (not worker.is_alive() or worker._proc is not process or worker._bridge is not bridge
                                or worker.installed_generation != generation
                                or (file_authority is not None and (getattr(bridge, '_managed_provider_generation', None) != holder
                                    or bridge.mapped_session_id(session_id) != original_native
                                    or bridge.mapped_session_workspace(session_id) != original_directory))):
                            raise SupervisorAdmissionError("SUPERVISOR_UNAVAILABLE", "history owner changed", phase="history", retryable=True)
                    check_current()
                    return await execute(worker, sid=session_id, owner=lease.owner, operation=operation, payload=payload, check_current=check_current, file_authority=file_authority)
        finally:
            try: latest, _, _ = _read(session_id, lease.owner)
            except KeyError: latest = {}
            fenced = pending_receipt(latest)
            current_epoch = self._owner_lifecycle_epoch(lease.owner) == lease._owner_epoch
            if fenced and current_epoch: self._history_leases[key] = lease
            else:
                self._history_leases.pop(key, None)
                await lease.release()

    async def session_fork_operation(self, session_id: str, *, owner: str, operation: str, payload: dict, manager) -> dict:
        from src.openclank.session_fork import execute, ForkOperationError
        from src.openclank.session_operations import _read
        key = (self._key(owner), str(payload.get("child_session_id") or ""))
        if operation == "fork-reconcile":
            lease = self._history_leases.get(key)
            if lease is None or not lease.worker.is_alive():
                raise SupervisorAdmissionError("SUPERVISOR_UNAVAILABLE", "fork reconciliation requires its native owner generation", phase="history", retryable=True)
        else: lease = await self.admit_provider_control(owner)
        candidate_id = key[1] or None
        try:
            self._assert_owner_lifecycle(lease.owner, lease._owner_epoch)
            worker = lease.worker
            from core.models import get_session_manager_instance
            if manager is not get_session_manager_instance():
                raise SupervisorAdmissionError('SESSION_NOT_READY', 'original history manager changed', phase='history', retryable=True)
            source_current = await self._resume_exact_history_source(worker, lease, session_id, owner)
            bridge = worker._bridge
            async with bridge._session_admission_lock(session_id, lease.owner):
                async with bridge._durable_session_map.async_chat_admission_lock(session_id):
                    def check_current():
                        try:
                            source_current()
                        except Exception as exc:
                            raise SupervisorAdmissionError('SESSION_NOT_READY', 'original fork source changed', phase='history', retryable=True) from exc
                    check_current()
                    result = await execute(worker, manager, sid=session_id, owner=lease.owner, operation=operation, payload=payload, check_current=check_current)
                    candidate_id = result.get("id")
                    return result
        except ForkOperationError as exc:
            if exc.retain_lease:
                candidate_id = exc.candidate_id
            raise
        finally:
            pending = False
            if candidate_id:
                try: child_state, _, _ = _read(candidate_id, lease.owner)
                except KeyError: child_state = {}
                pending = (child_state.get("history_operation") or {}).get("status") == "pending"
            retained_key = (self._key(lease.owner), candidate_id)
            retained = self._history_leases.get(retained_key)
            if pending and self._owner_lifecycle_epoch(lease.owner) == lease._owner_epoch:
                if retained is None:
                    self._history_leases[retained_key] = lease
                elif retained is not lease:
                    await lease.release()
            else:
                if retained is lease:
                    self._history_leases.pop(retained_key, None)
                await lease.release()

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
            if lease is not None: self._assert_owner_lifecycle(lease.owner, lease._owner_epoch)
            result = await worker.set_session_config(
                session_id,
                config_id,
                value,
                owner=owner,
                cwd=cwd,
            )
            if lease is not None: self._assert_owner_lifecycle(lease.owner, lease._owner_epoch)
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
        _chat_guard=None,
    ) -> None:
        if owner is None:
            raise RuntimeError(
                "authenticated Open Clank agent session deletion requires an owner"
            )
        owner_key = self._key(owner)
        candidates = [
            worker
            for worker in self._workers.values()
            if self._key(getattr(worker, "_owner", "")) == owner_key
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
        self._assert_host_directory_admission(None)
        async with self._owner_lock(owner_key):
            self._assert_host_directory_admission(None)
            epoch = self._owner_lifecycle_epoch(owner_key)
            self._assert_owner_lifecycle(owner_key,epoch)
            state = self._states.get(owner_key)
            if state is None or state.active is not worker or not worker.is_alive():
                raise RuntimeError('owner Open Clank agent runtime is unavailable')
            state.in_flight[worker] = state.in_flight.get(worker,0) + 1
            state.drain_events.setdefault(worker,asyncio.Event()).clear()
        lease = AgentWorkerLease(self,owner_key,worker,owner_epoch=epoch)
        try:
            lease.bind_unknown_directory()
            if _chat_guard is None:
                await worker.delete_session(odysseus_session, mimo_session_id=mimo_session_id)
            else:
                await worker.delete_session(odysseus_session, mimo_session_id=mimo_session_id, _chat_guard=_chat_guard)
        finally:
            await lease.release()

    def mapped_sessions(self, owner: str | None = None) -> dict[str, str]:
        if owner is None:
            return {}
        owner_key = self._key(owner)
        result: dict[str, str] = {}
        for candidate in self._workers.values():
            if (
                candidate.bridge
                and (
                    self._key(getattr(candidate, "_owner", "")) == owner_key
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
        self._assert_host_directory_admission(None)
        workers = {
            worker
            for state in self._states.values()
            for worker in [state.active, *state.in_flight.keys()]
            if worker is not None
        }
        self._workers.clear()
        for state in self._states.values():
            if state.idle_timer is not None:
                state.idle_timer.cancel()
                state.idle_timer = None
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
        state = self._owner_state(owner)
        if state.idle_timer is not None:
            state.idle_timer.cancel()
            state.idle_timer = None
        for key in tuple(self._history_leases):
            if key[0] == owner: self._history_leases.pop(key, None)
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
        processes = {worker: getattr(worker, '_proc', None) for worker in workers}
        if state is not None:
            state.active = None
            state.status = "stopping"
            for event in state.drain_events.values():
                event.set()
            for worker in workers:
                state.in_flight[worker] = max(1, state.in_flight.get(worker, 0))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if workers:
            ordered_workers = list(workers)
            results = await asyncio.gather(
                *(self._stop_exact_worker(worker, processes[worker]) for worker in ordered_workers),
                return_exceptions=True,
            )
            if state is not None:
                for worker, result in zip(ordered_workers, results):
                    if result is not True:
                        state.in_flight[worker] = max(1, state.in_flight.get(worker, 0))
                    else:
                        state.in_flight.pop(worker, None)
                        state.drain_events.pop(worker, None)
                if state.in_flight:
                    logger.warning("agent cleanup unavailable")
                    raise RuntimeError("agent cleanup unavailable")
        self._states.pop(owner, None)
        return [(worker, processes[worker]) for worker, result in zip(ordered_workers, results) if result is True] if workers else []

    def _begin_owner_lifecycle_locked(self, owner: str) -> None:
        """Fence one owner while its parent lifecycle guard is held."""
        self._assert_host_directory_admission(None)
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
                confirmed = await self._quiesce_owner_locked(key)
            # Do not reacquire the pool owner lock through AgentWorkerLease.release
            # while quiescence holds it. Its worker state was removed above.
            for worker, process in confirmed:
                barrier = getattr(worker, '_utility_barrier', None)
                if barrier is None:
                    continue
                for utility in await barrier.utility_for_worker(key, worker):
                    try:
                        if (not utility.account_current() or utility.process is not process
                            or utility.lease is None or utility.lease.worker is not worker
                            or utility.lease._owner_epoch != utility.epoch
                            or getattr(worker, 'installed_generation', None) != utility.generation
                            or getattr(utility.bridge, '_managed_provider_generation', None) != utility.holder
                            or getattr(worker, '_bridge', None) not in (None, utility.bridge)):
                            raise RuntimeError('utility identity unavailable')
                        # Death settles the native child, not host finalize or
                        # prepared-option cleanup. Join the exact owned task
                        # after dropping the owner lock; never cancel it here.
                        task = utility.task
                        if task is None:
                            raise RuntimeError('utility task unavailable')
                        try:
                            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
                        except asyncio.TimeoutError as exc:
                            raise RuntimeError('utility task still active') from exc
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            pass  # A finished transport error is not itself settlement proof.
                        live = await barrier.is_live(key, utility.admission)
                        if utility.settled and not live:
                            continue
                        if (not task.done() or task.cancelled() or not live or utility.settling
                                or utility.host_cleanup_unknown):
                            raise RuntimeError('utility host cleanup unavailable')
                        utility.settling = True
                        try:
                            if utility.foreground_runtime is not None:
                                if utility.foreground_handle is None:
                                    raise RuntimeError('utility occupancy settlement unavailable')
                                await asyncio.to_thread(utility.foreground_runtime.leave, utility.foreground_handle)
                            utility.lease._released = True
                            await barrier.release(utility.admission)
                            utility.settled = True
                        finally:
                            utility.settling = False
                    except BaseException:
                        # Keep the exact dead child reachable even under repeated
                        # cancellation; this mutation has no intervening await.
                        retained = self._owner_state(key)
                        retained.in_flight[worker] = max(1, retained.in_flight.get(worker, 0))
                        retained.status = 'stopping'
                        if getattr(worker, '_proc', None) is None:
                            worker._proc = process
                        raise

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
        if not key:
            raise RuntimeError("authenticated projection invalidation requires an owner")
        pass
        lifecycle_keys = self._begin_owner_lifecycle(key)
        try:
            await self._quiesce_owner_lifecycle(lifecycle_keys)
        finally:
            for lifecycle_key in lifecycle_keys:
                self._unblock_owner_lifecycle(lifecycle_key)

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

    def _runtime_inventory_at(
        self,
        owner: str,
        root: Path,
        *,
        rewrite_map_owner: str | None = None,
    ) -> dict[str, object]:
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
                relative = path.relative_to(root).as_posix()
                digest = hashlib.sha256()
                if rewrite_map_owner is not None and relative == "session-map.json":
                    mapping = __import__(
                        "src.openclank.session_map", fromlist=["OwnerSessionMap"]
                    ).OwnerSessionMap(path, owner)
                    data = mapping._read_unlocked()
                    try:
                        raw_data = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        raw_data = None
                    if isinstance(raw_data, dict) and "version" not in raw_data:
                        with path.open("rb") as handle:
                            raw_bytes = handle.read()
                        digest.update(raw_bytes)
                        byte_size = len(raw_bytes)
                        entries.append(["file", relative, byte_size, digest.hexdigest()])
                        file_count += 1
                        byte_count += byte_size
                        continue
                    if (
                        isinstance(raw_data, dict)
                        and raw_data.get("version") == 2
                        and isinstance(raw_data.get("chats"), dict)
                    ):
                        data = raw_data
                    for item in data.get("chats", {}).values():
                        item["owner"] = rewrite_map_owner
                    encoded_map = (
                        json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)
                        + "\n"
                    ).encode("utf-8")
                    digest.update(encoded_map)
                    byte_size = len(encoded_map)
                else:
                    with path.open("rb") as handle:
                        while block := handle.read(1024 * 1024):
                            digest.update(block)
                    byte_size = metadata.st_size
                entries.append(["file", relative, byte_size, digest.hexdigest()])
                file_count += 1
                byte_count += byte_size
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
        if not key:
            raise RuntimeError("authenticated Agent lifecycle requires an owner")
        self._validate_owner_lifecycle_roots()
        runtime = (
            self._runtime_inventory_at(key, self._runtime_home(key))
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
        if not old_key or not new_key or old_key == new_key:
            raise RuntimeError("distinct authenticated Agent owners are required")
        for lifecycle_owner in (old_key, new_key):
            self._assert_no_owner_purge(lifecycle_owner)
        source = self.owner_lifecycle_inventory(old_key)
        target = self.owner_lifecycle_inventory(new_key)
        if bool(target["runtime"]["present"]) or int(target["permission_grants"]["count"]):
            raise RuntimeError("target Agent owner already contains durable state")
        source_runtime = source["runtime"]
        if source_runtime["present"] and (self._runtime_home(old_key) / "session-map.json").exists():
            migrated = self._runtime_inventory_at(
                old_key,
                self._runtime_home(old_key),
                rewrite_map_owner=new_key,
            )
            source_runtime["migrated_fingerprint"] = migrated["fingerprint"]
            source_runtime["migrated_bytes"] = migrated["bytes"]
        return {
            "schema_version": 1,
            "source": source,
            "target": target,
            "content_included": False,
        }

    async def preview_owner_deletion(self, owner: str, tombstone: str, subject_id: str, *, original=None):
        """Pin stopped engine files, including any WAL checkpoint on worker exit."""
        if not subject_id or tombstone != f"deleted:{subject_id}" or owner == tombstone:
            raise RuntimeError("Agent deletion subject changed")
        keys = [owner, tombstone]
        with self._owner_map_guards(keys):
            if any(key in self._owner_lifecycle_blocked for key in keys):
                raise RuntimeError("Agent lifecycle already active")
            self._owner_lifecycle_blocked.update(keys)
            try:
                await self._quiesce_owner_lifecycle(keys)
                preview = await self.preview_owner_rename(owner, tombstone)
                if original is not None:
                    source, target = self._validate_agent_manifest(original, owner, tombstone)
                    if (original.get("quiesced_subject") is not None
                        or not source["runtime"]["present"] or not preview["source"]["runtime"]["present"]
                        or target != preview["target"]
                        or source["permission_grants"] != preview["source"]["permission_grants"]
                        or source["permission_grants"].get("subject_id") != subject_id):
                        raise RuntimeError("Agent deletion could not prove an unapplied owner move")
                return {**preview, "quiesced_subject": subject_id}
            finally:
                for key in keys:
                    self._unblock_owner_lifecycle(key)

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
        if (
            source["present"]
            and not target["present"]
            and source.get("fingerprint") == expected.get("migrated_fingerprint")
            and source.get("bytes") == expected.get("migrated_bytes")
        ):
            # Compensation can be interrupted after moving the destination
            # back but before restoring the source owner in the map.
            return "source_migrated", source, target
        if not source["present"] and self._runtime_inventory_matches(target, expected):
            return "target", source, target
        if (
            not source["present"]
            and target["present"]
            and target.get("fingerprint") == expected.get("migrated_fingerprint")
            and target.get("bytes") == expected.get("migrated_bytes")
        ):
            # A completed rename intentionally changes only the deterministic
            # session-map owner projection; the manifest pins its exact hash.
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
        if not old_key or not new_key or old_key == new_key:
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
                    if runtime_position == "source_migrated":
                        raise RuntimeError(
                            "Agent runtime rename is awaiting compensation"
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
                        self._rewrite_session_map_owner(
                            old_key,
                            new_key,
                            path=new_path / "session-map.json",
                        )
                        runtime_state = "applied"
                    elif runtime_position == "target":
                        self._rewrite_session_map_owner(
                            old_key,
                            new_key,
                            path=self._runtime_home(new_key) / "session-map.json",
                        )
                        runtime_state = "already_applied"
                    else:
                        runtime_state = "empty"
                    if runtime_state in {"applied", "already_applied"}:
                        runtime_target = self._runtime_inventory_at(
                            new_key, self._runtime_home(new_key)
                        )
                        expected_fingerprint = expected["runtime"].get(
                            "migrated_fingerprint"
                        ) or expected["runtime"].get("fingerprint")
                        expected_bytes = expected["runtime"].get(
                            "migrated_bytes"
                        ) or expected["runtime"].get("bytes")
                        if (
                            not runtime_target["present"]
                            or (
                                runtime_target["fingerprint"] != expected_fingerprint
                                or runtime_target["bytes"] != expected_bytes
                            )
                        ):
                            raise RuntimeError("Agent runtime owner rename did not converge")
                        runtime_position = "target"
                        runtime_source = self._runtime_inventory_at(
                            old_key, self._runtime_home(old_key)
                        )
                    else:
                        runtime_position = "empty"
                        runtime_source = self._runtime_inventory_at(
                            old_key, self._runtime_home(old_key)
                        )
                        runtime_target = self._runtime_inventory_at(
                            new_key, self._runtime_home(new_key)
                        )
                    if self._initial_owner == old_key:
                        self._initial_owner = new_key
                    if self._host_provider_owner == old_key:
                        self._host_provider_owner = new_key
        finally:
            map_guards.__exit__(None, None, None)
            for key in lifecycle_keys:
                self._unblock_owner_lifecycle(key)
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
        if not old_key or not new_key or old_key == new_key:
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
                        self._rewrite_session_map_owner(
                            new_key,
                            old_key,
                            path=self._runtime_home(old_key) / "session-map.json",
                        )
                        runtime_state = "compensated"
                    elif runtime_position == "source_migrated":
                        self._rewrite_session_map_owner(
                            new_key,
                            old_key,
                            path=self._runtime_home(old_key) / "session-map.json",
                        )
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
                self._unblock_owner_lifecycle(key)
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
        if not key:
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
                self._unblock_owner_lifecycle(lifecycle_key)
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
