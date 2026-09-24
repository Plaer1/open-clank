"""ACP bridge — maps mimo session/update notifications to odysseus chat SSE strings."""

import asyncio
import base64
import copy
import binascii
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Coroutine, Dict, List, Optional

from src.memory_scope import chat_workspace, memory_owner
from src.openclank.acp_client import ACPClient, RPCError, TransportError
from src.openclank.chat_routing import MANAGED_ENGINE_PUBLIC_URL
from src.openclank.permission_grants import derive_pattern
from src.openclank.session_map import OwnerSessionMap

logger = logging.getLogger(__name__)

# Debug switch — gate verbose bridge logging when OPENTHESIUS_DEBUG is set.
# Import inline to avoid circular dependency at module load.
def _debug_enabled() -> bool:
    try:
        from src.constants import is_openthesius_debug
        return is_openthesius_debug()
    except Exception:
        return False

# Path to the lifetools MCP server script
_LIFETOOLS_SERVER = Path(__file__).resolve().parent / "lifetools_server.py"

# Path to the fm-mcp binary (frankenmemory)
_FM_MCP_COMMAND = os.environ.get("FM_MCP_COMMAND", "fm-mcp")
_MEMORY_BROKER_URL = ""
_MEMORY_BROKER_TOKEN = ""
_COPAL_BROKER_URL = ""
_COPAL_BROKER_TOKEN = ""


def configure_frankenmemory_broker(url: str, token: str) -> None:
    """Configure the private app-owned memory transport inherited by lifetools.

    The token is held in process memory and projected only into the scoped
    lifetools descriptor; it is never put in the host-wide environment.
    """
    global _MEMORY_BROKER_URL, _MEMORY_BROKER_TOKEN
    _MEMORY_BROKER_URL = str(url or "").strip()
    _MEMORY_BROKER_TOKEN = str(token or "").strip()
    if bool(_MEMORY_BROKER_URL) != bool(_MEMORY_BROKER_TOKEN):
        raise ValueError("memory broker URL and token must be configured together")


def configure_copal_broker(url: str, token: str) -> None:
    """Configure the private app-owned Copal transport inherited by lifetools."""
    global _COPAL_BROKER_URL, _COPAL_BROKER_TOKEN
    _COPAL_BROKER_URL = str(url or "").strip()
    _COPAL_BROKER_TOKEN = str(token or "").strip()
    if bool(_COPAL_BROKER_URL) != bool(_COPAL_BROKER_TOKEN):
        raise ValueError("Copal broker URL and token must be configured together")


def copal_broker_token(master_token: str, owner: str, workspace: str) -> str:
    owner = str(owner or "").strip()
    workspace = str(workspace or "").strip()
    if not master_token or not owner or not workspace:
        raise ValueError("Copal broker scope is incomplete")
    payload = f"open-clank-copal-broker-v1\0{owner}\0{workspace}".encode()
    return hmac.new(master_token.encode(), payload, hashlib.sha256).hexdigest()


def frankenmemory_broker_token(
    master_token: str, owner: str, workspace_id: str
) -> str:
    owner = str(owner or "").strip()
    workspace_id = str(workspace_id or "").strip()
    if not master_token or not owner or not workspace_id:
        raise ValueError("memory broker scope is incomplete")
    payload = f"open-clank-memory-broker-v1\0{owner}\0{workspace_id}".encode()
    return hmac.new(master_token.encode(), payload, hashlib.sha256).hexdigest()


def frankenmemory_child_env(*, command: str | None = None) -> dict[str, str]:
    """Project only Frankenmemory runtime configuration into child processes."""
    from src.constants import FM_DB_PATH

    env = {
        name: value
        for name, value in os.environ.items()
        if value
        and name in {"FM_DB_PATH", "FM_DB_ID"}
    }
    # Children must share the exact store path even when the parent relies on
    # the compiled-in default: broker-mode providers gate direct sqlite v2
    # reads on an explicit FM_DB_PATH, and an implicit default could drift
    # from the broker's store.
    env.setdefault("FM_DB_PATH", str(FM_DB_PATH))
    db_path = env.get("FM_DB_PATH")
    if db_path and not os.path.isabs(db_path):
        raise ValueError("FM_DB_PATH must be absolute")

    resolved_command = (
        command
        if command is not None
        else os.environ.get("FM_MCP_COMMAND") or _FM_MCP_COMMAND
    )
    if resolved_command:
        env["FM_MCP_COMMAND"] = resolved_command
    return env


def lifetools_mcp_descriptor(
    owner: str = "",
    session_id: str = "",
    workspace: str = "",
    authority_workspace_id: str = "",
    memory_workspace_id: str = "",
    memory_enabled: bool = True,
    copal_workspace: str = "default",
    engine_session_aliases: Optional[list[str]] = None,
    binding_revision: int = 0,
    map_revision: int = 0,
    mapping_revision: int = 0,
) -> dict:
    """Build the MCP server descriptor for the life-tools bridge.

    Returns a dict matching the ACP McpServerStdio shape:
    {name, command, args, env:[{name,value}]}
    """
    skill_owner = str(owner or "").strip()
    owner = memory_owner(owner)
    workspace_id = str(memory_workspace_id or chat_workspace()).strip()
    authority_workspace_id = str(authority_workspace_id or "").strip()
    copal_workspace = str(copal_workspace or "default").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", copal_workspace):
        raise ValueError("invalid Copal workspace for lifetools descriptor")
    scope = hashlib.sha256(
        f"{owner}\0{session_id}\0{workspace}\0{authority_workspace_id}\0{copal_workspace}".encode("utf-8")
    ).hexdigest()[:12]
    child_env = {
        "OWNER": owner,
        "OPEN_CLANK_SKILL_OWNER": skill_owner,
        "SESSION_ID": session_id,
        "WORKSPACE": workspace,
        "OPEN_CLANK_AUTHORITY_WORKSPACE_ID": authority_workspace_id,
        "FM_SCOPE_AUTHORITY": "trusted-caller",
        "FM_OWNER": owner,
        "FM_WORKSPACE_ID": workspace_id,
        "COPAL_WORKSPACE": copal_workspace,
        "FM_MEMORY_ENABLED": "1" if memory_enabled else "0",
        "OPEN_CLANK_ENGINE_SESSION_ALIASES": json.dumps(
            list(engine_session_aliases or []), separators=(",", ":")
        ),
        "OPEN_CLANK_SESSION_BINDING_REVISION": str(max(0, int(binding_revision))),
        "OPEN_CLANK_SESSION_MAP_REVISION": str(max(0, int(map_revision))),
        "OPEN_CLANK_SESSION_MAPPING_REVISION": str(max(0, int(mapping_revision))),
        **frankenmemory_child_env(command=_FM_MCP_COMMAND),
    }
    # The managed engine and its lifetools process use an owner-private data
    # directory. Stable Workspace authority, however, lives in the app's
    # canonical policy/auth stores. Project their paths explicitly so the
    # trusted bridge can re-resolve the current immutable owner + Workspace on
    # every tool call instead of trusting the baked physical cwd.
    from src.constants import APP_DB, AUTH_FILE

    child_env.update(
        {
            "OPEN_CLANK_AUTHORITY_DB_PATH": os.path.abspath(APP_DB),
            "OPEN_CLANK_AUTHORITY_AUTH_PATH": os.path.abspath(AUTH_FILE),
        }
    )
    # The lifetools process owns its Copal adapter, so carry the app's
    # partitioned store into that process as well.  Without this, a
    # disposable/partitioned app falls back to the repository default and
    # collides with another running Copal bridge before the first tool call.
    copal_data_dir = os.environ.get("COPAL_DATA_DIR", "").strip()
    if copal_data_dir:
        child_env["COPAL_DATA_DIR"] = copal_data_dir
    if _COPAL_BROKER_URL:
        child_env.update(
            {
                "OPEN_CLANK_COPAL_BROKER_URL": _COPAL_BROKER_URL,
                "OPEN_CLANK_COPAL_BROKER_TOKEN": copal_broker_token(
                    _COPAL_BROKER_TOKEN, owner, copal_workspace
                ),
                "OPEN_CLANK_COPAL_BROKER_WORKSPACE": copal_workspace,
            }
        )
    if _MEMORY_BROKER_URL:
        child_env.update(
            {
                "OPEN_CLANK_MEMORY_BROKER_URL": _MEMORY_BROKER_URL,
                "OPEN_CLANK_MEMORY_BROKER_TOKEN": frankenmemory_broker_token(
                    _MEMORY_BROKER_TOKEN, owner, workspace_id
                ),
            }
        )
    return {
        "name": f"lifetools_{scope}",
        "command": sys.executable,
        "args": [str(_LIFETOOLS_SERVER)],
        "env": [
            {"name": name, "value": value}
            for name, value in child_env.items()
        ],
    }


def frankenmemory_mcp_descriptor(
    workspace: str = "",
    owner: str = "",
    session_id: str = "",
) -> dict:
    """Build the MCP server descriptor for the frankenmemory engine.

    Returns a dict matching the ACP McpServerStdio shape.
    fm-mcp tools surface as frankenmemory:recall, frankenmemory:capture, etc.

    workspace defaults to the canonical chat workspace; pass one only for a
    genuinely workspace-scoped session (never a filesystem path for chat).
    """
    owner = memory_owner(owner)
    workspace = workspace.strip() or chat_workspace()
    child_env = {
        "FM_WORKSPACE_ID": workspace,
        "FM_OWNER": owner,
        "FM_SESSION_ID": session_id,
        # This server is attached only so MiMo can register the scoped internal
        # provider. Its model-visible tools are denied separately, and fm-mcp
        # uses this marker as defense in depth for privileged/internal calls.
        "FM_AGENT_BRIDGE": "1",
        **frankenmemory_child_env(command=_FM_MCP_COMMAND),
    }

    scope = hashlib.sha256(
        f"{owner}\0{session_id}\0{workspace}".encode("utf-8")
    ).hexdigest()[:12]
    return {
        "name": f"frankenmemory_{scope}",
        "command": _FM_MCP_COMMAND,
        "args": [],
        "env": [
            {"name": name, "value": value}
            for name, value in child_env.items()
        ],
    }


def odysseus_mcp_descriptors(*, is_admin: bool) -> tuple[list[dict], list[str]]:
    """Return enabled admin-owned MCP transports without exposing secrets to prompts."""
    if not is_admin:
        return [], []
    try:
        from core.database import McpServer, SessionLocal

        db = SessionLocal()
        try:
            rows = db.query(McpServer).filter(McpServer.is_enabled == True).all()
            descriptors: list[dict] = []
            disabled: list[str] = []
            for row in rows:
                scope = hashlib.sha256(str(row.id).encode("utf-8")).hexdigest()[:10]
                name = f"odysseus_{scope}"
                if row.transport == "stdio" and row.command:
                    env = json.loads(row.env or "{}")
                    args = json.loads(row.args or "[]")
                    if not isinstance(env, dict) or not isinstance(args, list):
                        continue
                    descriptors.append({
                        "name": name,
                        "command": row.command,
                        "args": [str(value) for value in args],
                        "env": [
                            {"name": str(key), "value": str(value)}
                            for key, value in env.items()
                        ],
                    })
                elif row.transport in ("http", "sse") and row.url:
                    descriptors.append({
                        "name": name,
                        "type": row.transport,
                        "url": row.url,
                        "headers": [],
                    })
                for tool_name in json.loads(row.disabled_tools or "[]"):
                    disabled.append(f"{name}_{tool_name}")
            return descriptors, disabled
        finally:
            db.close()
    except Exception as exc:
        logger.warning("failed to compile Open Clank MCP descriptors: %s", exc)
        return [], []


def register_client_callbacks(
    client: ACPClient,
    permission_handler: Optional[Callable[[dict], Coroutine[Any, Any, dict]]] = None,
    terminal_manager: Any = None,
) -> None:
    """Register the ACP client-side callbacks that mimo may call.

    Args:
        client: the ACP client
        permission_handler: optional async handler for session/request_permission.
            If None, uses a fail-safe reject default.
    """

    async def _read_text_file(params: dict) -> dict:
        path = params.get("path", "")
        line = params.get("line")
        limit = params.get("limit")
        try:
            if not (
                permission_handler is not None
                and hasattr(permission_handler, "authorize_path")
                and permission_handler.authorize_path(
                    str(params.get("sessionId") or ""), path
                )
            ):
                raise PermissionError("ACP file read is outside the active workspace")
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            if line is not None:
                lines = text.splitlines(keepends=True)
                start = max(0, int(line) - 1)
                end = start + int(limit) if limit else len(lines)
                text = "".join(lines[start:end])
            elif limit is not None:
                lines = text.splitlines(keepends=True)
                text = "".join(lines[: int(limit)])
            return {"content": text}
        except FileNotFoundError:
            return {"content": ""}
        except Exception as e:
            logger.error("fs/read_text_file error for %s: %s", path, e)
            return {"content": ""}

    async def _write_text_file(params: dict) -> dict:
        path = params.get("path", "")
        content = params.get("content", "")
        try:
            if not (
                permission_handler is not None
                and hasattr(permission_handler, "authorize_path")
                and permission_handler.authorize_path(
                    str(params.get("sessionId") or ""), path
                )
            ):
                raise PermissionError("ACP file write is outside the active workspace")
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return {}
        except Exception as e:
            logger.error("fs/write_text_file error for %s: %s", path, e)
            raise

    async def _request_permission(params: dict) -> dict:
        # C4: delegate to the real handler if provided, else fail-safe reject
        if permission_handler:
            if callable(permission_handler):
                return await permission_handler(params)
            return await permission_handler.handle(params)
        # Fail-safe: reject on disconnect/missing handler
        return {"outcome": {"outcome": "selected", "optionId": "reject"}}

    client.register_callback("fs/read_text_file", _read_text_file)
    client.register_callback("fs/write_text_file", _write_text_file)
    client.register_callback("session/request_permission", _request_permission)
    if terminal_manager is not None:
        client.register_callback("terminal/create", terminal_manager.create)
        client.register_callback("terminal/output", terminal_manager.output)
        client.register_callback("terminal/wait_for_exit", terminal_manager.wait_for_exit)
        client.register_callback("terminal/kill", terminal_manager.kill)
        client.register_callback("terminal/release", terminal_manager.release)


class _TurnState:
    """Accumulates state for a single prompt turn."""

    __slots__ = (
        "full_response",
        "metrics",
        "tool_calls_seen",
        "tool_titles",
        "agent_rounds",
        "stop_reason",
        "turn_start",
        "usage",
        "max_tool_calls",
        "policy_stopped",
        "error_streamed",
    )

    def __init__(self, max_tool_calls: int = 0) -> None:
        self.full_response = ""
        self.metrics: dict = {}
        self.tool_calls_seen: set = set()
        self.tool_titles: dict = {}
        self.agent_rounds = 0
        self.stop_reason: Optional[str] = None
        self.turn_start = time.time()
        self.usage: Optional[dict] = None
        self.max_tool_calls = max(0, int(max_tool_calls or 0))
        self.policy_stopped = False
        self.error_streamed = False


class ACPBridge:
    """Translates ACP session/update notifications into odysseus chat SSE strings.

    One bridge per ACP client. Manages per-turn state and yields the canonical
    OpenClank Agent SSE contract.
    """

    def __init__(
        self,
        client: ACPClient,
        cwd: str,
        owner: str = "",
        permission_handler: Optional[Callable[[dict], Coroutine[Any, Any, dict]]] = None,
        memory_provider: Any = None,
        session_map_path: Optional[Path] = None,
        managed_provider_context: Any = None,
        session_workspace_adapter: Any = None,
        lifecycle_epoch: str | None = None,
    ) -> None:
        self._client = client
        self._cwd = cwd
        self._owner = owner
        self._memory_provider = memory_provider
        self._managed_provider_context = managed_provider_context
        self._session_workspace_adapter = session_workspace_adapter
        self._delete_session_callback = None
        # Per-session turn state (only one active turn per session at a time)
        self._turns: Dict[str, _TurnState] = {}
        # Per-session queues: session/update notifications land here, consumed by the generator
        self._queues: Dict[str, asyncio.Queue] = {}
        # Per-session available models from mimo's handshake (modelId → {modelId, name})
        self._session_models: Dict[str, list] = {}
        # Full negotiated control-plane state, keyed by Open Clank agent session id. The
        # canonical desired selection is persisted on the Open Clank session.
        self._session_state: Dict[str, dict] = {}
        self._session_context: Dict[str, dict] = {}
        self._session_context_locks: Dict[str, asyncio.Lock] = {}
        self._session_admission_locks: Dict[tuple[str, str], asyncio.Lock] = {}
        self._pending_session_results: Dict[str, dict] = {}
        self.question_handler = QuestionHandler()
        from src.openclank.acp_terminal import ACPTerminalManager

        self.terminal_manager = ACPTerminalManager(
            lambda session_id: dict(self._session_context.get(session_id) or {})
        )
        # Latest full catalog from any handshake — mimo is the model
        # authority; /api/models reports this list up to the picker.
        self.available_models: list = []
        # Open Clank-session → mimo-session remap for sessions whose mimo-side
        # state is gone (e.g. created before the MIMOCODE_HOME isolation).
        # Persisted so the remap survives server restarts.
        if session_map_path is not None:
            self._session_map_path = session_map_path
        else:
            runtime_root = (
                Path(
                    os.environ.get("OPEN_CLANK_DATA_DIR")
                    or os.environ.get("ODYSSEUS_DATA_DIR")
                    or str(Path(__file__).resolve().parents[2] / "data")
                ) / "runtime" / "agent-engine"
            )
            owner_key = hashlib.sha256(str(owner or "local").encode("utf-8")).hexdigest()
            self._session_map_path = runtime_root / "owners" / owner_key / "session-map.json"
        self._durable_session_map = OwnerSessionMap(self._session_map_path, self._owner, lifecycle_epoch=lifecycle_epoch)
        self._session_map = self._durable_session_map.flat_current()

        register_client_callbacks(
            client,
            permission_handler=permission_handler,
            terminal_manager=self.terminal_manager,
        )
        client.on_session_update(self._handle_session_update)
        if permission_handler is not None and hasattr(permission_handler, "set_context_resolver"):
            permission_handler.set_context_resolver(
                lambda session_id: dict(self._session_context.get(session_id) or {})
            )
        self.question_handler.set_context_resolver(
            lambda session_id: {
                **dict(self._session_context.get(session_id) or {}),
                "plan_revision": int(
                    ((self._session_state.get(session_id) or {}).get("plan_state") or {}).get("revision")
                    or 0
                ),
            }
        )
        self.question_handler.on_request(self._surface_question)
        self.question_handler.on_resolved(self._clear_question)
        client.register_callback("_odysseus/question", self.question_handler.handle)
        client.register_callback("_openclank/session/v1/cwd/change", self._handle_session_cwd_change)
        client.register_callback("_openclank/session/v1/binding/read", self._handle_session_binding_read)
        if permission_handler is not None and hasattr(permission_handler, "on_request"):
            permission_handler.on_request(self._surface_permission)

    def _bind_engine_session(
        self,
        chat_id: str,
        engine_id: str,
        *,
        expected_current: Optional[str] = None,
        expected_mapping_revision: Optional[int] = None,
    ) -> dict:
        expected_map_revision = self._durable_session_map.map_revision()
        try:
            item = self._durable_session_map.bind(
                chat_id,
                engine_id,
                expected_map_revision=expected_map_revision,
                expected_current=expected_current,
                expected_mapping_revision=expected_mapping_revision,
            )
        except Exception:
            # os.replace() may have succeeded while its directory fsync
            # reported an error. Re-read the authority before treating the
            # admission as lost; this is safe because the expected-current
            # CAS remains the winner test.
            current = self._durable_session_map.lookup(chat_id)
            if not current or current.get("current") != engine_id:
                raise
            map_revision, mapping_revision = self._durable_session_map.revisions(chat_id)
            item = dict(current)
            item.update({"mapRevision": map_revision, "mappingRevision": mapping_revision})
        self._session_map = self._durable_session_map.flat_current()
        return item

    def _session_admission_lock(self, chat_id: str, owner: str) -> asyncio.Lock:
        key = (str(owner or ""), str(chat_id))
        return self._session_admission_locks.setdefault(key, asyncio.Lock())

    def _session_map_entry(self, chat_id: str) -> dict:
        return self._durable_session_map.lookup(chat_id) or {}

    def _binding_revision_for_chat(self, chat_id: str) -> int:
        from src.openclank.transcript_projection import get_managed_binding
        binding = get_managed_binding(chat_id, owner=self._owner or None)
        value = binding.get("workspaceRevision")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError("managed binding workspace revision is invalid")
        return value

    def _map_revisions_for_chat(self, chat_id: str) -> tuple[int, int]:
        return self._durable_session_map.revisions(chat_id)


    def set_session_delete_callback(self, callback) -> None:
        self._delete_session_callback = callback

    def set_session_workspace_adapter(self, callback) -> None:
        """Install the host's per-chat workspace adapter.

        The callback receives ``(chat_id, cwd)`` before the in-memory ACP
        context is committed under the chat lock. It may be sync or async. A
        rejection leaves both host state and the engine's local cwd unchanged;
        this keeps physical cwd, stable file-policy workspace, Copal, and
        memory axes separate while ensuring the next turn sees one committed
        value.
        """
        self._session_workspace_adapter = callback

    async def cleanup_session(self, mimo_session_id: str) -> None:
        await self.terminal_manager.cleanup_session(mimo_session_id)

    @staticmethod
    def _current_config(config_options: list) -> dict:
        return {
            option["id"]: option.get("currentValue")
            for option in config_options
            if isinstance(option, dict) and option.get("id")
        }

    def _capture_handshake(self, mimo_session: str, result: dict) -> None:
        previous = self._session_state.get(mimo_session, {})
        config_options = result.get("configOptions") or previous.get("config_options") or []
        models = result.get("models") or previous.get("models") or {}
        modes = result.get("modes") or previous.get("modes")
        initialized = getattr(self._client, "initialize_result", {}) or {}
        capabilities = initialized.get("agentCapabilities") or {}
        auth_methods = []
        for method in initialized.get("authMethods") or []:
            if isinstance(method, dict):
                auth_methods.append({
                    key: method[key]
                    for key in ("id", "name", "description")
                    if key in method
                })
        self._session_state[mimo_session] = {
            "models": models,
            "modes": modes,
            "config_options": config_options,
            "commands": previous.get("commands", []),
            "prompt_capabilities": capabilities.get(
                "promptCapabilities", {}
            ),
            "session_capabilities": capabilities,
            "auth_methods": auth_methods,
            "meta": result.get("_meta") or previous.get("meta") or {},
            "current": self._current_config(config_options),
            "desired": previous.get("desired", {}),
            "last_root_operation_id": previous.get("last_root_operation_id"),
            "workspace": previous.get("workspace"),
            "workspace_id": previous.get("workspace_id"),
            "goal_id": previous.get("goal_id"),
            "file_policy_workspace": previous.get("file_policy_workspace"),
            "copal_workspace": previous.get("copal_workspace"),
            "memory_workspace": previous.get("memory_workspace"),
        }

    def _bind_canonical_session(
        self,
        mimo_session: str,
        odysseus_session: str,
        owner: str,
        *,
        physical_cwd: Optional[str] = None,
        authority_workspace_id: str = "",
        memory_enabled: bool = True,
        engine_aliases: Optional[list[str]] = None,
        binding_revision: int = 0,
        map_revision: int = 0,
        mapping_revision: int = 0,
        copal_workspace: str = "default",
        memory_workspace_id: str = "",
    ) -> None:
        initial_cwd = str(physical_cwd or self._cwd).strip()
        self._session_context[mimo_session] = {
            "odysseus_session_id": odysseus_session,
            "owner": owner,
            "workspace": initial_cwd,
            "cwd": initial_cwd,
            "physical_cwd": initial_cwd,
            "file_policy_workspace": self._cwd,
            "copal_workspace": copal_workspace,
            "memory_workspace": memory_workspace_id or chat_workspace(),
            "incognito": False,
            "managed_binding": {
                "owner": owner,
                "stableChatID": odysseus_session,
                "engineSessionID": mimo_session,
                "engineAliases": list(engine_aliases or [])[:16],
                "memoryWorkspaceID": memory_workspace_id or chat_workspace(),
                "authorityWorkspaceID": authority_workspace_id,
                "copalWorkspace": copal_workspace,
                "physicalCwd": initial_cwd,
                "workspaceRevision": max(0, int(binding_revision)),
                "mapRevision": max(0, int(map_revision)),
                "mappingRevision": max(0, int(mapping_revision)),
                "memoryEnabled": memory_enabled,
                "transition": None,
            },
        }
        state = self._session_state.get(mimo_session)
        if state is None:
            return
        try:
            from src.openclank.transcript_projection import get_mimo_state

            previous = get_mimo_state(
                odysseus_session, owner=owner if owner else None
            )
        except KeyError:
            return
        if previous.get("desired"):
            state["desired"] = dict(previous["desired"])
        elif not state.get("desired"):
            state["desired"] = dict(state.get("current") or {})
        if previous.get("commands") and not state.get("commands"):
            state["commands"] = list(previous["commands"])
        previous_workspace = str(previous.get("workspace") or "").strip()
        binding = previous.get("managed_binding")
        if isinstance(binding, dict):
            previous_workspace = str(binding.get("physicalCwd") or "").strip()
            candidate = dict(self._session_context[mimo_session].get("managed_binding") or {})
            if candidate.get("engineSessionID") != binding.get("engineSessionID"):
                # A remap preserves stable authority axes while publishing the
                # newly admitted engine and locked map evidence.
                binding = {
                    **binding,
                    "engineSessionID": candidate.get("engineSessionID"),
                    "engineAliases": list(candidate.get("engineAliases") or []),
                    "mapRevision": candidate.get("mapRevision"),
                    "mappingRevision": candidate.get("mappingRevision"),
                    "memoryEnabled": candidate.get("memoryEnabled", binding.get("memoryEnabled", True)),
                    "transition": None,
                }
            self._session_context[mimo_session].update({
                "workspace": previous_workspace,
                "cwd": previous_workspace,
                "physical_cwd": previous_workspace,
                "managed_binding": dict(binding),
            })
        if previous_workspace and not isinstance(binding, dict):
            state["workspace"] = previous_workspace
            self._session_context[mimo_session].update(
                {
                    "workspace": previous_workspace,
                    "cwd": previous_workspace,
                    "physical_cwd": previous_workspace,
                }
            )
        for key in ("workspace_id", "goal_id", "file_policy_workspace", "copal_workspace", "memory_workspace"):
            value = str(previous.get(key) or "").strip()
            if value:
                state[key] = value
                self._session_context[mimo_session][key] = value
        self._persist_session_state(mimo_session, persist_binding=True)

    def _persist_session_state(
        self,
        mimo_session: str,
        *,
        required: bool = False,
        expected_workspace_revision: Optional[int] = None,
        persist_binding: bool = False,
    ) -> bool:
        context = self._session_context.get(mimo_session)
        state = self._session_state.get(mimo_session)
        if not context or state is None:
            if required:
                raise RuntimeError("session state is not bound to a persistable chat")
            return False
        saved_binding = None
        prior_binding = None
        try:
            from src.openclank.transcript_projection import get_managed_binding, save_managed_binding, save_mimo_state

            # Prepare a detached snapshot so a failed projection commit cannot
            # partially mutate the live in-memory state. Required callers use
            # the return value as the authority boundary before acknowledging.
            staged = dict(state)
            for key in ("workspace", "workspace_id", "goal_id", "file_policy_workspace", "copal_workspace", "memory_workspace"):
                if context.get(key):
                    staged[key] = context[key]
            managed_binding = copy.deepcopy(context.get("managed_binding")) if persist_binding else None
            if managed_binding:
                try:
                    prior_binding = get_managed_binding(
                        context["odysseus_session_id"], owner=context.get("owner") or self._owner
                    )
                except (KeyError, ValueError):
                    prior_binding = None
            save_kwargs = {"owner": context.get("owner") or self._owner or None}
            if expected_workspace_revision is not None:
                save_kwargs["expected_workspace_revision"] = expected_workspace_revision
            if managed_binding:
                binding_kwargs = {"owner": save_kwargs["owner"]}
                if expected_workspace_revision is not None:
                    binding_kwargs["expected_workspace_revision"] = expected_workspace_revision
                # Binding replacement is a four-axis CAS.  Workspace
                # revision alone cannot distinguish a stale remap from a
                # later winner that happens to have the same cwd revision.
                expected_binding = managed_binding
                binding_kwargs.update({
                    "expected_engine_session_id": (
                        expected_binding.get("engineSessionID")
                        if isinstance(expected_binding, dict) else None
                    ),
                    "expected_map_revision": (
                        int(expected_binding.get("mapRevision") or 0)
                        if isinstance(expected_binding, dict) else 0
                    ),
                    "expected_mapping_revision": (
                        int(expected_binding.get("mappingRevision") or 0)
                        if isinstance(expected_binding, dict) else 0
                    ),
                })
                saved_binding = save_managed_binding(
                    context["odysseus_session_id"], managed_binding, **binding_kwargs
                )
            state_save_kwargs = {"owner": save_kwargs["owner"]}
            if not managed_binding and expected_workspace_revision is not None:
                state_save_kwargs["expected_workspace_revision"] = expected_workspace_revision
            saved = save_mimo_state(context["odysseus_session_id"], staged, **state_save_kwargs)
            revision = saved["revision"]
            state.update(staged)
            if saved_binding:
                context["managed_binding"] = copy.deepcopy(saved_binding)
                state["managed_binding"] = copy.deepcopy(saved_binding)
            state["revision"] = revision
            return True
        except KeyError:
            if required:
                raise
            return False
        except Exception:
            if saved_binding and prior_binding:
                try:
                    save_managed_binding(
                        context["odysseus_session_id"],
                        prior_binding,
                        owner=context.get("owner") or self._owner,
                        expected_workspace_revision=saved_binding.get("workspaceRevision"),
                        expected_engine_session_id=saved_binding.get("engineSessionID"),
                        expected_map_revision=saved_binding.get("mapRevision"),
                        expected_mapping_revision=saved_binding.get("mappingRevision"),
                    )
                except Exception:
                    logger.exception("managed binding compensation failed for %s", mimo_session)
                    self._fence_admission(context.get("odysseus_session_id"), "state compensation")
            elif saved_binding:
                try:
                    from src.openclank.transcript_projection import delete_managed_binding

                    delete_managed_binding(
                        context["odysseus_session_id"],
                        owner=context.get("owner") or self._owner,
                        expected_workspace_revision=saved_binding.get("workspaceRevision"),
                        expected_engine_session_id=saved_binding.get("engineSessionID"),
                        expected_map_revision=saved_binding.get("mapRevision"),
                        expected_mapping_revision=saved_binding.get("mappingRevision"),
                    )
                except Exception:
                    logger.exception("managed binding deletion compensation failed for %s", mimo_session)
                    self._fence_admission(context.get("odysseus_session_id"), "state deletion compensation")
            raise

    def negotiated_state(self, odysseus_session: str) -> dict:
        mimo_session = self._session_map.get(odysseus_session)
        if mimo_session and mimo_session in self._session_state:
            return dict(self._session_state[mimo_session])
        try:
            from src.openclank.transcript_projection import get_mimo_state

            return get_mimo_state(odysseus_session)
        except KeyError:
            return {}

    async def set_config_option(
        self,
        odysseus_session: str,
        config_id: str,
        value: str,
        *,
        cwd: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> dict:
        mimo_session = self._session_map.get(odysseus_session)
        if mimo_session not in self._session_state:
            mimo_session = await self.ensure_session(
                odysseus_session, cwd=cwd, owner=owner
            )
        state = self._session_state.get(mimo_session, {})
        option = next(
            (
                item
                for item in state.get("config_options", [])
                if item.get("id") == config_id
            ),
            None,
        )
        if option is None:
            raise ValueError(f"Open Clank agent did not advertise config option {config_id!r}")
        allowed = [item.get("value") for item in option.get("options", [])]
        if allowed and value not in allowed:
            raise ValueError(
                f"Unsupported {config_id} value {value!r}; allowed: {allowed}"
            )
        result = await self._client.set_session_config_option(
            mimo_session, config_id, value
        )
        self._capture_handshake(mimo_session, result)
        state = self._session_state[mimo_session]
        state.setdefault("desired", {})[config_id] = value
        state.setdefault("current", {})[config_id] = value
        self._persist_session_state(mimo_session)
        return dict(state)

    async def _surface_permission(self, req: "PermissionRequest") -> None:
        """Route a pending permission request into its session's update queue.

        Raises when the session has no active turn — the handler then fails
        safe to reject instead of waiting on a prompt nobody can see.
        """
        q = self._queues.get(req.session_id)
        if q is None:
            raise RuntimeError(f"no active turn for session {req.session_id!r}")
        await q.put({"sessionUpdate": "_permission_request", "request": req})

    async def _surface_question(self, req: "PendingQuestion") -> None:
        q = self._queues.get(req.mimo_session_id)
        if q is None:
            raise RuntimeError(f"no active turn for session {req.mimo_session_id!r}")
        state = self._session_state.setdefault(req.mimo_session_id, {})
        state["pending_question"] = req.payload()
        self._persist_session_state(req.mimo_session_id)
        await q.put({"sessionUpdate": "_question_request", "request": req})

    async def _clear_question(self, req: "PendingQuestion") -> None:
        state = self._session_state.get(req.mimo_session_id)
        if state is not None:
            result = req.result()
            first = req.questions[0] if req.questions else {}
            answers = result.get("answers") if isinstance(result, dict) else None
            selected = answers[0][0] if answers and answers[0] else None
            options = first.get("options") or []
            approved = bool(
                first.get("key") == "plan_exit"
                and options
                and selected == options[0].get("label")
            )
            if approved and state.get("plan_state"):
                plan_state = state["plan_state"]
                plan_state["approved_revision"] = req.plan_revision
                plan_state["approved_digest"] = plan_state.get("digest")
                plan_state["status"] = "approved"
            state.pop("pending_question", None)
            self._persist_session_state(req.mimo_session_id)

    async def _handle_session_update(self, mimo_session_id: str, update: dict) -> None:
        """Route a session/update notification to the right session's queue."""
        # The cwd change request/ack callback is the authority boundary. This
        # event is only a local/UI projection after that callback succeeds;
        # stale or forged notifications must never mutate host workspace state.
        if update.get("sessionUpdate") == "available_commands_update":
            state = self._session_state.setdefault(mimo_session_id, {})
            state["commands"] = list(update.get("availableCommands") or [])
            self._persist_session_state(mimo_session_id)
        q = self._queues.get(mimo_session_id)
        if q is not None:
            await q.put(update)

    async def _handle_session_cwd_change(self, params: dict) -> dict:
        from src.openclank.managed_protocol import (
            validate_managed_method_request,
            validate_managed_method_result,
        )

        method = "_openclank/session/v1/cwd/change"
        request = validate_managed_method_request(method, params)
        result = await self._apply_session_cwd(
            request["sessionID"],
            request["requestedCwd"],
            expected_workspace_revision=request["expectedWorkspaceRevision"],
            transition_id=request["transitionID"],
        )
        return validate_managed_method_result(method, result)

    async def _handle_session_binding_read(self, params: dict) -> dict:
        from src.openclank.managed_protocol import (
            validate_managed_method_request,
            validate_managed_method_result,
        )

        method = "_openclank/session/v1/binding/read"
        request = validate_managed_method_request(method, params)
        mimo_session_id = request["sessionID"]
        context = self._session_context.get(mimo_session_id)
        if not context:
            raise ValueError("unknown managed engine session")
        chat_id = str(context.get("odysseus_session_id") or "").strip()
        durable_entry = self._durable_session_map.lookup(chat_id) if chat_id else None
        if not chat_id or not durable_entry or durable_entry.get("current") != mimo_session_id:
            raise ValueError("stale managed engine session")
        entry = self._session_map_entry(chat_id)
        if entry.get("current") != mimo_session_id:
            raise ValueError("managed engine session mapping changed")
        from src.openclank.transcript_projection import get_managed_binding
        binding = get_managed_binding(chat_id, owner=self._owner or None)
        if binding.get("engineSessionID") != mimo_session_id:
            raise ValueError("managed binding engine mismatch")
        if binding.get("owner") != self._owner or binding.get("stableChatID") != chat_id:
            raise ValueError("managed binding identity mismatch")
        required_strings = ("physicalCwd", "memoryWorkspaceID", "authorityWorkspaceID", "copalWorkspace")
        if any(not isinstance(binding.get(key), str) or not binding[key].strip() for key in required_strings):
            raise ValueError("managed binding is incomplete")
        if not os.path.isabs(str(binding["physicalCwd"])) or str(Path(binding["physicalCwd"]).resolve(strict=False)) != binding["physicalCwd"]:
            raise ValueError("managed binding cwd is not canonical")
        aliases = binding.get("engineAliases")
        if not isinstance(aliases, list) or len(aliases) > 16 or any(not isinstance(value, str) or not value for value in aliases) or len(set(aliases)) != len(aliases) or mimo_session_id in aliases:
            raise ValueError("managed binding aliases are invalid")
        for key in ("workspaceRevision", "mapRevision", "mappingRevision"):
            if isinstance(binding.get(key), bool) or not isinstance(binding.get(key), int) or binding[key] < 0:
                raise ValueError("managed binding revisions are invalid")
        map_revision, mapping_revision = self._durable_session_map.revisions(chat_id)
        if binding["mapRevision"] > map_revision or binding["mappingRevision"] != mapping_revision:
            raise ValueError("managed binding map revision mismatch")
        result = {
            "engineSessionID": mimo_session_id,
            "stableChatID": chat_id,
            "owner": binding["owner"],
            "canonicalCwd": binding["physicalCwd"],
            "workspaceRevision": int(binding.get("workspaceRevision") or 0),
            "authorityWorkspaceID": binding["authorityWorkspaceID"],
            "memoryWorkspaceID": binding["memoryWorkspaceID"],
            "copalWorkspace": binding["copalWorkspace"],
            "memoryEnabled": bool(binding.get("memoryEnabled", True)),
            "engineAliases": list(binding["engineAliases"]),
            "mapRevision": map_revision,
            "mappingRevision": mapping_revision,
        }
        return validate_managed_method_result(method, result)

    async def _apply_session_cwd(
        self,
        mimo_session_id: str,
        cwd: Any,
        *,
        expected_workspace_revision: Optional[int] = None,
        transition_id: Optional[str] = None,
    ) -> Optional[dict]:
        value = str(cwd or "").strip()
        transition_id = str(transition_id or uuid.uuid4().hex)
        if not value or not os.path.isabs(value):
            logger.warning("ignoring invalid session cwd update for %s", mimo_session_id)
            return {"outcome": "rejected", "transitionID": transition_id, "committed": False, "code": "invalid_cwd"}
        context = self._session_context.get(mimo_session_id)
        if not context:
            logger.warning("ignoring cwd update for unknown session %s", mimo_session_id)
            return {"outcome": "rejected", "transitionID": transition_id, "committed": False, "code": "unknown_session"}
        chat_id = str(context.get("odysseus_session_id") or "").strip()
        if not chat_id or self._session_map.get(chat_id) != mimo_session_id:
            logger.warning("ignoring stale cwd update for session %s", mimo_session_id)
            return {"outcome": "rejected", "transitionID": transition_id, "committed": False, "code": "stale_engine_session"}
        if str(context.get("owner") or self._owner) != self._owner:
            logger.warning("ignoring cwd update from a different owner for %s", chat_id)
            return {"outcome": "rejected", "transitionID": transition_id, "committed": False, "code": "owner_mismatch"}

        def authority_matches_context() -> bool:
            binding = context.get("managed_binding")
            if not isinstance(binding, dict):
                return False
            try:
                entry = self._durable_session_map.lookup(chat_id)
                if not entry or entry.get("current") != mimo_session_id:
                    return False
                map_revision, mapping_revision = self._durable_session_map.revisions(chat_id)
                from src.openclank.transcript_projection import get_managed_binding

                persisted = get_managed_binding(chat_id, owner=self._owner)
                if not isinstance(persisted, dict):
                    return False
                if persisted.get("transition") is not None:
                    return False
                for key, expected in (
                    ("owner", self._owner),
                    ("stableChatID", chat_id),
                    ("engineSessionID", mimo_session_id),
                    ("physicalCwd", binding.get("physicalCwd")),
                    ("workspaceRevision", binding.get("workspaceRevision")),
                    ("mapRevision", binding.get("mapRevision")),
                    ("mappingRevision", binding.get("mappingRevision")),
                    ("memoryWorkspaceID", binding.get("memoryWorkspaceID")),
                    ("authorityWorkspaceID", binding.get("authorityWorkspaceID")),
                    ("copalWorkspace", binding.get("copalWorkspace")),
                    ("memoryEnabled", binding.get("memoryEnabled")),
                    ("engineAliases", binding.get("engineAliases")),
                ):
                    if persisted.get(key) != expected or binding.get(key) != expected:
                        return False
                workspace_revision = binding.get("workspaceRevision")
                local_map_revision = binding.get("mapRevision")
                local_mapping_revision = binding.get("mappingRevision")
                if any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in (workspace_revision, local_map_revision, local_mapping_revision)
                ):
                    return False
                return local_map_revision <= map_revision and local_mapping_revision == mapping_revision
            except (KeyError, ValueError, TypeError, OSError):
                return False

        if not authority_matches_context():
            logger.warning("ignoring cwd update against stale durable authority for %s", chat_id)
            return {"outcome": "rejected", "transitionID": transition_id, "committed": False, "code": "stale_engine_session"}
        canonical = str(Path(value).expanduser().resolve(strict=False))
        lock = self._session_context_locks.setdefault(mimo_session_id, asyncio.Lock())
        async with lock:
            if not authority_matches_context():
                logger.warning("ignoring cwd update after durable authority changed for %s", chat_id)
                return {"outcome": "rejected", "transitionID": transition_id, "committed": False, "code": "stale_engine_session"}
            previous_context = copy.deepcopy(context)
            had_state = mimo_session_id in self._session_state
            state = self._session_state.setdefault(mimo_session_id, {})
            previous_state = copy.deepcopy(state)
            previous_binding = dict(context.get("managed_binding") or {})
            previous_cwd = str(previous_binding.get("physicalCwd") or context.get("workspace") or "").strip()
            previous_workspace_revision = int(previous_binding.get("workspaceRevision") or 0)
            if expected_workspace_revision is not None and previous_workspace_revision != int(expected_workspace_revision):
                return {"outcome": "rejected", "transitionID": transition_id, "committed": False, "code": "workspace_revision_conflict"}
            context["managed_binding"] = {
                **previous_binding,
                "owner": self._owner,
                "stableChatID": chat_id,
                "engineSessionID": mimo_session_id,
                "physicalCwd": previous_cwd or canonical,
                "workspaceRevision": previous_workspace_revision,
                "transition": {
                    "transitionID": transition_id,
                    "requestedCwd": canonical,
                    "previousRevision": previous_workspace_revision,
                },
            }
            callback = self._session_workspace_adapter
            if callback is None:
                context["managed_binding"] = previous_binding
                logger.warning("rejecting cwd update without a workspace authority for %s", chat_id)
                return {"outcome": "rejected", "transitionID": transition_id, "committed": False, "code": "binding_unavailable"}
            try:
                result = callback(chat_id, canonical, dict(context))
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                context["managed_binding"] = previous_binding
                logger.warning("rejecting cwd update for %s: %s", chat_id, exc)
                return None
            approved = str(result or "").strip()
            if approved != canonical:
                context["managed_binding"] = previous_binding
                logger.warning("rejecting noncanonical workspace approval for %s", chat_id)
                return {"outcome": "rejected", "transitionID": transition_id, "committed": False, "code": "workspace_rejected"}
            # Approval is complete before any ACP-visible state or transcript
            # state changes. Stable policy/Copal/memory identities stay intact.
            changed = canonical != previous_cwd
            next_workspace_revision = previous_workspace_revision + (1 if changed else 0)
            binding = {
                **previous_binding,
                "owner": self._owner,
                "stableChatID": chat_id,
                "engineSessionID": mimo_session_id,
                "engineAliases": list(previous_binding.get("engineAliases") or []),
                "memoryWorkspaceID": str(previous_binding.get("memoryWorkspaceID") or chat_workspace()),
                "authorityWorkspaceID": str(previous_binding.get("authorityWorkspaceID") or context.get("authority_workspace_id") or ""),
                "physicalCwd": canonical,
                "workspaceRevision": next_workspace_revision,
                "memoryEnabled": bool(previous_binding.get("memoryEnabled", True)),
                "transition": None,
            }
            context.update({"workspace": canonical, "cwd": canonical, "physical_cwd": canonical, "managed_binding": binding})
            state["workspace"] = canonical
            state["managed_binding"] = binding
            try:
                self._persist_session_state(
                    mimo_session_id,
                    required=True,
                    expected_workspace_revision=previous_workspace_revision,
                    persist_binding=True,
                )
            except Exception as exc:
                context.clear()
                context.update(previous_context)
                if had_state:
                    state.clear()
                    state.update(previous_state)
                else:
                    self._session_state.pop(mimo_session_id, None)
                logger.warning("rejecting cwd update for %s: persistence failed: %s", chat_id, exc)
                return None
            result = {
                "outcome": "accepted",
                "canonicalCwd": canonical,
                "workspaceRevision": next_workspace_revision,
                "changed": changed,
                "transitionID": transition_id,
            }
            return result

    async def open_session(
        self,
        cwd: Optional[str] = None,
        owner: Optional[str] = None,
        odysseus_session: str = "",
        extra_mcp_servers: Optional[list[dict]] = None,
        with_agent_tools: bool = True,
        with_memory: bool = True,
        copal_workspace: str = "default",
        authority_workspace_id: str = "",
        memory_workspace_id: str = "",
        publish_handshake: bool = True,
    ) -> str:
        """Create a new mimo session and return the ses_… id directly.

        Also stores the available models from mimo's handshake so the bridge
        can match thesius model names to mimo model IDs later.
        """
        requested_workspace = str(cwd or "").strip()
        target_cwd = requested_workspace or self._cwd
        mcp_servers = ([
            lifetools_mcp_descriptor(
                owner=owner if owner is not None else self._owner,
                session_id=odysseus_session,
                workspace=requested_workspace,
                authority_workspace_id=authority_workspace_id,
                memory_workspace_id=memory_workspace_id or chat_workspace(),
                memory_enabled=with_memory,
                copal_workspace=copal_workspace,
                binding_revision=0,
                map_revision=0,
                mapping_revision=0,
            ),
        ] + list(extra_mcp_servers or [])) if with_agent_tools else []
        result = await self._client.new_session(target_cwd, mcp_servers=mcp_servers)
        session_id = result["sessionId"]
        if publish_handshake:
            self._capture_handshake(session_id, result)
        else:
            # Admission owns publication.  Keep the handshake private until
            # the durable map and complete managed binding both exist.
            self._pending_session_results[session_id] = dict(result)

        # Store mimo's available models so we can match thesius model names
        models = result.get("models", {})
        available = models.get("availableModels", [])
        if available and publish_handshake:
            self._session_models[session_id] = available
            self.available_models = available
            logger.info("mimo session %s: %d models available", session_id, len(available))

        return session_id

    def _publish_admitted_session(self, session_id: str) -> None:
        result = self._pending_session_results.pop(session_id, None)
        if result is None:
            return
        self._capture_handshake(session_id, result)
        available = (result.get("models") or {}).get("availableModels", [])
        if available:
            self._session_models[session_id] = available

    def _finalize_admitted_session(self, session_id: str) -> None:
        available = self._session_models.get(session_id)
        if available:
            self.available_models = list(available)

    async def _discard_unbound_engine_session(
        self,
        session_id: str,
        *,
        chat_id: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> None:
        """Destructively discard a privately created engine session.

        The engine requires the canonical physical cwd to resolve its durable
        store. Prefer the exact persisted candidate binding, falling back only
        to a caller-supplied canonical candidate cwd for the pre-binding
        failure window.
        """
        candidate_cwd = None
        if chat_id:
            try:
                from src.openclank.transcript_projection import get_managed_binding

                binding = get_managed_binding(chat_id, owner=self._owner)
                if binding.get("engineSessionID") == session_id:
                    value = binding.get("physicalCwd")
                    if isinstance(value, str) and value and os.path.isabs(value) and str(Path(value).resolve(strict=False)) == value:
                        candidate_cwd = value
            except KeyError:
                pass
            except Exception:
                logger.warning("unable to read candidate binding before discard %s", session_id)
        if candidate_cwd is None and isinstance(cwd, str) and cwd and os.path.isabs(cwd):
            candidate_cwd = str(Path(cwd).resolve(strict=False))
        if candidate_cwd is None:
            if chat_id:
                self._fence_admission(chat_id, "candidate discard cwd unavailable")
            raise RuntimeError("candidate discard requires an authoritative canonical cwd")
        self._pending_session_results.pop(session_id, None)
        self._session_models.pop(session_id, None)
        self._session_state.pop(session_id, None)
        self._session_context.pop(session_id, None)
        self._turns.pop(session_id, None)
        self._queues.pop(session_id, None)
        try:
            await self.cleanup_session(session_id)
        except Exception:
            logger.warning("failed to clean up unbound engine session %s", session_id)
        discarded = False
        discard = getattr(self._client, "discard_session", None)
        if discard is not None:
            try:
                await discard(session_id, candidate_cwd)
                discarded = True
            except Exception:
                logger.warning("failed to discard unbound engine session %s", session_id)
        if not discarded:
            if chat_id:
                self._fence_admission(chat_id, "candidate engine discard")
            raise RuntimeError("candidate engine discard was not confirmed")

    def _fence_admission(self, chat_id: str, reason: str) -> None:
        """Persist fail-closed authority when cross-store repair is unknown."""
        try:
            self._durable_session_map.fence(chat_id)
            self._session_map = self._durable_session_map.flat_current()
        except Exception as exc:
            logger.exception("unable to persist admission fence for %s (%s)", chat_id, reason)
            raise RuntimeError("managed admission fence could not be persisted") from exc

    def _compensate_candidate_binding(
        self,
        chat_id: str,
        candidate_id: str,
        owner: str,
        previous_binding: Optional[dict],
    ) -> None:
        """Restore host authority only while this candidate still owns the map."""
        if self._session_map_entry(chat_id).get("current") != candidate_id:
            raise RuntimeError("candidate map authority moved before binding compensation")
        from src.openclank.transcript_projection import (
            delete_managed_binding,
            get_managed_binding,
            save_managed_binding,
        )

        current = get_managed_binding(chat_id, owner=owner)
        candidate_revision = current.get("workspaceRevision")
        candidate_engine = current.get("engineSessionID")
        candidate_map = current.get("mapRevision")
        candidate_mapping = current.get("mappingRevision")
        if candidate_engine != candidate_id:
            raise RuntimeError("candidate binding authority moved before compensation")
        if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
                   for value in (candidate_revision, candidate_map, candidate_mapping)):
            raise RuntimeError("candidate managed binding has invalid CAS axes")
        cas = {
            "expected_engine_session_id": candidate_engine,
            "expected_map_revision": candidate_map,
            "expected_mapping_revision": candidate_mapping,
        }
        if isinstance(previous_binding, dict):
            save_managed_binding(
                chat_id,
                previous_binding,
                owner=owner,
                expected_workspace_revision=candidate_revision,
                **cas,
            )
        else:
            delete_managed_binding(
                chat_id,
                owner=owner,
                expected_workspace_revision=candidate_revision,
                **cas,
            )

    def _compensate_staged_binding(
        self,
        chat_id: str,
        candidate_id: str,
        owner: str,
        previous_binding: Optional[dict],
    ) -> None:
        """CAS-restore a projection staged before map publication.

        This path is used when map admission itself loses.  It deliberately
        does not infer authority from the map: the complete candidate binding
        axes are the cleanup guard, so a later winner cannot be removed.
        """
        from src.openclank.transcript_projection import (
            delete_managed_binding,
            get_managed_binding,
            save_managed_binding,
        )
        current = get_managed_binding(chat_id, owner=owner)
        if current.get("engineSessionID") != candidate_id:
            raise RuntimeError("staged binding authority moved before compensation")
        cas = {
            "expected_workspace_revision": current.get("workspaceRevision"),
            "expected_engine_session_id": candidate_id,
            "expected_map_revision": current.get("mapRevision"),
            "expected_mapping_revision": current.get("mappingRevision"),
        }
        if isinstance(previous_binding, dict):
            save_managed_binding(chat_id, previous_binding, owner=owner, **cas)
        else:
            delete_managed_binding(chat_id, owner=owner, **cas)

    def _align_binding_to_map(self, chat_id: str, engine_id: str, owner: str) -> None:
        """Refresh only the map evidence after a CAS rollback mutation."""
        from src.openclank.transcript_projection import get_managed_binding, save_managed_binding

        binding = get_managed_binding(chat_id, owner=owner)
        if binding.get("engineSessionID") != engine_id:
            raise RuntimeError("rollback binding engine mismatch")
        map_revision, mapping_revision = self._durable_session_map.revisions(chat_id)
        if binding.get("mapRevision") == map_revision and binding.get("mappingRevision") == mapping_revision:
            return
        updated = dict(binding)
        updated["mapRevision"] = map_revision
        updated["mappingRevision"] = mapping_revision
        save_managed_binding(
            chat_id,
            updated,
            owner=owner,
            expected_workspace_revision=int(binding.get("workspaceRevision") or 0),
            expected_engine_session_id=engine_id,
            expected_map_revision=binding.get("mapRevision"),
            expected_mapping_revision=binding.get("mappingRevision"),
        )

    def _stage_candidate_binding(
        self,
        chat_id: str,
        candidate_id: str,
        owner: str,
        *,
        previous_binding: Optional[dict],
        cwd: str,
        authority_workspace_id: str,
        memory_enabled: bool,
        copal_workspace: str,
        memory_workspace_id: str,
    ) -> dict:
        """Persist a complete candidate binding before publishing its map row.

        The file map and SQLite projection cannot share one transaction.  The
        safe ordering is therefore projection first, map second: a crash can
        leave an unreferenced binding, but never an authoritative map entry
        without its complete binding.  Every axis is checked again by both
        stores and cleanup is CAS-protected.
        """
        from src.openclank.transcript_projection import save_managed_binding

        entry = self._durable_session_map.lookup(chat_id)
        old_engine = str(entry.get("current") or "") if entry else None
        map_revision = self._durable_session_map.map_revision()
        mapping_revision = int(entry.get("revision") or 0) if entry else 0
        prior = dict(previous_binding) if isinstance(previous_binding, dict) else {}
        if prior:
            if prior.get("owner") != owner or prior.get("stableChatID") != chat_id:
                raise RuntimeError("managed binding identity mismatch")
            prior_mapping_revision = int(prior.get("mappingRevision") or 0)
            if prior_mapping_revision != mapping_revision:
                # A crash after projection-first staging leaves an orphaned
                # candidate while the old map row remains authoritative.
                # Recover that exact old row under the candidate binding CAS,
                # then stage the requested candidate from the recovered pair.
                if old_engine and prior.get("engineSessionID") != old_engine and old_engine in (prior.get("engineAliases") or []) and prior_mapping_revision == mapping_revision + 1:
                    from src.openclank.transcript_projection import save_managed_binding

                    recovered = dict(prior)
                    recovered["engineSessionID"] = old_engine
                    recovered["engineAliases"] = [value for value in (prior.get("engineAliases") or []) if value != old_engine and value != prior.get("engineSessionID")]
                    recovered["mapRevision"] = map_revision
                    recovered["mappingRevision"] = mapping_revision
                    recovered["transition"] = None
                    save_managed_binding(
                        chat_id,
                        recovered,
                        owner=owner,
                        expected_workspace_revision=int(prior.get("workspaceRevision") or 0),
                        expected_engine_session_id=prior.get("engineSessionID"),
                        expected_map_revision=int(prior.get("mapRevision") or 0),
                        expected_mapping_revision=int(prior.get("mappingRevision") or 0),
                    )
                    prior = recovered
                else:
                    raise RuntimeError("managed binding is stale relative to the durable map")
            physical_cwd = str(prior.get("physicalCwd") or "").strip()
            workspace_revision = int(prior.get("workspaceRevision") or 0)
            aliases = list(prior.get("engineAliases") or [])
            memory_workspace_id = str(prior.get("memoryWorkspaceID") or memory_workspace_id)
            authority_workspace_id = str(prior.get("authorityWorkspaceID") or authority_workspace_id)
            copal_workspace = str(prior.get("copalWorkspace") or copal_workspace)
            memory_enabled = bool(prior.get("memoryEnabled", memory_enabled))
        else:
            physical_cwd = str(cwd or "").strip()
            workspace_revision = 0
            aliases = []
        if not physical_cwd or not os.path.isabs(physical_cwd):
            raise RuntimeError("managed candidate has no canonical physical cwd")
        candidate = {
            **prior,
            "owner": owner,
            "stableChatID": chat_id,
            "engineSessionID": candidate_id,
            "engineAliases": [value for value in dict.fromkeys([old_engine, *aliases]) if value and value != candidate_id][:16],
            "memoryWorkspaceID": memory_workspace_id,
            "authorityWorkspaceID": authority_workspace_id,
            "copalWorkspace": copal_workspace,
            "physicalCwd": str(Path(physical_cwd).resolve(strict=False)),
            "workspaceRevision": workspace_revision,
            "mapRevision": map_revision + 1,
            "mappingRevision": mapping_revision + 1,
            "memoryEnabled": memory_enabled,
            "transition": None,
        }
        expected_binding_map_revision = int(prior.get("mapRevision") or 0) if prior else 0
        expected_binding_mapping_revision = int(prior.get("mappingRevision") or 0) if prior else 0
        save_managed_binding(
            chat_id,
            candidate,
            owner=owner,
            expected_workspace_revision=workspace_revision,
            expected_engine_session_id=old_engine,
            expected_map_revision=expected_binding_map_revision,
            expected_mapping_revision=expected_binding_mapping_revision,
        )
        return candidate

    async def ensure_session(
        self,
        odysseus_session: str,
        cwd: Optional[str] = None,
        owner: Optional[str] = None,
        extra_mcp_servers: Optional[list[dict]] = None,
        with_memory: bool = True,
        copal_workspace: str = "default",
        authority_workspace_id: str = "",
    ) -> str:
        effective_owner = owner if owner is not None else self._owner
        async with self._session_admission_lock(odysseus_session, effective_owner):
            return await self._ensure_session_locked(
                odysseus_session,
                cwd=cwd,
                owner=owner,
                extra_mcp_servers=extra_mcp_servers,
                with_memory=with_memory,
                copal_workspace=copal_workspace,
                authority_workspace_id=authority_workspace_id,
            )

    async def _ensure_session_locked(
        self,
        odysseus_session: str,
        cwd: Optional[str] = None,
        owner: Optional[str] = None,
        extra_mcp_servers: Optional[list[dict]] = None,
        with_memory: bool = True,
        copal_workspace: str = "default",
        authority_workspace_id: str = "",
    ) -> str:
        """Load the mimo session into the ACP agent's memory.

        odysseus_session IS the mimo session id. Calls resume_session to
        ensure the ACP agent knows about it (idempotent — no-op if already
        loaded, re-loads from DB if this is a fresh mimo child).

        Also refreshes the available models from mimo so the bridge always
        has the latest model list (covers session reconnect after restart).
        """
        target_entry = self._durable_session_map.lookup(odysseus_session)
        target = target_entry.get("current") if target_entry else None
        if target is None:
            effective_owner = owner if owner is not None else self._owner
            previous_binding = None
            try:
                from src.openclank.transcript_projection import get_managed_binding

                previous_binding = get_managed_binding(
                    odysseus_session, owner=effective_owner or None
                ) or None
            except (KeyError, ValueError):
                previous_binding = None
            if previous_binding:
                from src.openclank.transcript_projection import delete_managed_binding

                try:
                    await self._discard_unbound_engine_session(
                        str(previous_binding["engineSessionID"]),
                        chat_id=odysseus_session,
                    )
                    delete_managed_binding(
                        odysseus_session,
                        owner=effective_owner,
                        expected_workspace_revision=previous_binding.get("workspaceRevision"),
                        expected_engine_session_id=previous_binding.get("engineSessionID"),
                        expected_map_revision=previous_binding.get("mapRevision"),
                        expected_mapping_revision=previous_binding.get("mappingRevision"),
                    )
                except Exception as exc:
                    self._fence_admission(odysseus_session, "orphan managed binding")
                    raise RuntimeError("orphan managed binding could not be reconciled") from exc
                previous_binding = None
            new_id = await self.open_session(
                cwd=cwd,
                owner=owner,
                odysseus_session=odysseus_session,
                extra_mcp_servers=extra_mcp_servers,
                with_agent_tools=False,
                with_memory=with_memory,
                copal_workspace=copal_workspace,
                authority_workspace_id=authority_workspace_id,
                publish_handshake=False,
            )
            try:
                self._stage_candidate_binding(
                    odysseus_session,
                    new_id,
                    effective_owner,
                    previous_binding=previous_binding,
                    cwd=cwd or self._cwd,
                    authority_workspace_id=authority_workspace_id,
                    memory_enabled=with_memory,
                    copal_workspace=copal_workspace,
                    memory_workspace_id=chat_workspace(),
                )
                self._bind_engine_session(
                    odysseus_session,
                    new_id,
                    expected_current=None,
                    expected_mapping_revision=0,
                )
                self._publish_admitted_session(new_id)
                self._bind_canonical_session(
                    new_id,
                    odysseus_session,
                    owner if owner is not None else self._owner,
                    physical_cwd=cwd or self._cwd,
                    authority_workspace_id=authority_workspace_id,
                    memory_enabled=with_memory,
                    map_revision=self._map_revisions_for_chat(odysseus_session)[0],
                    mapping_revision=self._map_revisions_for_chat(odysseus_session)[1],
                    copal_workspace=copal_workspace,
                )
                await self.resume_session(
                    odysseus_session,
                    new_id,
                    with_memory=with_memory,
                    copal_workspace=copal_workspace,
                    authority_workspace_id=authority_workspace_id,
                )
                self._finalize_admitted_session(new_id)
            except Exception:
                preserve_candidate = False
                candidate_discarded = False
                try:
                    winner = self._session_map_entry(odysseus_session).get("current")
                    if winner and winner != new_id:
                        await self._discard_unbound_engine_session(new_id, chat_id=odysseus_session, cwd=cwd or self._cwd)
                        candidate_discarded = True
                        self._session_map = self._durable_session_map.flat_current()
                        await self.resume_session(
                            odysseus_session,
                            winner,
                            with_memory=with_memory,
                            copal_workspace=copal_workspace,
                            authority_workspace_id=authority_workspace_id,
                        )
                        return winner
                    if winner == new_id:
                        try:
                            self._compensate_candidate_binding(
                                odysseus_session,
                                new_id,
                                effective_owner,
                                previous_binding,
                            )
                        except Exception:
                            logger.exception(
                                "candidate binding compensation failed for %s", odysseus_session
                            )
                            self._fence_admission(odysseus_session, "candidate binding compensation")
                            preserve_candidate = True
                            raise
                        try:
                            current_entry = self._session_map_entry(odysseus_session)
                            self._durable_session_map.forget(
                                odysseus_session,
                                expected_map_revision=self._durable_session_map.map_revision(),
                                expected_current=new_id,
                                expected_mapping_revision=int(current_entry.get("revision") or 0),
                            )
                        except Exception:
                            self._fence_admission(odysseus_session, "candidate map rollback")
                            preserve_candidate = True
                            raise
                        self._session_map = self._durable_session_map.flat_current()
                    else:
                        self._compensate_staged_binding(
                            odysseus_session,
                            new_id,
                            effective_owner,
                            previous_binding,
                        )
                finally:
                    if not preserve_candidate and not candidate_discarded:
                        await self._discard_unbound_engine_session(new_id, chat_id=odysseus_session, cwd=cwd or self._cwd)
                raise
            return new_id
        from src.openclank.transcript_projection import get_managed_binding
        orphan_binding = get_managed_binding(odysseus_session, owner=self._owner or None)
        current_mapping_revision = int(target_entry.get("revision") or 0)
        orphan_engine = orphan_binding.get("engineSessionID") if isinstance(orphan_binding, dict) else None
        orphan_map_revision = orphan_binding.get("mapRevision") if isinstance(orphan_binding, dict) else None
        orphan_mapping_revision = orphan_binding.get("mappingRevision") if isinstance(orphan_binding, dict) else None
        if (
            isinstance(orphan_engine, str)
            and orphan_engine
            and orphan_engine != target
            and target in (orphan_binding.get("engineAliases") or [])
            and isinstance(orphan_map_revision, int)
            and isinstance(orphan_mapping_revision, int)
            and orphan_mapping_revision == current_mapping_revision + 1
        ):
            await self._discard_unbound_engine_session(
                orphan_engine,
                chat_id=odysseus_session,
                cwd=orphan_binding.get("physicalCwd"),
            )
            from src.openclank.transcript_projection import save_managed_binding

            recovered = dict(orphan_binding)
            recovered["engineSessionID"] = target
            recovered["engineAliases"] = [
                value for value in (orphan_binding.get("engineAliases") or [])
                if value not in {target, orphan_engine}
            ]
            recovered["mapRevision"] = self._durable_session_map.map_revision()
            recovered["mappingRevision"] = current_mapping_revision
            recovered["transition"] = None
            try:
                save_managed_binding(
                    odysseus_session,
                    recovered,
                    owner=self._owner,
                    expected_workspace_revision=orphan_binding.get("workspaceRevision"),
                    expected_engine_session_id=orphan_engine,
                    expected_map_revision=orphan_map_revision,
                    expected_mapping_revision=orphan_mapping_revision,
                )
            except Exception as exc:
                self._fence_admission(odysseus_session, "orphan binding recovery")
                raise RuntimeError("orphan managed binding recovery was not confirmed") from exc
        restored_workspace = self.mapped_session_workspace(odysseus_session)
        persisted_binding = get_managed_binding(odysseus_session, owner=self._owner or None)
        persisted_owner = persisted_binding["owner"]
        persisted_memory = str(persisted_binding["memoryWorkspaceID"])
        persisted_authority = str(persisted_binding["authorityWorkspaceID"])
        persisted_copal = str(persisted_binding["copalWorkspace"])
        persisted_memory_enabled = bool(persisted_binding["memoryEnabled"])
        persisted_aliases = list(persisted_binding["engineAliases"])
        persisted_binding_revision = int(persisted_binding["workspaceRevision"])
        persisted_map_revision = int(persisted_binding["mapRevision"])
        persisted_mapping_revision = int(persisted_binding["mappingRevision"])
        # A mapped chat is host-owned state. A caller's cwd can seed only a new
        # chat; it must not revert a persisted engine workspace on reconnect.
        resume_cwd = str(restored_workspace or cwd or self._cwd).strip()
        mcp_servers = [
            lifetools_mcp_descriptor(
                owner=persisted_owner,
                session_id=odysseus_session,
                workspace=resume_cwd,
                authority_workspace_id=persisted_authority,
                memory_workspace_id=persisted_memory,
                memory_enabled=persisted_memory_enabled,
                copal_workspace=persisted_copal,
                engine_session_aliases=persisted_aliases,
                binding_revision=persisted_binding_revision,
                map_revision=persisted_map_revision,
                mapping_revision=persisted_mapping_revision,
            ),
            *(extra_mcp_servers or []),
        ]
        try:
            result = await self._client.resume_session(target, resume_cwd, mcp_servers=mcp_servers)
        except RPCError as e:
            if not e.session_missing:
                raise
            # mimo doesn't know this session (state predates the isolated
            # MIMOCODE_HOME, or its store was wiped). The full chat history
            # rides in every prompt, so a fresh mimo session continues the
            # conversation seamlessly — remap and persist.
            logger.warning(
                "mimo resume failed for %s (%s) — opening a fresh mimo session", target, e
            )
            previous_binding = None
            try:
                from src.openclank.transcript_projection import get_managed_binding

                previous_binding = get_managed_binding(
                    odysseus_session,
                    owner=(owner if owner is not None else self._owner) or None,
                ) or None
            except (KeyError, ValueError):
                previous_binding = None
            new_id = await self.open_session(
                cwd=resume_cwd,
                owner=owner,
                odysseus_session=odysseus_session,
                extra_mcp_servers=extra_mcp_servers,
                with_agent_tools=False,
                with_memory=with_memory,
                copal_workspace=copal_workspace,
                authority_workspace_id=authority_workspace_id,
                memory_workspace_id=str((self._session_context.get(target) or {}).get("managed_binding", {}).get("memoryWorkspaceID") or chat_workspace()),
                publish_handshake=False,
            )
            try:
                self._stage_candidate_binding(
                    odysseus_session,
                    new_id,
                    owner if owner is not None else self._owner,
                    previous_binding=previous_binding,
                    cwd=resume_cwd,
                    authority_workspace_id=authority_workspace_id,
                    memory_enabled=with_memory,
                    copal_workspace=copal_workspace,
                    memory_workspace_id=str((self._session_context.get(target) or {}).get("managed_binding", {}).get("memoryWorkspaceID") or chat_workspace()),
                )
                self._bind_engine_session(
                    odysseus_session,
                    new_id,
                    expected_current=target,
                    expected_mapping_revision=int((target_entry or {}).get("revision") or 0),
                )
                self._publish_admitted_session(new_id)
                self._bind_canonical_session(
                    new_id,
                    odysseus_session,
                    owner if owner is not None else self._owner,
                    physical_cwd=resume_cwd,
                    authority_workspace_id=authority_workspace_id,
                    memory_enabled=with_memory,
                    engine_aliases=list(self._session_map_entry(odysseus_session).get("aliases", [])),
                    map_revision=self._map_revisions_for_chat(odysseus_session)[0],
                    mapping_revision=self._map_revisions_for_chat(odysseus_session)[1],
                    copal_workspace=copal_workspace,
                )
                await self.resume_session(
                    odysseus_session,
                    new_id,
                    with_memory=with_memory,
                    copal_workspace=copal_workspace,
                    authority_workspace_id=authority_workspace_id,
                )
                self._finalize_admitted_session(new_id)
            except Exception:
                preserve_candidate = False
                candidate_discarded = False
                try:
                    winner = self._session_map_entry(odysseus_session).get("current")
                    if winner and winner != new_id:
                        await self._discard_unbound_engine_session(new_id, chat_id=odysseus_session, cwd=resume_cwd)
                        candidate_discarded = True
                        self._session_map = self._durable_session_map.flat_current()
                        await self.resume_session(
                            odysseus_session,
                            winner,
                            with_memory=with_memory,
                            copal_workspace=copal_workspace,
                            authority_workspace_id=authority_workspace_id,
                        )
                        return winner
                    if winner == new_id:
                        try:
                            self._compensate_candidate_binding(
                                odysseus_session,
                                new_id,
                                owner if owner is not None else self._owner,
                                previous_binding,
                            )
                        except Exception:
                            logger.exception(
                                "candidate binding compensation failed for %s", odysseus_session
                            )
                            self._fence_admission(odysseus_session, "candidate binding compensation")
                            preserve_candidate = True
                            raise
                        try:
                            current_entry = self._session_map_entry(odysseus_session)
                            self._durable_session_map.bind(
                                odysseus_session,
                                target,
                                expected_map_revision=self._durable_session_map.map_revision(),
                                expected_current=new_id,
                                expected_mapping_revision=int(current_entry.get("revision") or 0),
                            )
                            self._align_binding_to_map(
                                odysseus_session,
                                target,
                                owner if owner is not None else self._owner,
                            )
                        except Exception:
                            self._fence_admission(odysseus_session, "candidate map rollback or binding alignment")
                            preserve_candidate = True
                            raise
                        self._session_map = self._durable_session_map.flat_current()
                    else:
                        self._compensate_staged_binding(
                            odysseus_session,
                            new_id,
                            owner if owner is not None else self._owner,
                            previous_binding,
                        )
                finally:
                    if not preserve_candidate and not candidate_discarded:
                        await self._discard_unbound_engine_session(new_id, chat_id=odysseus_session, cwd=resume_cwd)
                raise
            return new_id

        # Refresh available models from mimo (handles reconnect after restart)
        models = result.get("models", {})
        available = models.get("availableModels", [])
        if available:
            self._session_models[target] = available
            self.available_models = available

        self._capture_handshake(target, result)
        self._bind_canonical_session(
            target,
            odysseus_session,
            owner if owner is not None else self._owner,
            physical_cwd=resume_cwd,
            authority_workspace_id=authority_workspace_id,
            memory_enabled=with_memory,
            engine_aliases=list(self._session_map_entry(odysseus_session).get("aliases", [])),
            map_revision=self._map_revisions_for_chat(odysseus_session)[0],
            mapping_revision=self._map_revisions_for_chat(odysseus_session)[1],
            copal_workspace=copal_workspace,
        )

        return target

    async def resume_session(
        self,
        odysseus_session: str,
        mimo_session_id: str,
        *,
        with_memory: bool = True,
        copal_workspace: str = "default",
        authority_workspace_id: str = "",
    ) -> None:
        """Re-establish a session after a crash/restart.

        odysseus_session IS the mimo session id. Re-attaches the standard
        life-tools server so the session has tools and a memory scope carrier.
        """
        workspace = self.mapped_session_workspace(odysseus_session)
        durable_entry = self._durable_session_map.lookup(odysseus_session)
        if not durable_entry or durable_entry.get("current") != mimo_session_id:
            raise RuntimeError("managed session engine is no longer the durable current mapping")
        from src.openclank.transcript_projection import get_managed_binding
        persisted_binding = get_managed_binding(odysseus_session, owner=self._owner or None)
        map_revision, mapping_revision = self._durable_session_map.revisions(odysseus_session)
        binding_map_revision = persisted_binding.get("mapRevision")
        binding_mapping_revision = persisted_binding.get("mappingRevision")
        binding_workspace_revision = persisted_binding.get("workspaceRevision")
        valid_revisions = all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in (binding_map_revision, binding_mapping_revision, binding_workspace_revision)
        )
        if (
            persisted_binding.get("owner") != self._owner
            or persisted_binding.get("stableChatID") != odysseus_session
            or persisted_binding.get("engineSessionID") != mimo_session_id
            or not valid_revisions
            or binding_map_revision > map_revision
            or binding_mapping_revision != mapping_revision
            or persisted_binding.get("transition") is not None
        ):
            raise RuntimeError("managed session binding is no longer the durable current mapping")
        mcp_servers = [
            lifetools_mcp_descriptor(
                owner=persisted_binding["owner"],
                session_id=odysseus_session,
                workspace=workspace,
                authority_workspace_id=str(persisted_binding["authorityWorkspaceID"]),
                memory_workspace_id=str(persisted_binding["memoryWorkspaceID"]),
                memory_enabled=bool(persisted_binding["memoryEnabled"]),
                copal_workspace=str(persisted_binding["copalWorkspace"]),
                engine_session_aliases=list(persisted_binding["engineAliases"]),
                binding_revision=binding_workspace_revision,
                map_revision=map_revision,
                mapping_revision=mapping_revision,
            ),
        ]
        await self._client.resume_session(mimo_session_id, workspace, mcp_servers=mcp_servers)

    def _match_model(self, mimo_session: str, thesius_model: str) -> str:
        """Match a thesius model name to a mimo modelId.

        Tries exact name match, then prefix match, then falls back to
        passing the raw thesius name (mimo's parseModelSelection handles
        provider/model format like "deepseek/deepseek-v4-pro").
        """
        available = self._session_models.get(mimo_session, [])
        thesius_lower = thesius_model.lower()

        # Exact name match
        for m in available:
            if m.get("name", "").lower() == thesius_lower:
                return m["modelId"]

        # Prefix match (e.g. "deepseek-v4" matches "DeepSeek/DeepSeek V4 Pro")
        for m in available:
            if thesius_lower in m.get("name", "").lower():
                return m["modelId"]

        # Try provider/model format if the thesius name already has a slash
        if "/" in thesius_model:
            return thesius_model

        # Last resort: guess provider from model name
        for m in available:
            mid = m.get("modelId", "")
            if thesius_lower in mid.lower():
                return mid

        return thesius_model

    def get_session_models(self, odysseus_session: str) -> list:
        """Return mimo's available models for a session.

        Each entry is {modelId, name} from mimo's handshake.
        The thesius UI should use this instead of its own endpoint DB —
        model authority lives in mimo.
        """
        return self._session_models.get(odysseus_session, [])

    def mapped_session_id(self, odysseus_session: str) -> str:
        self._session_map = self._durable_session_map.flat_current()
        return self._session_map.get(odysseus_session, odysseus_session)

    def mapped_session_workspace(self, odysseus_session: str) -> str:
        entry = self._durable_session_map.lookup(odysseus_session)
        self._session_map = self._durable_session_map.flat_current()
        mimo_session = str(entry.get("current") or "") if entry else odysseus_session
        if entry:
            try:
                from src.openclank.transcript_projection import get_managed_binding
                binding = get_managed_binding(odysseus_session, owner=self._owner or None)
            except Exception as exc:
                raise RuntimeError("managed session binding is unavailable") from exc
            map_revision, mapping_revision = self._map_revisions_for_chat(odysseus_session)
            # A complete candidate binding can be committed before its map
            # replace.  Only async admission can confirm destructive cleanup
            # of that exact candidate before repairing the old pair.
            if (
                binding.get("engineSessionID") != mimo_session
                and mimo_session in (binding.get("engineAliases") or [])
                and binding.get("mappingRevision") == mapping_revision + 1
                and isinstance(binding.get("mapRevision"), int)
                and not isinstance(binding.get("mapRevision"), bool)
                and 0 <= binding.get("mapRevision") <= map_revision + 1
            ):
                raise RuntimeError("mapped managed session has an unadopted candidate binding")
            if binding.get("owner") != self._owner or binding.get("stableChatID") != odysseus_session or binding.get("engineSessionID") != mimo_session or isinstance(binding.get("mapRevision"), bool) or not isinstance(binding.get("mapRevision"), int) or binding.get("mapRevision") < 0 or binding.get("mapRevision") > map_revision or binding.get("mappingRevision") != mapping_revision or binding.get("transition") is not None:
                raise RuntimeError("mapped managed session binding is stale")
            restored = str(binding.get("physicalCwd") or "").strip()
            if restored and os.path.isabs(restored) and str(Path(restored).resolve(strict=False)) == restored:
                return restored
            raise RuntimeError("mapped managed session has no authoritative physical cwd")
        current = str(self._session_context.get(mimo_session, {}).get("workspace") or "").strip()
        if current:
            return current
        return self._cwd

    def mapped_sessions(self, owner: Optional[str] = None) -> dict[str, str]:
        if owner is not None and owner != self._owner:
            return {}
        self._session_map = self._durable_session_map.flat_current()
        return dict(self._session_map)

    def forget_session(self, odysseus_session: str) -> None:
        # The local map identifies the engine this bridge owns.  A stale bridge
        # must verify that exact candidate against durable authority; it must
        # never adopt the fresh winner merely because it is the latest row.
        expected_engine = self._session_map.get(odysseus_session)
        if expected_engine is None:
            expected_engine = odysseus_session
        entry = self._durable_session_map.lookup(odysseus_session)
        if entry is None:
            from src.openclank.transcript_projection import (
                delete_managed_binding,
                delete_projection,
                get_managed_binding,
            )
            try:
                orphan_binding = get_managed_binding(odysseus_session, owner=self._owner)
            except KeyError:
                return
            except Exception as exc:
                self._fence_admission(odysseus_session, "managed binding read during orphan forget")
                raise RuntimeError("managed binding authority could not be read during orphan forget") from exc
            if not orphan_binding:
                return
            try:
                delete_managed_binding(
                    odysseus_session,
                    owner=self._owner,
                    expected_workspace_revision=orphan_binding.get("workspaceRevision"),
                    expected_engine_session_id=orphan_binding.get("engineSessionID"),
                    expected_map_revision=orphan_binding.get("mapRevision"),
                    expected_mapping_revision=orphan_binding.get("mappingRevision"),
                )
                delete_projection(odysseus_session, owner=self._owner)
            except KeyError:
                return
            except Exception as exc:
                self._fence_admission(odysseus_session, "orphan projection cleanup during forget")
                raise RuntimeError("orphan managed authority cleanup was not confirmed") from exc
            return
        if entry.get("current") != expected_engine:
            self._session_map = self._durable_session_map.flat_current()
            return
        mimo_session = expected_engine
        map_revision, mapping_revision = self._durable_session_map.revisions(odysseus_session)
        from src.openclank.transcript_projection import (
            delete_managed_binding,
            delete_projection,
            get_managed_binding,
            save_managed_binding,
        )

        try:
            previous_binding = get_managed_binding(odysseus_session, owner=self._owner)
        except KeyError:
            previous_binding = None
        except Exception as exc:
            self._fence_admission(odysseus_session, "managed binding read during forget")
            raise RuntimeError("managed binding authority could not be read during forget") from exc
        if previous_binding:
            try:
                delete_managed_binding(
                    odysseus_session,
                    owner=self._owner,
                    expected_workspace_revision=previous_binding.get("workspaceRevision"),
                    expected_engine_session_id=mimo_session,
                    expected_map_revision=previous_binding.get("mapRevision"),
                    expected_mapping_revision=previous_binding.get("mappingRevision"),
                )
            except Exception as exc:
                self._fence_admission(odysseus_session, "managed binding deletion during forget")
                raise RuntimeError("managed binding deletion was not confirmed") from exc
        try:
            self._durable_session_map.forget(
                odysseus_session,
                expected_map_revision=map_revision,
                expected_current=mimo_session,
                expected_mapping_revision=mapping_revision,
            )
        except Exception as exc:
            if previous_binding:
                try:
                    save_managed_binding(
                        odysseus_session,
                        previous_binding,
                        owner=self._owner,
                        expected_workspace_revision=0,
                        expected_engine_session_id=None,
                        expected_map_revision=0,
                        expected_mapping_revision=0,
                    )
                except Exception as restore_exc:
                    self._fence_admission(odysseus_session, "managed binding restore after forget conflict")
                    raise RuntimeError("managed forget compensation was not confirmed") from restore_exc
            raise
        self._session_map = self._durable_session_map.flat_current()
        self._session_models.pop(mimo_session, None)
        self._session_state.pop(mimo_session, None)
        self._session_context.pop(mimo_session, None)
        self._turns.pop(mimo_session, None)
        self._queues.pop(mimo_session, None)
        try:
            delete_projection(odysseus_session, owner=self._owner)
        except KeyError:
            pass
        except Exception as exc:
            self._fence_admission(odysseus_session, "projection deletion during forget")
            raise RuntimeError("managed projection deletion was not confirmed") from exc

    async def _maybe_inject_digest(
        self,
        messages: list,
        *,
        owner: Optional[str],
        incognito: bool,
        memory_read_allowed: bool = True,
    ) -> list:
        """Memory injection for one mimo turn: returns (messages, trusted_block).

        The untrusted index card is prepended to turns that don't carry one
        (Open Clank-prefaced turns already have it; the sentinel keeps it
        single — this covers resume flows and any future mimo-first path).
        The TRUSTED guidance block (T6) is returned to the caller so it can
        ride envelope.system_prompt — true system tier — instead of being
        demoted to synthetic prompt text. If the Open Clank preface already
        produced a trusted block, that exact text is reused (no second
        digest fetch, no drift). Fail-open: no digest, no blocks."""
        if incognito or not memory_read_allowed or self._memory_provider is None:
            return messages, ""
        if not hasattr(self._memory_provider, "digest"):
            return messages, ""
        from src.memory_digest import DIGEST_SENTINEL, TRUST_SENTINEL

        existing_trusted = next(
            (
                _content_text(message.get("content"))
                for message in messages
                if TRUST_SENTINEL in _content_text(message.get("content"))
            ),
            "",
        )
        for message in messages:
            if DIGEST_SENTINEL in _content_text(message.get("content")):
                return messages, existing_trusted
        try:
            from src.memory_digest import DIGEST_FETCH_TIMEOUT_SECONDS

            digest = await asyncio.wait_for(
                self._memory_provider.digest(owner=owner or self._owner),
                timeout=DIGEST_FETCH_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("bridge memory digest unavailable, block dropped this turn: %s", exc)
            return messages, existing_trusted
        trusted_block, card = _split_memory_digest(digest, owner or self._owner)
        if existing_trusted:
            trusted_block = existing_trusted
        if not card:
            return messages, trusted_block
        from src.prompt_security import untrusted_context_message

        out = list(messages)
        insert_at = next(
            (index for index in range(len(out) - 1, -1, -1) if out[index].get("role") == "user"),
            len(out),
        )
        out.insert(insert_at, untrusted_context_message("memory bank index", card))
        return out, trusted_block

    async def run_turn(
        self,
        odysseus_session: str,
        messages: list,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
        owner: Optional[str] = None,
        turn_envelope: Optional[dict] = None,
    ) -> AsyncGenerator[str, None]:
        """Run a prompt turn via ACP, yielding the canonical Agent SSE contract.

        cwd: per-chat workspace override (else the bridge's global default).
        """
        prior_mimo_session = self._session_map.get(odysseus_session)
        prior_context = self._session_context.get(prior_mimo_session or "", {})
        mapped_workspace = self.mapped_session_workspace(odysseus_session)
        effective_cwd = str(
            (prior_context.get("workspace") or prior_context.get("cwd") or mapped_workspace)
            if prior_mimo_session is not None
            else (cwd or mapped_workspace)
        ).strip()
        envelope = json.loads(json.dumps(turn_envelope or {}, default=str))
        # These are host-authenticated routing fields. Never infer them from
        # the provider session id or accept model/user payload identity.
        envelope["chat_id"] = odysseus_session
        if prior_context.get("workspace_id"):
            envelope["workspace_id"] = prior_context["workspace_id"]
        if prior_context.get("goal_id") and not envelope.get("goal_id"):
            envelope["goal_id"] = prior_context["goal_id"]
        authority_workspace_id = str(
            envelope.get("authority_workspace_id") or ""
        ).strip()
        if authority_workspace_id and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", authority_workspace_id
        ):
            yield f"event: error\ndata: {json.dumps({'code': 'INVALID_AUTHORITY_WORKSPACE', 'error': 'The Workspace authority identity is invalid', 'status': 409, 'retryable': False})}\n\n"
            yield "data: [DONE]\n\n"
            return
        if authority_workspace_id:
            envelope["workspace_id"] = authority_workspace_id
        copal_workspace = str(envelope.get("copal_workspace") or "default").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", copal_workspace):
            yield f"event: error\ndata: {json.dumps({'code': 'INVALID_COPAL_WORKSPACE', 'error': 'Copal workspace is invalid', 'status': 400, 'retryable': False})}\n\n"
            yield "data: [DONE]\n\n"
            return
        incognito = bool(envelope.get("incognito"))
        auxiliary = envelope.get("lane") == "auxiliary"
        memory_read_allowed = bool(envelope.get("memory_read_allowed", True))
        extra_mcp_servers, mcp_disabled = odysseus_mcp_descriptors(
            is_admin=bool(envelope.get("is_admin")) and not incognito
        )
        envelope.setdefault("disabled_tools", []).extend(mcp_disabled)
        if incognito or auxiliary:
            mimo_session = await self.open_session(
                cwd=effective_cwd,
                owner=owner,
                odysseus_session=odysseus_session,
                with_agent_tools=not auxiliary,
                with_memory=False,
                copal_workspace=copal_workspace,
                authority_workspace_id=authority_workspace_id,
            )
            self._session_context[mimo_session] = {
                "odysseus_session_id": odysseus_session,
                "owner": owner if owner is not None else self._owner,
                "workspace": effective_cwd,
                "cwd": effective_cwd,
                "physical_cwd": effective_cwd,
                "file_policy_workspace": effective_cwd,
                "memory_workspace": chat_workspace(),
                "copal_workspace": copal_workspace,
                "authority_workspace_id": authority_workspace_id,
                "workspace_id": authority_workspace_id,
                "incognito": True,
                "auxiliary": auxiliary,
                "is_admin": bool(envelope.get("is_admin")),
                "interaction_policy": envelope.get("interaction_policy", "interactive"),
            }
        else:
            mimo_session = await self.ensure_session(
                odysseus_session,
                cwd=effective_cwd,
                owner=owner,
                extra_mcp_servers=extra_mcp_servers,
                with_memory=memory_read_allowed,
                copal_workspace=copal_workspace,
                authority_workspace_id=authority_workspace_id,
            )
            existing_context = self._session_context.get(mimo_session, {})
            self._session_context.setdefault(mimo_session, {}).update({
                "workspace": effective_cwd,
                "cwd": effective_cwd,
                "physical_cwd": effective_cwd,
                "file_policy_workspace": self._session_context.get(mimo_session, {}).get(
                    "file_policy_workspace", effective_cwd
                ),
                "authority_workspace_id": authority_workspace_id,
                "workspace_id": authority_workspace_id or existing_context.get("workspace_id"),
                "goal_id": envelope.get("goal_id") or existing_context.get("goal_id"),
                "incognito": False,
                "is_admin": bool(envelope.get("is_admin")),
                "interaction_policy": envelope.get("interaction_policy", "interactive"),
                "copal_workspace": copal_workspace,
            })
        metadata_root = _message_root_operation_id(messages)
        supplied_root = str(envelope.get("root_operation_id") or "").strip()
        if metadata_root and supplied_root and metadata_root != supplied_root:
            yield f'event: error\ndata: {json.dumps({"code": "ROOT_OPERATION_MISMATCH", "error": "The persisted turn operation identity did not match the execution envelope", "status": 409, "retryable": False})}\n\n'
            yield "data: [DONE]\n\n"
            return
        turn_id = metadata_root or supplied_root or _turn_source_id(messages)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}", turn_id):
            yield f'event: error\ndata: {json.dumps({"code": "INVALID_ROOT_OPERATION", "error": "The turn operation identity is invalid", "status": 409, "retryable": False})}\n\n'
            yield "data: [DONE]\n\n"
            return
        envelope["root_operation_id"] = turn_id
        envelope["root_turn_id"] = turn_id
        # Host-side tool callbacks resolve this nonsecret context rather than
        # inventing an operation identity from the long-lived session ID.
        self._session_context.setdefault(mimo_session, {}).update({
            "root_operation_id": turn_id,
            "provider_grant_id": (
                str(envelope.get("provider_grant_id") or "").strip() or None
            ),
        })
        if not incognito and not auxiliary and not turn_id.startswith("turn-"):
            from src.agent_actor_accounting import begin_agent_turn

            if not begin_agent_turn(turn_id, mimo_session):
                yield f'data: {json.dumps({"type": "actor_accounting_failure", "data": {"state": "unavailable", "root_turn_id": turn_id}})}\n\n'
                yield f'event: error\ndata: {json.dumps({"error": "Agent actor accounting could not attach to the persisted turn", "status": 500})}\n\n'
                yield "data: [DONE]\n\n"
                return
        control_state = self._session_state.get(mimo_session, {})
        if not auxiliary and control_state is not None:
            control_state["last_root_operation_id"] = turn_id
            self._persist_session_state(mimo_session)
        try:
            if auxiliary:
                raise KeyError("auxiliary turns have no transcript projection")
            from src.openclank.transcript_projection import canonical_snapshot, record_projection

            snapshot = canonical_snapshot(
                odysseus_session,
                owner=owner if owner is not None else self._owner,
            )
            record_projection(
                snapshot,
                mimo_session_id=mimo_session,
                workspace=effective_cwd,
                endpoint_url=MANAGED_ENGINE_PUBLIC_URL,
                model=model or "openclank-engine",
                turn_id=turn_id,
                mode_config_revision=int(control_state.get("revision") or 0),
            )
            envelope["transcript_revision"] = snapshot.revision
        except (KeyError, ValueError):
            # Bounded auxiliary calls deliberately have no canonical chat row.
            pass
        state = _TurnState(envelope.get("max_tool_calls") or 0)
        self._turns[mimo_session] = state

        desired = control_state.get("desired", {})
        # The Open Clank interaction mode is the authority for this turn.
        # Persisted ACP state is only a negotiated cache and must never let a
        # stale provider-native mode override a newly captured Chat/Plan/Agent
        # choice.  Chat and Agent both use ACP's internal build mode.
        desired_mode = str(
            envelope.get("provider_mode")
            or ("plan" if envelope.get("mode") == "plan" else "build")
        ).strip().lower()
        if desired_mode not in {"build", "plan"}:
            desired_mode = "build"
        selected_runtime_model = str(desired.get("model") or model or "")
        try:
            await self.set_config_option(
                odysseus_session,
                "mode",
                desired_mode,
                cwd=effective_cwd,
                owner=owner,
            )
        except (ValueError, RPCError) as exc:
            logger.warning("Open Clank agent mode negotiation failed (%s): %s", desired_mode, exc)
            yield f'data: {json.dumps({"type": "config_error", "config": "mode", "requested": desired_mode, "error": str(exc)})}\n\n'
            yield f'event: error\ndata: {json.dumps({"error": f"Open Clank agent rejected mode {desired_mode!r}; the prompt was not sent", "status": 409})}\n\n'
            yield "data: [DONE]\n\n"
            return

        # Tell mimo which model to use. The thesius model name (e.g. "deepseek-v4-pro")
        # may not match mimo's provider/model format ("deepseek/deepseek-v4-pro").
        # Match against the available models from the session handshake; if no match,
        # pass the raw thesius name and let mimo's parseModelSelection try.
        if model:
            try:
                model_id = self._match_model(mimo_session, model)
                if _debug_enabled():
                    logger.info(
                        "[debug] model match: thesius=%s → mimo=%s (available: %d models)",
                        model, model_id, len(self._session_models.get(mimo_session, [])),
                    )
                result = await self._client.set_session_config_option(
                    mimo_session, "model", model_id
                )
                selected_runtime_model = model_id
                self._capture_handshake(mimo_session, result)
                self._session_state[mimo_session].setdefault("desired", {})[
                    "model"
                ] = model_id
                self._persist_session_state(mimo_session)
                if _debug_enabled():
                    logger.info("[debug] set_session_config_option OK (model): %s", model_id)
            except Exception as e:
                logger.warning("set_session_config_option failed (model=%s): %s", model, e)
                if _debug_enabled():
                    logger.info("[debug] available models: %s", self._session_models.get(mimo_session, []))
                available = [
                    item.get("modelId")
                    for item in self._session_models.get(mimo_session, [])
                    if item.get("modelId")
                ]
                yield f'data: {json.dumps({"type": "config_error", "config": "model", "requested": model, "available": available, "error": str(e)})}\n\n'
                yield f'event: error\ndata: {json.dumps({"error": f"Open Clank agent rejected model {model!r}; the prompt was not sent", "status": 409})}\n\n'
                yield "data: [DONE]\n\n"
                return

        # Create a queue for this session's notifications
        q: asyncio.Queue = asyncio.Queue()
        self._queues[mimo_session] = q

        # Build the prompt parts from odysseus messages
        messages, trusted_block = await self._maybe_inject_digest(
            messages,
            owner=owner,
            incognito=incognito or auxiliary,
            memory_read_allowed=memory_read_allowed,
        )
        if trusted_block:
            # T6: endorsed guidance rides the persona seam to TRUE system
            # tier (mimo applies envelope.system_prompt as PromptInput.system).
            # The demoted in-message copy is skipped in _build_prompt_parts.
            base_system = str(envelope.get("system_prompt") or "").rstrip()
            envelope["system_prompt"] = (
                f"{base_system}\n\n{trusted_block}" if base_system else trusted_block
            )
        prompt_parts = _build_prompt_parts(
            messages,
            turn_id=turn_id,
            workspace=effective_cwd,
            authoritative_system=str(envelope.get("system_prompt") or ""),
        )
        prompt_meta = {"odysseus": envelope}
        prompt_meta["odysseus"]["tools"] = _mimo_tool_policy(envelope)
        if self._managed_provider_context is not None:
            try:
                route_context = await self._managed_provider_context(
                    selected_runtime_model,
                    grant_id=(str(envelope.get("provider_grant_id") or "").strip() or None),
                )
                managed_wire = route_context.to_wire(
                    root_operation_id=turn_id,
                )
                for source, target in (
                    ("preferred_provider_account_id", "preferredAccountID"),
                    ("inherited_provider_account_id", "inheritedAccountID"),
                ):
                    value = str(envelope.get(source) or "").strip()
                    if value:
                        managed_wire[target] = value
                prompt_meta["openclankProvider"] = managed_wire
            except Exception as exc:
                logger.warning(
                    "managed provider route resolution rejected model %r: %s",
                    selected_runtime_model,
                    exc,
                )
                yield f'event: error\ndata: {json.dumps({"code": "MANAGED_PROVIDER_ROUTE_UNAVAILABLE", "error": str(exc), "status": 409, "retryable": False})}\n\n'
                yield "data: [DONE]\n\n"
                return

        # Fire the prompt request (blocks until stopReason, notifications arrive concurrently)
        prompt_task = asyncio.ensure_future(
            self._client.prompt(mimo_session, prompt_parts, metadata=prompt_meta)
        )

        try:
            # Consume notifications until the prompt returns
            done = False
            while not done:
                # Check if prompt is already done
                if prompt_task.done():
                    done = True
                    # Drain any remaining queued updates
                    while not q.empty():
                        update = q.get_nowait()
                        for sse in self._process_update(mimo_session, update, state):
                            yield sse
                    break

                try:
                    update = await asyncio.wait_for(q.get(), timeout=1.0)
                    for sse in self._process_update(mimo_session, update, state):
                        yield sse
                    if (
                        state.max_tool_calls
                        and len(state.tool_calls_seen) >= state.max_tool_calls
                        and not state.policy_stopped
                    ):
                        state.policy_stopped = True
                        await self._client.cancel(mimo_session)
                        yield f'data: {json.dumps({"type": "rounds_exhausted", "reason": "max_tool_calls", "limit": state.max_tool_calls})}\n\n'
                except asyncio.TimeoutError:
                    # No update within 1s — check prompt task and continue
                    continue

            # Get the prompt result
            try:
                result = prompt_task.result()
            except Exception as e:
                logger.error("ACP prompt failed: %s", e)
                # A forwarded session.error already put the real provider
                # message on the stream — don't repeat it as a second card.
                if not state.error_streamed:
                    yield f'event: error\ndata: {json.dumps({"error": str(e), "status": 500})}\n\n'
                yield "data: [DONE]\n\n"
                return

            state.stop_reason = result.get("stopReason", "end_turn")
            state.usage = result.get("usage")

            # Emit final metrics
            elapsed = time.time() - state.turn_start
            metrics = {
                "response_time": round(elapsed, 2),
                "model": model or "openclank-engine",
                "stop_reason": state.stop_reason,
                "root_turn_id": turn_id,
                **state.metrics,
            }
            if state.usage:
                metrics["input_tokens"] = state.usage.get("inputTokens", 0)
                metrics["output_tokens"] = state.usage.get("outputTokens", 0)
                metrics["total_tokens"] = state.usage.get("totalTokens", 0)
                metrics["thinking_tokens"] = state.usage.get("thoughtTokens", 0)
                metrics["cache_read_tokens"] = state.usage.get("cachedReadTokens", 0)
                metrics["cache_write_tokens"] = state.usage.get("cachedWriteTokens", 0)
                metrics["usage_source"] = "reported"
            else:
                metrics["usage_source"] = "estimated"

            yield f'data: {json.dumps({"type": "metrics", "data": metrics})}\n\n'

            # Non-end_turn stop reasons get a notice
            if state.stop_reason != "end_turn":
                notice = _stop_reason_notice(state.stop_reason)
                if notice:
                    yield f'data: {json.dumps({"delta": notice})}\n\n'

            yield "data: [DONE]\n\n"

        except (asyncio.CancelledError, GeneratorExit):
            # Client disconnected — cancel the turn
            try:
                await self._client.cancel(mimo_session)
            except Exception:
                pass
            raise
        finally:
            self._turns.pop(mimo_session, None)
            self._queues.pop(mimo_session, None)
            if not prompt_task.done():
                prompt_task.cancel()
                try:
                    await prompt_task
                except (asyncio.CancelledError, Exception):
                    pass
            if incognito:
                self._session_context.pop(mimo_session, None)
                self._session_state.pop(mimo_session, None)
                self._session_models.pop(mimo_session, None)
                if self._delete_session_callback:
                    try:
                        await self._delete_session_callback(
                            odysseus_session,
                            mimo_session_id=mimo_session,
                        )
                    except Exception as exc:
                        logger.warning("failed to delete incognito Open Clank agent session %s: %s", mimo_session, exc)

    def _persist_plan_update(self, mimo_session_id: str, payload: dict) -> dict:
        state = self._session_state.setdefault(mimo_session_id, {})
        previous = state.get("plan_state") or {}
        combined = {
            key: value
            for key, value in previous.items()
            if key not in ("digest", "revision", "approved_revision", "approved_digest")
        }
        combined.update(payload)
        material = json.dumps(combined, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        revision = int(previous.get("revision") or 0)
        if previous.get("digest") != digest:
            revision += 1
        plan_state = {
            **combined,
            "digest": digest,
            "revision": revision,
            "approved_revision": (
                previous.get("approved_revision")
                if previous.get("digest") == digest
                else None
            ),
            "approved_digest": (
                previous.get("approved_digest")
                if previous.get("digest") == digest
                else None
            ),
        }
        state["plan_state"] = plan_state
        self._persist_session_state(mimo_session_id)
        return plan_state

    def _process_update(self, mimo_session_id: str, update: dict, state: _TurnState) -> list:
        """Exhaustively convert one ACP update into SSE or internal state."""
        update_type = update.get("sessionUpdate", "")
        sses = []

        if update_type in ("agent_message_chunk", "agent_thought_chunk"):
            content = update.get("content", {})
            text = content.get("text", "") if isinstance(content, dict) else ""
            if text:
                thinking = update_type == "agent_thought_chunk"
                if not thinking:
                    state.full_response += text
                sses.append(f'data: {json.dumps({"delta": text, **({"thinking": True} if thinking else {})})}\n\n')

        elif update_type == "user_message_chunk":
            sses.append(f'data: {json.dumps({"type": "user_replay", "data": _sanitize_event_value(update)})}\n\n')

        elif update_type == "tool_call":
            tool_call_id = update.get("toolCallId", "")
            title = update.get("title") or "tool"
            if tool_call_id not in state.tool_calls_seen:
                state.tool_calls_seen.add(tool_call_id)
                state.tool_titles[tool_call_id] = title
                sses.append(f'data: {json.dumps({"type": "tool_start", "tool": title, "id": tool_call_id, "data": _sanitize_event_value(update)})}\n\n')

        elif update_type == "tool_call_update":
            tool_call_id = update.get("toolCallId", "")
            status = update.get("status", "")
            # Updates often omit the title; fall back to the one the
            # initial tool_call carried so the finished card keeps its name.
            title = update.get("title") or state.tool_titles.get(tool_call_id) or "tool"
            if tool_call_id and tool_call_id not in state.tool_calls_seen:
                # Update arrived without (or before) its tool_call — open a
                # card anyway so the completion below has something to close.
                state.tool_calls_seen.add(tool_call_id)
                state.tool_titles[tool_call_id] = title
                sses.append(f'data: {json.dumps({"type": "tool_start", "tool": title, "id": tool_call_id, "data": _sanitize_event_value(update)})}\n\n')
            if status not in ("pending", "in_progress", "completed", "failed"):
                sses.append(f'data: {json.dumps({"type": "protocol_error", "data": {"message": "Unknown ACP tool status", "status": status}})}\n\n')
                return sses
            content_arr = update.get("content") if isinstance(update.get("content"), list) else []
            output_text = ""
            for block in content_arr:
                if isinstance(block, dict) and block.get("type") == "content":
                    inner = block.get("content", {})
                    if isinstance(inner, dict):
                        output_text += str(inner.get("text") or "")
                if isinstance(block, dict) and block.get("type") == "diff":
                    path = str(block.get("path") or "")
                    if "/plans/" in path.replace("\\", "/"):
                        plan = self._persist_plan_update(mimo_session_id, {
                            "plan": str(block.get("newText") or ""),
                            "path": path,
                        })
                        sses.append(f'data: {json.dumps({"type": "plan_update", "data": plan})}\n\n')
            raw_output = update.get("rawOutput", {})
            if isinstance(raw_output, dict) and not output_text:
                output_text = str(raw_output.get("output") or raw_output.get("error") or "")
            event_type = "tool_progress" if status in ("pending", "in_progress") else "tool_output"
            event: dict = {"type": event_type, "tool": title, "id": tool_call_id, "status": status, "output": output_text, "data": _sanitize_event_value(update)}
            if event_type == "tool_output":
                # The UI derives ✓/✗ from exit_code (homegrown-loop shape);
                # ACP only reports status, so map it.
                event["exit_code"] = 0 if status == "completed" else 1
            sses.append(f'data: {json.dumps(event)}\n\n')

        elif update_type == "usage_update":
            usage = _sanitize_event_value(update)
            state.metrics.update({key: value for key, value in usage.items() if key != "sessionUpdate"})
            state.metrics["usage_source"] = "reported"
            sses.append(f'data: {json.dumps({"type": "usage", "data": usage})}\n\n')

        elif update_type == "plan":
            plan = self._persist_plan_update(mimo_session_id, {
                "todos": _sanitize_event_value(update.get("entries") or update.get("plan") or []),
            })
            sses.append(f'data: {json.dumps({"type": "plan_update", "data": plan})}\n\n')

        elif update_type == "available_commands_update":
            sses.append(f'data: {json.dumps({"type": "commands_update", "data": _sanitize_event_value(update)})}\n\n')

        elif update_type == "_openclank_session_cwd":
            # The notification handler commits this per-chat workspace before
            # the update reaches the turn queue. Surface it for the current
            # client turn without ever changing the process cwd.
            sses.append(f'data: {json.dumps({"type": "session_cwd", "data": _sanitize_event_value(update)})}\n\n')

        elif update_type == "current_mode_update":
            sses.append(f'data: {json.dumps({"type": "mode_update", "data": _sanitize_event_value(update)})}\n\n')

        elif update_type == "config_option_update":
            sses.append(f'data: {json.dumps({"type": "config_update", "data": _sanitize_event_value(update)})}\n\n')

        elif update_type == "session_info_update":
            sses.append(f'data: {json.dumps({"type": "session_info", "data": _sanitize_event_value(update)})}\n\n')

        elif update_type == "_odysseus_error":
            # mimo forwards session.error bus events (provider/LLM failures
            # that its server otherwise resolves as a clean 200 prompt).
            message = str(update.get("message") or update.get("name") or "Model provider error")
            state.error_streamed = True
            sses.append(f'event: error\ndata: {json.dumps({"error": message, "status": 502})}\n\n')

        elif update_type == "_odysseus_retry":
            data = _sanitize_event_value(update)
            data.pop("sessionUpdate", None)
            sses.append(f'data: {json.dumps({"type": "retry_notice", "data": data})}\n\n')

        elif update_type == "_odysseus_actor_snapshot":
            from src.agent_actor_accounting import apply_actor_feed

            aggregate = apply_actor_feed(_sanitize_event_value(update))
            sses.append(f'data: {json.dumps({"type": "actor_accounting", "data": aggregate})}\n\n')

        elif update_type == "_permission_request":
            # C1: synthetic update injected by _surface_permission — ride the
            # turn's SSE stream so the UI can render an inline approval card.
            req = update.get("request")
            if req is not None:
                detail = req.raw_input if isinstance(req.raw_input, dict) else {}
                payload = {
                    "request_id": req.request_id,
                    "session_id": req.odysseus_session_id,
                    "turn_id": req.turn_id,
                    "revision": req.revision,
                    "permission_type": req.title,
                    "detail": detail,
                    "options": req.options,
                    "always_pattern": derive_pattern(detail),
                }
                sses.append(f'data: {json.dumps({"type": "permission_request", "data": payload})}\n\n')

        elif update_type == "_question_request":
            req = update.get("request")
            if req is not None:
                sses.append(f'data: {json.dumps({"type": "ask_user", "data": req.payload()})}\n\n')

        else:
            logger.error("unknown ACP session update: %s", update_type)
            sses.append(f'data: {json.dumps({"type": "protocol_error", "data": {"message": "Unknown ACP session update", "update_type": str(update_type)[:128]}})}\n\n')

        return sses


def _sanitize_event_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated]"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:200]:
            name = str(key)[:128]
            if any(token in name.lower() for token in ("password", "secret", "token", "api_key", "authorization")):
                result[name] = "[redacted]"
            else:
                result[name] = _sanitize_event_value(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [_sanitize_event_value(item, depth=depth + 1) for item in value[:500]]
    if isinstance(value, str):
        return value[:131_072]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:4_096]


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or item.get("content") or "")
            for item in content
            if isinstance(item, dict) and (item.get("text") or item.get("content"))
        )
    return str(content or "")


def _message_root_operation_id(messages: list) -> str:
    """Return the server-persisted root identity on the latest user turn."""

    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        metadata = message.get("metadata") or {}
        if metadata.get("root_operation_id"):
            return str(metadata["root_operation_id"])
        if metadata.get("_db_id"):
            return str(metadata["_db_id"])
    return ""


def _turn_source_id(messages: list) -> str:
    persisted = _message_root_operation_id(messages)
    if persisted:
        return persisted
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        material = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str)
        return f"turn-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"
    return f"turn-{hashlib.sha256(json.dumps(messages, default=str).encode('utf-8')).hexdigest()[:24]}"


def _safe_resource_uri(uri: str, workspace: str) -> bool:
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme in ("http", "https", "attachment"):
        return True
    if parsed.scheme != "file" or not workspace:
        return False
    try:
        Path(urllib.parse.unquote(parsed.path)).resolve().relative_to(Path(workspace).resolve())
        return True
    except (OSError, ValueError):
        return False


def _path_within(path: str, root: str) -> bool:
    try:
        Path(path).expanduser().resolve().relative_to(Path(root).expanduser().resolve())
        return True
    except (OSError, ValueError):
        return False


def _data_uri(value: str) -> tuple[str, str] | None:
    if not value.startswith("data:") or "," not in value:
        return None
    header, payload = value[5:].split(",", 1)
    fields = header.split(";")
    mime = fields[0] or "application/octet-stream"
    if "base64" in fields[1:]:
        try:
            base64.b64decode(payload, validate=True)
        except (ValueError, binascii.Error):
            return None
        return mime, payload
    return mime, base64.b64encode(urllib.parse.unquote_to_bytes(payload)).decode("ascii")


def _content_parts(
    content: Any,
    *,
    annotations: Optional[dict] = None,
    workspace: str = "",
    attachment_names: Optional[list[str]] = None,
) -> list[dict]:
    if isinstance(content, str):
        part = {"type": "text", "text": content}
        if annotations:
            part["annotations"] = annotations
        return [part] if content else []
    if not isinstance(content, list):
        return []

    result: list[dict] = []
    names = iter(attachment_names or [])
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            part = {"type": "text", "text": block["text"]}
            if annotations:
                part["annotations"] = annotations
            result.append(part)
            continue
        if kind in ("image", "image_url"):
            image = block.get("image_url") if kind == "image_url" else block
            uri = image.get("url") if isinstance(image, dict) else block.get("uri")
            data = _data_uri(uri or "")
            name = next(names, "image")
            if data:
                result.append({
                    "type": "image",
                    "data": data[1],
                    "mimeType": data[0],
                    "uri": f"attachment://{urllib.parse.quote(name)}",
                })
            elif isinstance(uri, str) and _safe_resource_uri(uri, workspace):
                result.append({
                    "type": "image",
                    "uri": uri,
                    "mimeType": block.get("mimeType") or "image/*",
                })
            continue
        if kind in ("audio", "input_audio"):
            audio = block.get("audio") or block.get("input_audio") or {}
            uri = audio.get("url", "") if isinstance(audio, dict) else ""
            data = _data_uri(uri)
            if not data and isinstance(audio, dict) and audio.get("data"):
                data = (f"audio/{audio.get('format') or 'mpeg'}", audio["data"])
            if data:
                name = next(names, "audio")
                result.append({
                    "type": "resource",
                    "resource": {
                        "uri": f"attachment://{urllib.parse.quote(name)}",
                        "mimeType": data[0],
                        "blob": data[1],
                    },
                })
            continue
        if kind == "resource_link":
            uri = str(block.get("uri") or "")
            if _safe_resource_uri(uri, workspace):
                result.append({
                    "type": "resource_link",
                    "uri": uri,
                    "name": str(block.get("name") or "resource"),
                    "mimeType": str(block.get("mimeType") or "application/octet-stream"),
                    "size": block.get("size"),
                })
            continue
        if kind == "resource" and isinstance(block.get("resource"), dict):
            resource = block["resource"]
            uri = str(resource.get("uri") or "")
            if _safe_resource_uri(uri, workspace) and (
                isinstance(resource.get("text"), str)
                or isinstance(resource.get("blob"), str)
            ):
                result.append({"type": "resource", "resource": dict(resource)})
    return result


def _build_prompt_parts(
    messages: list,
    *,
    turn_id: str = "",
    workspace: str = "",
    authoritative_system: str = "",
) -> list:
    """Compile canonical roles and structured content into valid ACP parts.

    authoritative_system: the persona/system prompt that crosses the seam as
    TRUE system authority (envelope → child PromptInput.system, identity
    ruling R1). Any context message carrying that exact text is skipped here
    so the persona never ALSO arrives demoted to synthetic prompt text.
    """
    if not messages:
        return []
    from src.memory_digest import TRUST_SENTINEL

    current_index = next(
        (index for index in range(len(messages) - 1, -1, -1) if messages[index].get("role") == "user"),
        len(messages) - 1,
    )
    parts: list[dict] = []
    annotations = {"audience": ["assistant"]}
    authority = (authoritative_system or "").strip()
    for index, message in enumerate(messages[:current_index]):
        metadata = message.get("metadata") or {}
        message_id = metadata.get("_db_id") or f"context-{index}"
        role = str(message.get("role") or "unknown").lower()
        text = _content_text(message.get("content"))
        if (
            authority
            and role == "system"
            and text.strip() == authority
        ):
            continue
        if role == "system" and TRUST_SENTINEL in text:
            # The endorsed-guidance block rides envelope.system_prompt as
            # true system authority; never ALSO send it demoted to
            # synthetic prompt text.
            continue
        parts.append({
            "type": "text",
            "text": f"[odysseus_context role={role} id={message_id}]",
            "annotations": annotations,
        })
        attachment_names = [
            str(item.get("name") or item.get("id") or "attachment")
            for item in metadata.get("attachments") or []
            if isinstance(item, dict)
        ]
        parts.extend(_content_parts(
            message.get("content"),
            annotations=annotations,
            workspace=workspace,
            attachment_names=attachment_names,
        ))

    current_message = messages[current_index]
    metadata = current_message.get("metadata") or {}
    attachment_names = [
        str(item.get("name") or item.get("id") or "attachment")
        for item in metadata.get("attachments") or []
        if isinstance(item, dict)
    ]
    current_parts = _content_parts(
        current_message.get("content"),
        workspace=workspace,
        attachment_names=attachment_names,
    )
    prefix = f"[turn_source_id={turn_id}]\n" if turn_id else ""
    first_text = next((part for part in current_parts if part.get("type") == "text"), None)
    if first_text is None:
        current_parts.insert(0, {"type": "text", "text": prefix.rstrip()})
    else:
        current = first_text["text"]
        if current.startswith("/mimo:"):
            current = "/" + current[len("/mimo:"):]
        first_text["text"] = prefix + current
    parts.extend(current_parts)
    return parts


_MIMO_TOOL_ALIASES = {
    "bash": {"bash", "shell"},
    "python": {"bash"},
    "read_file": {"read"},
    "write_file": {"write", "edit", "apply_patch", "patch"},
    "web_search": {"websearch", "web_search"},
    "web_fetch": {"webfetch", "web_fetch"},
    "manage_memory": {"memory"},
    "recall_memory": {"memory"},
}


def _mimo_tool_policy(envelope: dict) -> dict[str, bool]:
    if envelope.get("lane") == "auxiliary":
        return {"*": False}
    allowed = envelope.get("allowed_tools")
    policy: dict[str, bool] = {"frankenmemory_*": False}
    if allowed is not None:
        policy["*"] = False
    for raw_name in allowed or []:
        name = str(raw_name)
        normalized = name.replace("mcp__", "").replace(":", "_")
        if normalized.startswith("frankenmemory_"):
            continue
        for alias in _MIMO_TOOL_ALIASES.get(name, {normalized}):
            policy[alias] = True
        policy[f"lifetools_*_{normalized}"] = True
    for raw_name in envelope.get("disabled_tools") or []:
        name = str(raw_name)
        normalized = name.replace("mcp__", "").replace(":", "_")
        aliases = _MIMO_TOOL_ALIASES.get(name, {normalized})
        for alias in aliases:
            policy[alias] = False
        policy[f"lifetools_*_{normalized}"] = False
    for raw_name in envelope.get("forced_tools") or []:
        name = str(raw_name)
        normalized = name.replace("mcp__", "").replace(":", "_")
        if normalized.startswith("frankenmemory_"):
            continue
        if allowed is not None and name not in allowed:
            continue
        for alias in _MIMO_TOOL_ALIASES.get(name, {name}):
            policy.setdefault(alias, True)
    # Native OpenCode file/search/process tools are never the authority lane.
    # Server-minted brokered_file_tools selectively re-enable only the private
    # lifetools adapters after current AgentScope projection.
    from src.tool_security import SCOPED_FILE_TOOLS, STRICT_NATIVE_MIMO_TOOLS

    for native_name in STRICT_NATIVE_MIMO_TOOLS:
        policy[native_name] = False
    brokered = set(map(str, envelope.get("brokered_file_tools") or ())) & set(SCOPED_FILE_TOOLS)
    explicitly_disabled = set(map(str, envelope.get("disabled_tools") or ()))
    explicitly_allowed = None if allowed is None else set(map(str, allowed or ()))
    for public_name in SCOPED_FILE_TOOLS:
        enabled = (
            public_name in brokered
            and public_name not in explicitly_disabled
            and (explicitly_allowed is None or public_name in explicitly_allowed)
        )
        policy[f"lifetools_*_{public_name}"] = enabled
    policy["frankenmemory_*"] = False
    return policy


def _extract_user_text(messages: list) -> str:
    """Extract the last user message text from odysseus messages."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return _content_text(msg.get("content", ""))
    return ""


def _split_memory_digest(digest, owner) -> tuple:
    """Shared trusted/untrusted split for bridge-side injection.

    Untrusted framing comes from the untrusted_context_message wrapper at
    injection, not repeated here. Prefs fail CLOSED (broken prefs file =
    master off), matching ChatProcessor._trust_prefs. The tail names
    mimo's native memory tool — the one this lane actually holds (F4)."""
    from src.memory_digest import render_split

    try:
        from routes.prefs_routes import _load_for_user

        prefs = _load_for_user(owner) or {}
    except Exception:
        prefs = {}
    return render_split(digest, prefs, tool_hint="the memory tool")


def _stop_reason_notice(reason: str) -> Optional[str]:
    """Map non-end_turn stop reasons to user-visible text."""
    notices = {
        "max_tokens": "[Agent stopped: maximum token limit reached]",
        "max_turn_requests": "[Agent stopped: maximum turn requests reached]",
        "refusal": "[Agent stopped: model refused the request]",
        "cancelled": "[Agent stopped: turn was cancelled]",
    }
    return notices.get(reason)


# ---------------------------------------------------------------------------
# C4 — Real ACP permission handler
# ---------------------------------------------------------------------------

class PendingQuestion:
    __slots__ = (
        "request_id", "mimo_request_id", "mimo_session_id",
        "odysseus_session_id", "owner", "turn_id", "revision",
        "plan_revision", "questions", "_future",
    )

    def __init__(
        self,
        *,
        mimo_request_id: str,
        mimo_session_id: str,
        odysseus_session_id: str,
        owner: str,
        turn_id: str,
        revision: int,
        plan_revision: int,
        questions: list[dict],
    ) -> None:
        digest = hashlib.sha256(
            f"{mimo_session_id}\0{mimo_request_id}".encode("utf-8")
        ).hexdigest()[:16]
        self.request_id = f"question_{digest}"
        self.mimo_request_id = mimo_request_id
        self.mimo_session_id = mimo_session_id
        self.odysseus_session_id = odysseus_session_id
        self.owner = owner
        self.turn_id = turn_id
        self.revision = revision
        self.plan_revision = plan_revision
        self.questions = questions
        self._future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()

    def payload(self) -> dict:
        first = self.questions[0] if self.questions else {}
        return {
            "request_id": self.request_id,
            "session_id": self.odysseus_session_id,
            "turn_id": self.turn_id,
            "revision": self.revision,
            "plan_revision": self.plan_revision,
            "questions": self.questions,
            "question": str(first.get("question") or "Question"),
            "options": list(first.get("options") or []),
            "multi": bool(first.get("multiple")),
            "custom": first.get("custom") is not False,
        }

    async def wait(self) -> dict:
        return await self._future

    def resolve(self, result: dict) -> bool:
        if self._future.done():
            return False
        self._future.set_result(result)
        return True

    def result(self) -> dict:
        return self._future.result() if self._future.done() else {}


class QuestionHandler:
    def __init__(self) -> None:
        self.pending_requests: Dict[str, PendingQuestion] = {}
        self._context_resolver: Optional[Callable[[str], dict]] = None
        self._on_request: Optional[Callable[[PendingQuestion], Coroutine[Any, Any, None]]] = None
        self._on_resolved: Optional[Callable[[PendingQuestion], Coroutine[Any, Any, None]]] = None

    def set_context_resolver(self, resolver: Callable[[str], dict]) -> None:
        self._context_resolver = resolver

    def on_request(self, callback: Callable[[PendingQuestion], Coroutine[Any, Any, None]]) -> None:
        self._on_request = callback

    def on_resolved(self, callback: Callable[[PendingQuestion], Coroutine[Any, Any, None]]) -> None:
        self._on_resolved = callback

    @staticmethod
    def _questions(value: Any) -> list[dict]:
        if not isinstance(value, list) or not 1 <= len(value) <= 16:
            raise ValueError("Open Clank agent question must contain 1-16 prompts")
        result: list[dict] = []
        for raw in value:
            if not isinstance(raw, dict):
                raise ValueError("Open Clank agent question prompt must be an object")
            question = str(raw.get("question") or "").strip()[:4_096]
            if not question:
                raise ValueError("Open Clank agent question text is required")
            options = []
            for option in list(raw.get("options") or [])[:100]:
                if not isinstance(option, dict):
                    continue
                label = str(option.get("label") or "").strip()[:512]
                if label:
                    options.append({
                        "label": label,
                        "description": str(option.get("description") or "")[:2_048],
                    })
            result.append({
                "header": str(raw.get("header") or "")[:128],
                "question": question,
                "options": options,
                "multiple": bool(raw.get("multiple")),
                "custom": raw.get("custom") is not False,
                "key": str(raw.get("key") or "")[:128],
                "params": {
                    str(key)[:128]: str(value)[:2_048]
                    for key, value in (raw.get("params") or {}).items()
                } if isinstance(raw.get("params"), dict) else {},
            })
        return result

    async def handle(self, params: dict) -> dict:
        mimo_session = str(params.get("sessionId") or "")
        context = self._context_resolver(mimo_session) if self._context_resolver else {}
        odysseus_session = str(context.get("odysseus_session_id") or "")
        owner = str(context.get("owner") or "")
        incognito = bool(context.get("incognito"))
        if (
            not mimo_session
            or not odysseus_session
            or not owner
            or context.get("interaction_policy") == "fail_on_interaction"
        ):
            return {"rejected": True}
        # Permission mode "auto": questions never block the turn — the worker
        # gets the standard rejection and proceeds on its own judgment.
        try:
            from src.permission_mode import suppresses_questions

            if suppresses_questions(owner):
                logger.info("question suppressed by owner permission mode (auto)")
                return {"rejected": True}
        except Exception:
            pass
        projection = {}
        if not incognito:
            from src.openclank.transcript_projection import get_projection

            projection = get_projection(odysseus_session, owner=owner)
            if not projection or projection.get("mimo_session_id") != mimo_session:
                return {"rejected": True}
        req = PendingQuestion(
            mimo_request_id=str(params.get("requestId") or ""),
            mimo_session_id=mimo_session,
            odysseus_session_id=odysseus_session,
            owner=owner,
            turn_id=str(projection.get("active_turn_id") or ""),
            revision=int(projection.get("transcript_revision") or 0),
            plan_revision=int(context.get("plan_revision") or 0),
            questions=self._questions(params.get("questions")),
        )
        if req.request_id in self.pending_requests or self._on_request is None:
            return {"rejected": True}
        self.pending_requests[req.request_id] = req
        try:
            await self._on_request(req)
            return await req.wait()
        except Exception as exc:
            logger.warning("failed to surface Open Clank agent question: %s", exc)
            return {"rejected": True}
        finally:
            self.pending_requests.pop(req.request_id, None)
            if self._on_resolved:
                await self._on_resolved(req)

    def resolve(
        self,
        request_id: str,
        *,
        owner: str,
        session_id: str,
        answers: Optional[list[list[str]]] = None,
        rejected: bool = False,
    ) -> bool:
        req = self.pending_requests.get(request_id)
        if req is None or req.owner != owner or req.odysseus_session_id != session_id:
            return False
        context = self._context_resolver(req.mimo_session_id) if self._context_resolver else {}
        if not context.get("incognito"):
            from src.openclank.transcript_projection import get_projection

            projection = get_projection(session_id, owner=owner)
            if (
                not projection
                or projection.get("mimo_session_id") != req.mimo_session_id
                or projection.get("active_turn_id") != req.turn_id
                or int(projection.get("transcript_revision") or 0) != req.revision
                or int(context.get("plan_revision") or 0) != req.plan_revision
            ):
                return False
        if rejected:
            return req.resolve({"rejected": True})
        if not isinstance(answers, list) or len(answers) != len(req.questions):
            return False
        clean: list[list[str]] = []
        for index, answer in enumerate(answers):
            if not isinstance(answer, list) or not answer:
                return False
            values = [str(value).strip()[:2_048] for value in answer if str(value).strip()]
            if not values:
                return False
            question = req.questions[index]
            if not question["multiple"] and len(values) != 1:
                return False
            allowed = {item["label"] for item in question["options"]}
            if not question["custom"] and any(value not in allowed for value in values):
                return False
            clean.append(values)
        return req.resolve({"answers": clean})


class PermissionRequest:
    """A pending permission request surfaced to the UI."""

    __slots__ = (
        "request_id", "tool_call", "raw_input", "options", "title",
        "session_id", "odysseus_session_id", "owner", "workspace",
        "authority_workspace_id",
        "turn_id", "revision", "_future",
    )

    def __init__(
        self,
        request_id: str,
        tool_call: dict,
        raw_input: Any,
        options: list,
        title: str,
        session_id: str = "",
        odysseus_session_id: str = "",
        owner: str = "",
        workspace: str = "",
        authority_workspace_id: str = "",
        turn_id: str = "",
        revision: int = 0,
    ) -> None:
        self.request_id = request_id
        self.tool_call = tool_call
        self.raw_input = raw_input
        self.options = options
        self.title = title
        self.session_id = session_id
        self.odysseus_session_id = odysseus_session_id
        self.owner = owner
        self.workspace = workspace
        self.authority_workspace_id = authority_workspace_id
        self.turn_id = turn_id
        self.revision = revision
        self._future: asyncio.Future[str] = asyncio.get_event_loop().create_future()

    async def wait(self, timeout: Optional[float] = None) -> str:
        """Wait for the human's choice.

        C1 (e's ruling): no timeout by default — the prompt waits forever
        and the turn blocks until answered.
        """
        if timeout is None:
            return await self._future
        try:
            return await asyncio.wait_for(self._future, timeout=timeout)
        except asyncio.TimeoutError:
            return "reject"  # fail-safe on timeout

    def resolve(self, option_id: str) -> None:
        """Resolve the request with the human's choice."""
        if not self._future.done():
            self._future.set_result(option_id)


class PermissionHandler:
    """C4/C1: Real permission handler for ACP session/request_permission.

    Check order (e's rulings 2026-07-09): safe-dirs -> stored durable
    grants -> surface a prompt to the UI and wait forever. 'Always allow'
    answers persist a grant in the odysseus DB so they survive restarts
    (mimo's own permission memory resets per launch). Requests that cannot
    be surfaced to any UI fail safe to reject instead of hanging invisibly.

    Usage:
        handler = PermissionHandler(safe_dirs=[...], grant_store=GrantStore(db))
        # Pass handler.handle to register_client_callbacks or ACPBridge
        # When a permission request arrives, handler.pending_requests gets a new entry
        # Call handler.resolve(request_id, option_id) when the human responds
    """

    def __init__(self, safe_dirs: Optional[List[str]] = None, grant_store: Any = None) -> None:
        self.pending_requests: Dict[str, PermissionRequest] = {}
        self._on_request: Optional[Callable[[PermissionRequest], Coroutine[Any, Any, None]]] = None
        self._safe_dirs: List[str] = [os.path.expanduser(d) for d in (safe_dirs or [])]
        self._grant_store = grant_store
        self._context_resolver: Optional[Callable[[str], dict]] = None

    def set_context_resolver(self, resolver: Callable[[str], dict]) -> None:
        self._context_resolver = resolver

    def authorize_path(self, session_id: str, raw_path: str) -> bool:
        if not self._context_resolver or not session_id or not raw_path:
            return False
        context = self._context_resolver(session_id) or {}
        if context.get("incognito"):
            return False
        owner = str(context.get("owner") or "")
        if owner and not context.get("is_admin"):
            return False
        workspace = str(context.get("workspace") or "")
        if not workspace:
            return False
        try:
            Path(raw_path).expanduser().resolve().relative_to(
                Path(workspace).expanduser().resolve()
            )
            return True
        except (OSError, ValueError):
            return False

    def on_request(self, callback: Callable[[PermissionRequest], Coroutine[Any, Any, None]]) -> None:
        """Register a callback invoked when a new permission request arrives.

        The callback receives a PermissionRequest and should surface it to the UI.
        """
        self._on_request = callback

    @staticmethod
    def _approve(option_id: str = "always") -> dict:
        return {"outcome": {"outcome": "selected", "optionId": option_id}}

    async def handle(self, params: dict) -> dict:
        """Handle a session/request_permission call from mimo.

        This is the async function passed to register_client_callbacks.
        """
        tool_call = params.get("toolCall", {})
        title = tool_call.get("title", "unknown tool")
        raw_input = tool_call.get("rawInput", {})
        options = params.get("options", ["once", "chat", "workspace", "always", "reject"])
        session_id = params.get("sessionId", "")
        filepath = raw_input.get("filepath", "") if isinstance(raw_input, dict) else ""
        context = self._context_resolver(session_id) if self._context_resolver else {}
        owner = str(context.get("owner") or "")
        odysseus_session = str(context.get("odysseus_session_id") or "")
        workspace = str(context.get("workspace") or "")
        authority_workspace_id = str(
            context.get("authority_workspace_id") or ""
        )
        incognito = bool(context.get("incognito"))
        is_admin = bool(context.get("is_admin"))

        if context.get("interaction_policy") == "fail_on_interaction":
            return self._approve("reject")
        if owner and not is_admin:
            return self._approve("reject")

        # ── safe-dirs auto-approve ──
        # Any request whose target file is inside a configured safe dir is
        # approved immediately so the always-on assistant doesn't block on
        # known workspaces.
        # A selected Workspace is authority narrowing, not operation consent.
        # Only explicit installation safe directories retain this legacy
        # convenience; canonical Workspace requests continue to the durable
        # grant matcher or human prompt below.
        allowed_roots = [] if incognito or authority_workspace_id else self._safe_dirs
        if filepath and allowed_roots and any(
            _path_within(filepath, root) for root in allowed_roots
        ):
            logger.info("auto-approved %s: %s (safe-dirs match)", title, filepath)
            return self._approve()

        # ── stored durable grants ──
        if not incognito:
            try:
                from src.openclank.operation_approvals import (
                    match_operation_approval,
                )

                if match_operation_approval(
                    owner=owner,
                    permission_type=title,
                    filepath=filepath or "",
                    session_id=odysseus_session,
                    workspace_id=authority_workspace_id,
                    workspace_path=workspace,
                ):
                    logger.info("auto-approved %s: %s (canonical grant)", title, filepath or "*")
                    return self._approve()
            except Exception:
                pass
        if not incognito and self._grant_store is not None and self._grant_store.match(
            title,
            filepath=filepath or None,
            owner=owner,
            session_id=odysseus_session,
            workspace=workspace,
            workspace_id=authority_workspace_id,
        ):
            logger.info("auto-approved %s: %s (stored grant)", title, filepath or "*")
            return self._approve()

        # ── owner permission mode (yolo/auto) ──
        # Approve with once semantics — no durable grant, no UI wait. The
        # fail_on_interaction and non-admin rejects above stay authoritative.
        try:
            from src.permission_mode import auto_approves

            if auto_approves(owner):
                logger.info("auto-approved %s: %s (permission mode)", title, filepath or "*")
                return self._approve("once")
        except Exception:
            pass

        # ── surface to the UI and wait (forever) for the human ──
        if self._on_request is None:
            logger.warning("permission request for %s has no UI to surface to — rejecting", title)
            return self._approve("reject")

        projection = {}
        if not incognito and (owner or odysseus_session):
            from src.openclank.transcript_projection import get_projection

            projection = get_projection(odysseus_session, owner=owner)
            if not projection or projection.get("mimo_session_id") != session_id:
                return self._approve("reject")
        request_id = "perm_" + hashlib.sha256(
            f"{session_id}\0{projection.get('active_turn_id')}\0{time.time_ns()}".encode()
        ).hexdigest()[:16]
        req = PermissionRequest(
            request_id=request_id,
            tool_call=tool_call,
            raw_input=raw_input,
            options=options,
            title=title,
            session_id=session_id,
            odysseus_session_id=odysseus_session,
            owner=owner,
            workspace=workspace,
            authority_workspace_id=authority_workspace_id,
            turn_id=str(projection.get("active_turn_id") or ""),
            revision=int(projection.get("transcript_revision") or 0),
        )
        self.pending_requests[request_id] = req

        try:
            try:
                await self._on_request(req)
            except Exception as e:
                logger.warning("failed to surface permission request (%s) — rejecting: %s", title, e)
                return self._approve("reject")

            option_id = await req.wait()
            if incognito and option_id in {"chat", "workspace", "always"}:
                option_id = "once"

            if not incognito and option_id in {"chat", "workspace", "always"} and self._grant_store is not None:
                from src.openclank.permission_grants import derive_pattern
                from src.openclank.permission_grants import grant_scope_for_lifetime
                pattern = derive_pattern(raw_input)
                (
                    grant_session,
                    grant_workspace,
                    grant_workspace_id,
                ) = grant_scope_for_lifetime(
                    option_id,
                    session_id=odysseus_session,
                    workspace=workspace,
                    workspace_id=authority_workspace_id,
                )
                if (
                    option_id == "always"
                    or grant_session
                    or grant_workspace
                    or grant_workspace_id
                ):
                    from src.openclank.operation_approvals import (
                        record_operation_approval,
                    )

                    persisted = record_operation_approval(
                        owner=owner,
                        permission_type=title,
                        pattern=pattern,
                        lifetime=option_id,
                        session_id=grant_session,
                        workspace_id=grant_workspace_id,
                        target_path=filepath or workspace,
                    )
                    if not persisted:
                        self._grant_store.add(
                            title,
                            pattern,
                            owner=owner,
                            session_id=grant_session,
                            workspace=grant_workspace,
                            workspace_id=grant_workspace_id,
                        )
                        logger.info("stored %s grant: (%s, %s)", option_id, title, pattern)
                    else:
                        logger.info("stored canonical %s grant: (%s, %s)", option_id, title, pattern)
                else:
                    # There is no stable scope to persist against; preserve
                    # the safe Once semantics instead of minting an account-
                    # wide approval under a misleading label.
                    option_id = "once"

            # ACP/OpenCode only understands its wire-level allow-once and
            # allow-always outcomes. Chat/workspace are Open Clank's durable
            # scope refinements; they are persisted above and sent upstream
            # as the ordinary allow-always decision for this request.
            return self._approve("always" if option_id in {"chat", "workspace"} else option_id)
        finally:
            self.pending_requests.pop(request_id, None)

    def resolve(self, request_id: str, option_id: str) -> bool:
        """Resolve a pending permission request.

        Args:
            request_id: the permission request ID
            option_id: 'once', 'chat', 'workspace', 'always', or 'reject'

        Returns True if the request was found and resolved, False otherwise.
        """
        req = self.pending_requests.get(request_id)
        if req:
            req.resolve(option_id)
            return True
        return False

    def reject_scope(
        self,
        *,
        session_id: str = "",
        workspace: str = "",
        authority_workspace_id: str = "",
        all_pending: bool = False,
    ) -> int:
        """Reject pending approvals covered by a reset domain.

        Canonical Location/All-agent resets cannot safely map every legacy raw
        pending filepath back to a stable Location yet, so their fail-closed
        behavior is to reject all pending requests for this owner.
        """
        if (
            not session_id
            and not workspace
            and not authority_workspace_id
            and not all_pending
        ):
            return 0
        rejected = 0
        for req in list(self.pending_requests.values()):
            if req._future.done():
                continue
            if not all_pending and session_id and req.odysseus_session_id != session_id:
                continue
            if not all_pending and workspace and authority_workspace_id:
                if (
                    req.authority_workspace_id != authority_workspace_id
                    and req.workspace != workspace
                ):
                    continue
            elif not all_pending and workspace and req.workspace != workspace:
                continue
            elif (
                not all_pending
                and authority_workspace_id
                and req.authority_workspace_id != authority_workspace_id
            ):
                continue
            req.resolve("reject")
            rejected += 1
        return rejected

    def resolve_for(
        self,
        request_id: str,
        option_id: str,
        *,
        owner: str,
        session_id: str,
    ) -> bool:
        req = self.pending_requests.get(request_id)
        if (
            req is None
            or req.owner != owner
            or req.odysseus_session_id != session_id
        ):
            return False
        from src.openclank.transcript_projection import get_projection

        projection = get_projection(session_id, owner=owner)
        if (
            not projection
            or projection.get("mimo_session_id") != req.session_id
            or projection.get("active_turn_id") != req.turn_id
            or int(projection.get("transcript_revision") or 0) != req.revision
        ):
            return False
        req.resolve(option_id)
        return True
