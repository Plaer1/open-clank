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
import shutil
import signal
import socket
from datetime import timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx

from src.openclank.acp_client import ACPClient, TransportError
from src.openclank.acp_bridge import ACPBridge, PermissionHandler, register_client_callbacks

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
MIMO_BIN = REPO_ROOT / "bin" / "mimo"

# Odysseus skill root — where SkillsManager stores SKILL.md files.
# mimo's discoverSkills scans cfg.skills.paths for **/SKILL.md.
_ODYSSEUS_ROOT = REPO_ROOT
_ODYSSEUS_SKILLS_DIR = os.getenv(
    "ODYSSEUS_DATA_DIR",
    str(_ODYSSEUS_ROOT / "data"),
) + "/skills"

# Restart backoff
# Strips ANSI color/style sequences from mimo's stderr log lines.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Max size of one newline-delimited ACP JSON message from the child. Tool
# results embed whole file contents, so this must comfortably exceed any
# single read (asyncio's default is 64 KiB — a live worker-killer).
ACP_STDOUT_LIMIT = 32 * 1024 * 1024

_RESTART_DELAY_INITIAL = 1.0
_RESTART_DELAY_MAX = 5.0
_RESTART_DELAY_MULTIPLIER = 2.0
_MAX_RESTART_ATTEMPTS = 10
_RESTART_WINDOW = 60.0  # reset attempt counter after this many seconds of stability

# Model ids that cannot hold a chat conversation — never a small_model pick.
_NON_CHAT_MODEL_RE = re.compile(r"tts|voice|embed|rerank|image|audio|video|ocr|asr", re.IGNORECASE)


def _pick_small_model(providers: dict) -> str | None:
    """First chat-capable model across the injected providers (provider/model).

    Mimo's title/summarize subagents use the configured ``small_model``; left
    unset, its picker grabbed xiaomi's TTS voicedesign model from our injected
    list and every title call 400'd ("system role is not allowed for TTS
    model"). We injected the list, so we pin a sane default. Override with
    ODYSSEUS_SMALL_MODEL (the injected env config is merged last in mimo's
    chain, so a file-level small_model would not win against it).
    """
    override = os.environ.get("ODYSSEUS_SMALL_MODEL")
    if override:
        return override
    for pid, cfg in providers.items():
        models = cfg.get("models") or {}
        ids = list(models) if isinstance(models, (dict, list, tuple)) else []
        for mid in ids:
            if not _NON_CHAT_MODEL_RE.search(str(mid)):
                return f"{pid}/{mid}"
    return None


_OPENCLAW_PROVIDER_ADAPTERS = {
    "openai-completions": "@ai-sdk/openai-compatible",
    "anthropic-messages": "@ai-sdk/anthropic",
}
_OPENCLAW_PROVIDER_ENV = {
    "xiaomi": "XIAOMI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}
_MAX_PROVIDER_KEY_LENGTH = 1024


def _mimo_child_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("MIMOCODE_PROVIDER_AUTH_FD", None)
    for env_name in list(env):
        if any(marker in env_name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            env.pop(env_name, None)
    return env


def _load_openclaw_providers(path: Path | None = None) -> tuple[dict, dict[str, str]]:
    """Translate the operator's Xiaomi/DeepSeek config without copying its keys."""
    if path is None:
        configured = os.environ.get("OPENCLAW_CONFIG_PATH", "")
        if not configured:
            return {}, {}
        source = Path(configured).expanduser()
    else:
        source = path
    if not source.is_file():
        return {}, {}
    try:
        payload = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("unable to load OpenClaw model providers from %s: %s", source, exc)
        return {}, {}

    if not isinstance(payload, dict):
        return {}, {}
    source_models = payload.get("models")
    if not isinstance(source_models, dict):
        return {}, {}
    source_providers = source_models.get("providers")
    if not isinstance(source_providers, dict):
        return {}, {}
    providers: dict[str, dict] = {}
    credentials: dict[str, str] = {}
    for provider_id, env_name in _OPENCLAW_PROVIDER_ENV.items():
        provider = source_providers.get(provider_id)
        if not isinstance(provider, dict):
            continue
        base_url = provider.get("baseUrl")
        adapter = _OPENCLAW_PROVIDER_ADAPTERS.get(provider.get("api"))
        if not isinstance(base_url, str) or not base_url or adapter is None:
            logger.warning("OpenClaw provider %s has no supported API configuration", provider_id)
            continue

        models: dict[str, dict] = {}
        source_model_list = provider.get("models")
        if not isinstance(source_model_list, list):
            continue
        for model in source_model_list:
            if not isinstance(model, dict):
                continue
            model_id = model.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            item: dict = {
                "id": model_id,
                "name": model.get("name") or model_id,
                "provider": {"npm": adapter, "api": base_url},
            }
            if isinstance(model.get("reasoning"), bool):
                item["reasoning"] = model["reasoning"]
            inputs = model.get("input")
            if isinstance(inputs, list):
                inputs = [kind for kind in inputs if kind in {"text", "audio", "image", "video", "pdf"}]
                if inputs:
                    item["modalities"] = {"input": inputs, "output": ["text"]}
                    item["attachment"] = any(kind != "text" for kind in inputs)
            context = model.get("contextWindow")
            output = model.get("maxTokens")
            if isinstance(context, (int, float)) and isinstance(output, (int, float)):
                item["limit"] = {"context": context, "output": output}
            cost = model.get("cost")
            if isinstance(cost, dict):
                item["cost"] = {
                    "input": cost.get("input", 0),
                    "output": cost.get("output", 0),
                    "cache_read": cost.get("cacheRead", 0),
                    "cache_write": cost.get("cacheWrite", 0),
                }
            models[model_id] = item
        if not models:
            continue
        # MiMo Router ships a built-in routing alias that picks the model per
        # request ("auto" mode). It's absent from operator configs because it
        # isn't a real model row; synthesize it so users can turn it on.
        if provider_id == "xiaomi" and not any("auto" in mid for mid in models):
            models["mimo-auto"] = {
                "id": "mimo-auto",
                "name": "MiMo Auto",
                "provider": {"npm": adapter, "api": base_url},
            }

        options = {"baseURL": base_url}
        timeout = provider.get("timeoutSeconds")
        if isinstance(timeout, (int, float)) and timeout > 0:
            options["timeout"] = int(timeout * 1000)
        providers[provider_id] = {
            "name": provider_id.title(),
            "env": [env_name],
            "npm": adapter,
            "api": base_url,
            "options": options,
            "models": models,
            "only_configured_models": True,
        }
        api_key = provider.get("apiKey")
        if isinstance(api_key, str) and 0 < len(api_key) <= _MAX_PROVIDER_KEY_LENGTH:
            credentials[provider_id] = api_key

    return ({"provider": providers} if providers else {}), credentials


ENDPOINT_PROVIDER_PREFIX = "ody-"


def _endpoint_registry_providers(owner: str = "") -> tuple[dict, dict[str, str]]:
    """Project Odysseus's ModelEndpoint registry into mimo providers.

    Every enabled OpenAI-compatible endpoint becomes a provider named
    `ody-<endpoint_id>` (collision-proof against xiaomi/deepseek/native
    mimo providers) with the same model list the Settings UI shows
    (cached + pinned − hidden; no probing at spawn). Keys ride the
    credential dict for the pipe FD, NEVER the config content or env.

    This is why mimo can drive the agent lane for operator models
    (e's ruling 2026-07-17): mimo's native tool engine takes any
    OpenAI-compatible provider, so the homegrown loop only remains for
    models that don't project. supports_tools == False rows are
    skipped — agent mode is function calling.
    """
    if os.environ.get("ODYSSEUS_PROJECT_ENDPOINTS", "").strip().lower() in {"0", "false", "no"}:
        return {}, {}
    try:
        from core.database import SessionLocal, ModelEndpoint
        from src.auth_helpers import owner_filter
        from src.endpoint_resolver import (
            _endpoint_enabled_models,
            normalize_base,
            resolve_endpoint_runtime,
        )
    except Exception as exc:  # pragma: no cover - import cycle guard
        logger.warning("endpoint projection unavailable: %s", exc)
        return {}, {}

    providers: dict[str, dict] = {}
    credentials: dict[str, str] = {}
    db = SessionLocal()
    try:
        query = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        query = owner_filter(query, ModelEndpoint, owner or "")
        rows = query.all()
    except Exception as exc:
        logger.warning("endpoint projection query failed: %s", exc)
        return {}, {}
    finally:
        db.close()

    for ep in rows:
        if getattr(ep, "supports_tools", None) is False:
            continue
        try:
            base, api_key = resolve_endpoint_runtime(ep, owner=owner or None)
        except Exception as exc:
            logger.warning("endpoint %s credential resolution failed: %s", ep.id, exc)
            continue
        base = normalize_base(base or "")
        if not base.startswith(("http://", "https://")):
            continue
        model_ids = _endpoint_enabled_models(ep)
        if not model_ids:
            continue
        provider_id = f"{ENDPOINT_PROVIDER_PREFIX}{ep.id}"
        adapter = "@ai-sdk/openai-compatible"
        providers[provider_id] = {
            "name": ep.name or ep.id,
            "npm": adapter,
            "api": base,
            "options": {"baseURL": base},
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
            raise ValueError("ODYSSEUS_MIMO_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("ODYSSEUS_MIMO_PORT must be between 1 and 65535")
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
        inherit_host_providers: bool = False,
        grant_store=None,
    ) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._client: ACPClient | None = None
        self._bridge: ACPBridge | None = None
        self._health_task: asyncio.Task | None = None
        self._restart_count = 0
        self._last_restart_time = 0.0
        self._stopping = False
        self._owner = owner
        self._permission_handler = permission_handler
        self._memory_provider = memory_provider
        self._safe_dirs = safe_dirs
        self._grant_store = grant_store
        self._runtime_home = runtime_home
        self._partitioned = partitioned
        self._inherit_host_providers = inherit_host_providers
        self._provider_apis: dict[str, str] = {}
        self._mimocode_home: str | None = None
        # The ACP command already owns an HTTP server. Pin its loopback port so
        # Odysseus can expose a narrow provider-auth adapter without launching
        # a second `mimo serve` process. Keep it stable across child restarts.
        self._http_port = _loopback_port(
            None if partitioned else os.environ.get("ODYSSEUS_MIMO_PORT")
        )
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

        # A1.1: inject odysseus skills path into mimo config via env.
        # MIMOCODE_CONFIG_CONTENT is loaded last in mimo's config chain
        # (config.ts L835) and merges on top of everything else.
        # Phase 5: os.environ.copy() inherits all env vars including
        # FM_DB_PATH — this is how the mimo child process converges on
        # the same frankenmemory db as the thesius parent. All spawners
        # (thesius-provider, mimo-capture, bridged-tool) share one db
        # because they all fork from this inherited env.
        env = _mimo_child_environment()
        provider_auth_fd: int | None = None
        # Server directory-containment check (middleware.ts:24-29): when no
        # server password is set, the server requires requested directories to
        # be within its CWD. Change the child's CWD to /home/e so all user
        # workspaces (~/sauce, ~/entities, ~/open-clank) are reachable.
        # Also set MIMOCODE_HOME if THESIUS_AGENT_HOME is configured (Phase 4).
        if os.path.isdir(_ODYSSEUS_SKILLS_DIR) and not self._partitioned:
            skills_config = json.dumps({
                "skills": {"paths": [_ODYSSEUS_SKILLS_DIR]},
                "memory": {"provider": "frankenmemory"},
            })
            env["MIMOCODE_CONFIG_CONTENT"] = skills_config
            # A1.3: also expose the data dir so mimo's usage writer can find _usage.json
            env["ODYSSEUS_DATA_DIR"] = str(Path(_ODYSSEUS_SKILLS_DIR).parent)
            logger.info("injected odysseus skills path: %s", _ODYSSEUS_SKILLS_DIR)
        else:
            logger.warning("odysseus skills dir not found: %s", _ODYSSEUS_SKILLS_DIR)

        # The embedded mimo must NEVER share state with a personal mimocode
        # install: under XDG defaults it reads ~/.config/mimocode (the user's
        # model defaults + provider config) and writes sessions/auth/logs into
        # ~/.local/share/mimocode — both directions of that are wrong. Always
        # set MIMOCODE_HOME (redirects config/data/state/cache wholesale) to
        # Odysseus's own data dir. Precedence: explicit MIMOCODE_HOME env >
        # THESIUS_AGENT_HOME (Phase 4 agent home) > data/mimocode default.
        # Config + auth in that home are hand-managed (e's ruling 2026-07-09:
        # no automatic copying of credential files — boot once, edit config).
        if self._runtime_home is not None:
            self._runtime_home.mkdir(parents=True, exist_ok=True)
            private_home = self._runtime_home / "home"
            private_home.mkdir(parents=True, exist_ok=True)
            env["MIMOCODE_HOME"] = str(self._runtime_home / "mimocode")
            env["HOME"] = str(private_home)
            env["USERPROFILE"] = str(private_home)
            env["ODYSSEUS_DATA_DIR"] = str(self._runtime_home / "odysseus")
            env["MIMOCODE_CONFIG_CONTENT"] = json.dumps({
                "memory": {"provider": "frankenmemory"},
                "skills": {"paths": []},
            })
        elif "MIMOCODE_HOME" not in env:
            _agent_home = os.environ.get("THESIUS_AGENT_HOME")
            if _agent_home:
                env["MIMOCODE_HOME"] = os.path.join(os.path.expanduser(_agent_home), ".mimocode")
            else:
                _data_dir = os.environ.get("ODYSSEUS_DATA_DIR", str(REPO_ROOT / "data"))
                env["MIMOCODE_HOME"] = os.path.join(_data_dir, "mimocode")
        self._mimocode_home = env["MIMOCODE_HOME"]
        self._reconcile_auth_store()
        merged_providers: dict[str, dict] = {}
        merged_credentials: dict[str, str] = {}
        if self._inherit_host_providers:
            provider_config, provider_credentials = _load_openclaw_providers()
            if provider_config:
                merged_providers.update(provider_config["provider"])
                merged_credentials.update(provider_credentials)
        # The operator's own endpoint registry projects unconditionally —
        # this is what lets mimo drive the agent lane for every
        # OpenAI-compatible model Odysseus knows (e's ruling 2026-07-17).
        registry_config, registry_credentials = _endpoint_registry_providers(self._owner)
        if registry_config:
            # Openclaw entries win on id collision; the ody- namespace
            # should make collisions impossible anyway.
            for pid, cfg in registry_config["provider"].items():
                merged_providers.setdefault(pid, cfg)
            for pid, key in registry_credentials.items():
                merged_credentials.setdefault(pid, key)
        if merged_providers:
            config_content = json.loads(env.get("MIMOCODE_CONFIG_CONTENT", "{}"))
            config_content.setdefault("provider", {}).update(merged_providers)
            small_model = _pick_small_model(merged_providers)
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
                        raise RuntimeError("incomplete MiMo provider credential handoff")
                finally:
                    os.close(write_fd)
                env["MIMOCODE_PROVIDER_AUTH_FD"] = str(provider_auth_fd)
            logger.info(
                "injected host model providers for owner %s: %s",
                self._owner,
                ", ".join(sorted(merged_providers)),
            )
        env["MIMOCODE_ENABLE_QUESTION_TOOL"] = "1"
        logger.info("mimo child MIMOCODE_HOME: %s", env["MIMOCODE_HOME"])

        try:
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
                "cwd": "/home/e",
                # Detach from the terminal's process group: Ctrl+C must reach
                # only the server. Otherwise the child dies on the operator's
                # SIGINT before the shutdown event runs, and the health
                # monitor "helpfully" respawns it mid-shutdown, blocking exit.
                # Shutdown is owned by stop(): stdin EOF, then kill.
                "start_new_session": True,
            }
            if provider_auth_fd is not None:
                spawn_options["pass_fds"] = (provider_auth_fd,)
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
        configured_cwd = Path(os.environ.get("OPENTHESIUS_GLOBAL_CWD", str(REPO_ROOT))).expanduser()
        if not configured_cwd.is_dir():
            logger.warning("configured OPENTHESIUS_GLOBAL_CWD is missing: %s; using %s", configured_cwd, REPO_ROOT)
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
        # only in the isolated mimo store; Odysseus never lists it.
        catalog_session = None
        try:
            catalog_session = await self._bridge.open_session()
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
        """Handle child crash: fail in-flight requests, restart with backoff, reconcile."""
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

        # Restart with bounded backoff
        await self._restart_with_backoff()

        if not self._client or not self._bridge:
            logger.error("mimo restart failed — no client available")
            return

        # Reconcile in-flight sessions — DB-driven: query thesius DB for
        # recent ses_-prefixed sessions and resume each one.
        await self._reconcile_sessions()

    async def _restart_with_backoff(self) -> None:
        """Restart the child with bounded exponential backoff."""
        import time
        now = time.time()

        # Reset counter if we've been stable long enough
        if now - self._last_restart_time > _RESTART_WINDOW:
            self._restart_count = 0

        delay = _RESTART_DELAY_INITIAL
        for attempt in range(_MAX_RESTART_ATTEMPTS):
            self._restart_count += 1
            self._last_restart_time = time.time()
            logger.info("mimo restart attempt %d/%d (delay %.1fs)", attempt + 1, _MAX_RESTART_ATTEMPTS, delay)
            await asyncio.sleep(delay)

            try:
                await self._spawn_and_init()
                logger.info("mimo restarted successfully")
                return
            except Exception as e:
                logger.error("mimo restart attempt %d failed: %s", attempt + 1, e)
                delay = min(delay * _RESTART_DELAY_MULTIPLIER, _RESTART_DELAY_MAX)

        logger.error("mimo restart exhausted %d attempts — giving up", _MAX_RESTART_ATTEMPTS)

    async def _reconcile_sessions(self) -> None:
        """Discard interrupted projections; the next turn replays canonical history."""
        await self._purge_stale_projections()

    async def _purge_stale_projections(self) -> None:
        if not self._bridge:
            return
        sessions = self._bridge.mapped_sessions()
        try:
            from src.openclank.transcript_projection import list_projections

            for row in list_projections(owner=self._owner or None):
                sessions.setdefault(
                    row["odysseus_session_id"], row["mimo_session_id"]
                )
        except Exception as exc:
            logger.debug("projection table unavailable during cleanup: %s", exc)
        for session_id, mimo_session_id in sessions.items():
            try:
                await self.delete_session(
                    session_id, mimo_session_id=mimo_session_id
                )
            except Exception as exc:
                logger.warning("failed to purge stale MiMo projection %s: %s", session_id, exc)

    async def _teardown_child(self) -> None:
        """Clean up the child process and associated tasks."""
        if self._bridge:
            try:
                await self._bridge.terminal_manager.close()
            except Exception as exc:
                logger.warning("MiMo terminal cleanup failed: %s", exc)
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
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
            return

        proc = self._proc
        self._proc = None

        if proc.returncode is not None:
            logger.info("mimo child already exited (code %d)", proc.returncode)
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
        try:
            stored, stored_at = _load_stored_auth(self._owner)
        except Exception as exc:
            logger.warning("provider credential store unavailable: %s", exc)
            return
        file_text: str | None = None
        file_at = 0.0
        try:
            if path.is_file():
                file_text = path.read_text(encoding="utf-8")
                json.loads(file_text)
                file_at = path.stat().st_mtime
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("unreadable runtime auth store %s: %s", path, exc)
            file_text = None
        try:
            if file_text and (stored is None or file_at > stored_at):
                if file_text != stored:
                    _store_auth(self._owner, file_text)
                    logger.info("provider credentials mirrored to app.db for owner %r", self._owner)
            elif stored and stored != file_text:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(stored)
                logger.info("provider credentials restored from app.db for owner %r", self._owner)
        except Exception as exc:
            logger.warning("provider credential reconcile failed: %s", exc)

    def sync_auth_to_db(self) -> None:
        """Mirror the runtime's auth store into app.db (after mutations)."""
        path = self._auth_file()
        if path is None or not path.is_file():
            return
        try:
            file_text = path.read_text(encoding="utf-8")
            json.loads(file_text)
            stored, _ = _load_stored_auth(self._owner)
            if file_text != stored:
                _store_auth(self._owner, file_text)
        except Exception as exc:
            logger.warning("provider credential mirror failed: %s", exc)

    @property
    def grant_store(self):
        return self._grant_store

    async def refresh_model_catalog(self, *, owner: str | None = None) -> list:
        """Refresh MiMo's authenticated provider/model catalog in-place."""
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
        """Refresh one canonical session's negotiated MiMo control plane."""
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
        """Acknowledge and persist a typed MiMo session config value."""
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
        """Delete a MiMo-side session and forget any Odysseus remap."""
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
                logger.warning("failed to release MiMo session MCP clients %s: %s", mimo_session, exc)
        async with httpx.AsyncClient(
            base_url=self.http_base_url,
            follow_redirects=False,
            timeout=10.0,
            trust_env=False,
        ) as client:
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


class MimoSupervisorPool:
    """Lazy owner-keyed MiMo runtimes; auth-disabled mode keeps one worker."""

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
    ) -> None:
        self._memory_provider = memory_provider
        self._safe_dirs = safe_dirs
        self._auth_enabled = auth_enabled
        self._initial_owner = self._key(initial_owner) if initial_owner else ""
        self._host_provider_owner = self._key(host_provider_owner) if host_provider_owner else ""
        self._workers: dict[str, MimoSupervisor] = {}
        self._lock = asyncio.Lock()
        from src.constants import DATA_DIR
        from src.openclank.permission_grants import GrantStore

        root = Path(data_dir) if data_dir is not None else Path(DATA_DIR)
        self._owners_root = root / "mimocode" / "owners"
        self._grant_store = grant_store or GrantStore(str(root / "app.db"))

    @staticmethod
    def _key(owner: str | None) -> str:
        return str(owner or "").strip().lower()

    def _runtime_home(self, owner: str) -> Path:
        digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()
        return self._owners_root / digest

    async def start(self) -> None:
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

    async def for_owner(self, owner: str | None) -> MimoSupervisor:
        key = self._key(owner)
        if self._auth_enabled and not key:
            raise RuntimeError("authenticated MiMo execution requires an owner")
        if not self._auth_enabled:
            key = ""
        existing = self._workers.get(key)
        if existing and existing.is_alive():
            return existing
        async with self._lock:
            existing = self._workers.get(key)
            if existing and existing.is_alive():
                return existing
            worker = MimoSupervisor(
                owner=key,
                memory_provider=self._memory_provider,
                safe_dirs=self._safe_dirs,
                runtime_home=self._runtime_home(key) if self._auth_enabled else None,
                partitioned=self._auth_enabled,
                inherit_host_providers=(
                    not self._auth_enabled or key == self._host_provider_owner
                ),
                grant_store=self._grant_store,
            )
            self._workers[key] = worker
            try:
                await worker.start()
            except Exception:
                self._workers.pop(key, None)
                await worker.stop()
                raise
            return worker

    def worker_for_owner(self, owner: str | None) -> MimoSupervisor | None:
        key = self._key(owner) if self._auth_enabled else ""
        return self._workers.get(key)

    def _default_worker(self) -> MimoSupervisor | None:
        if self._initial_owner in self._workers:
            return self._workers[self._initial_owner]
        return next(iter(self._workers.values()), None)

    def available_models(self, owner: str | None = None) -> list:
        worker = self.worker_for_owner(owner) if owner else self._default_worker()
        if worker:
            return worker.available_models()
        merged: dict[str, dict] = {}
        for item in self._workers.values():
            for model in item.available_models():
                model_id = str(model.get("modelId") or "")
                if model_id:
                    merged[model_id] = model
        return list(merged.values())

    def provider_apis(self, owner: str | None = None) -> dict[str, str]:
        worker = self.worker_for_owner(owner) if owner else self._default_worker()
        if worker:
            return worker.provider_apis()
        merged: dict[str, str] = {}
        for item in self._workers.values():
            merged.update(item.provider_apis())
        return merged

    async def refresh_model_catalog(self, *, owner: str | None = None) -> list:
        return await (await self.for_owner(owner)).refresh_model_catalog()

    async def negotiate_session(self, session_id: str, *, owner: str, cwd: str | None = None) -> dict:
        return await (await self.for_owner(owner)).negotiate_session(
            session_id, owner=owner, cwd=cwd
        )

    async def set_session_config(
        self,
        session_id: str,
        config_id: str,
        value: str,
        *,
        owner: str,
        cwd: str | None = None,
    ) -> dict:
        return await (await self.for_owner(owner)).set_session_config(
            session_id, config_id, value, owner=owner, cwd=cwd
        )

    async def delete_session(
        self,
        odysseus_session: str,
        *,
        owner: str | None = None,
        mimo_session_id: str | None = None,
    ) -> None:
        worker = self.worker_for_owner(owner) if owner is not None else None
        if worker is None:
            for candidate in self._workers.values():
                if (
                    mimo_session_id
                    or (
                        candidate.bridge
                        and odysseus_session in candidate.bridge.mapped_sessions()
                    )
                ):
                    worker = candidate
                    break
        if worker is None:
            raise RuntimeError("owner MiMo runtime is unavailable")
        await worker.delete_session(
            odysseus_session, mimo_session_id=mimo_session_id
        )

    def mapped_sessions(self, owner: str | None = None) -> dict[str, str]:
        worker = self.worker_for_owner(owner) if owner is not None else None
        if worker and worker.bridge:
            return worker.bridge.mapped_sessions()
        result: dict[str, str] = {}
        for candidate in self._workers.values():
            if candidate.bridge:
                result.update(candidate.bridge.mapped_sessions())
        return result

    def permission_handler_for(self, owner: str | None):
        worker = self.worker_for_owner(owner)
        return worker.permission_handler if worker else None

    def question_handler_for(self, owner: str | None):
        worker = self.worker_for_owner(owner)
        return worker.question_handler if worker else None

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
            raise RuntimeError("MiMo owner runtime is unavailable")
        return worker.http_base_url

    async def stop(self) -> None:
        workers = list(self._workers.values())
        self._workers.clear()
        await asyncio.gather(*(worker.stop() for worker in workers), return_exceptions=True)

    async def refresh_endpoint_projection(self) -> None:
        """The endpoint registry changed: provider config is spawn-time
        env, so stop the workers — the next turn lazily respawns them
        with a freshly projected config. Best-effort; endpoint edits
        are rare admin actions and a dropped in-flight turn surfaces as
        a clean stream error."""
        async with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        if workers:
            logger.info(
                "endpoint registry changed — recycling %d mimo worker(s) for reprojection",
                len(workers),
            )
        await asyncio.gather(*(worker.stop() for worker in workers), return_exceptions=True)

    async def rename_owner(self, old_owner: str, new_owner: str) -> None:
        old_key = self._key(old_owner)
        new_key = self._key(new_owner)
        worker = self._workers.pop(old_key, None)
        if worker:
            await worker.stop()
        self._grant_store.rename_owner(old_key, new_key)
        old_path = self._runtime_home(old_key)
        new_path = self._runtime_home(new_key)
        if old_path.exists():
            if new_path.exists():
                raise RuntimeError("target MiMo owner partition already exists")
            new_path.parent.mkdir(parents=True, exist_ok=True)
            old_path.rename(new_path)
        if self._initial_owner == old_key:
            self._initial_owner = new_key
        if self._host_provider_owner == old_key:
            self._host_provider_owner = new_key
        try:
            from core.database import SessionLocal, MimoAuthStore
            db = SessionLocal()
            try:
                row = db.query(MimoAuthStore).filter(MimoAuthStore.owner == old_key).first()
                if row is not None:
                    db.query(MimoAuthStore).filter(MimoAuthStore.owner == new_key).delete()
                    row.owner = new_key
                    db.commit()
            finally:
                db.close()
        except Exception as exc:
            logger.warning("provider credential rename failed: %s", exc)

    async def purge_owner(self, owner: str) -> None:
        key = self._key(owner)
        worker = self._workers.pop(key, None)
        if worker:
            await worker.stop()
        self._grant_store.purge_owner(key)
        path = self._runtime_home(key)
        if path.exists():
            shutil.rmtree(path)
        try:
            from core.database import SessionLocal, MimoAuthStore
            db = SessionLocal()
            try:
                db.query(MimoAuthStore).filter(MimoAuthStore.owner == key).delete()
                db.commit()
            finally:
                db.close()
        except Exception as exc:
            logger.warning("provider credential purge failed: %s", exc)
