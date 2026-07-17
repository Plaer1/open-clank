"""ACP bridge — maps mimo session/update notifications to odysseus chat SSE strings."""

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Coroutine, Dict, List, Optional

from src.memory_scope import chat_workspace
from src.openclank.acp_client import ACPClient, RPCError, TransportError
from src.openclank.permission_grants import derive_pattern

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


def lifetools_mcp_descriptor(
    owner: str = "",
    session_id: str = "",
    workspace: str = "",
) -> dict:
    """Build the MCP server descriptor for the life-tools bridge.

    Returns a dict matching the ACP McpServerStdio shape:
    {name, command, args, env:[{name,value}]}
    """
    scope = hashlib.sha256(
        f"{owner}\0{session_id}\0{workspace}".encode("utf-8")
    ).hexdigest()[:12]
    return {
        "name": f"lifetools_{scope}",
        "command": sys.executable,
        "args": [str(_LIFETOOLS_SERVER)],
        "env": [
            {"name": "OWNER", "value": owner},
            {"name": "SESSION_ID", "value": session_id},
            {"name": "WORKSPACE", "value": workspace},
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
    owner = owner.strip()
    workspace = workspace.strip() or chat_workspace()
    if not owner:
        raise ValueError("frankenmemory MCP sessions require an authenticated owner")
    env_entries = [
        {"name": "FM_WORKSPACE_ID", "value": workspace},
        {"name": "FM_OWNER", "value": owner},
        {"name": "FM_SESSION_ID", "value": session_id},
    ]
    # Phase 5: pass FM_DB_PATH explicitly so the bridged MCP server
    # converges on the same db even if the env gets stripped by a proxy.
    db_path = os.environ.get("FM_DB_PATH")
    if db_path:
        if not os.path.isabs(db_path):
            raise ValueError("FM_DB_PATH must be absolute")
        env_entries.append({"name": "FM_DB_PATH", "value": db_path})
    db_id = os.environ.get("FM_DB_ID")
    if db_id:
        env_entries.append({"name": "FM_DB_ID", "value": db_id})

    scope = hashlib.sha256(
        f"{owner}\0{session_id}\0{workspace}".encode("utf-8")
    ).hexdigest()[:12]
    return {
        "name": f"frankenmemory_{scope}",
        "command": _FM_MCP_COMMAND,
        "args": [],
        "env": env_entries,
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
        logger.warning("failed to compile Odysseus MCP descriptors: %s", exc)
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
        "agent_rounds",
        "stop_reason",
        "turn_start",
        "usage",
        "max_tool_calls",
        "policy_stopped",
    )

    def __init__(self, max_tool_calls: int = 0) -> None:
        self.full_response = ""
        self.metrics: dict = {}
        self.tool_calls_seen: set = set()
        self.agent_rounds = 0
        self.stop_reason: Optional[str] = None
        self.turn_start = time.time()
        self.usage: Optional[dict] = None
        self.max_tool_calls = max(0, int(max_tool_calls or 0))
        self.policy_stopped = False


class ACPBridge:
    """Translates ACP session/update notifications into odysseus chat SSE strings.

    One bridge per ACP client. Manages per-turn state and yields SSE strings
    that the chat_routes handler can consume identically to stream_agent_loop().
    """

    def __init__(
        self,
        client: ACPClient,
        cwd: str,
        owner: str = "",
        permission_handler: Optional[Callable[[dict], Coroutine[Any, Any, dict]]] = None,
        memory_provider: Any = None,
        session_map_path: Optional[Path] = None,
    ) -> None:
        self._client = client
        self._cwd = cwd
        self._owner = owner
        self._memory_provider = memory_provider
        self._delete_session_callback = None
        # Per-session turn state (only one active turn per session at a time)
        self._turns: Dict[str, _TurnState] = {}
        # Per-session queues: session/update notifications land here, consumed by the generator
        self._queues: Dict[str, asyncio.Queue] = {}
        # Per-session available models from mimo's handshake (modelId → {modelId, name})
        self._session_models: Dict[str, list] = {}
        # Full negotiated control-plane state, keyed by MiMo session id. The
        # canonical desired selection is persisted on the Odysseus session.
        self._session_state: Dict[str, dict] = {}
        self._session_context: Dict[str, dict] = {}
        self.question_handler = QuestionHandler()
        from src.openclank.acp_terminal import ACPTerminalManager

        self.terminal_manager = ACPTerminalManager(
            lambda session_id: dict(self._session_context.get(session_id) or {})
        )
        # Latest full catalog from any handshake — mimo is the model
        # authority; /api/models reports this list up to the picker.
        self.available_models: list = []
        # Odysseus-session → mimo-session remap for sessions whose mimo-side
        # state is gone (e.g. created before the MIMOCODE_HOME isolation).
        # Persisted so the remap survives server restarts.
        self._session_map_path = session_map_path or (
            Path(os.environ.get(
                "ODYSSEUS_DATA_DIR",
                str(Path(__file__).resolve().parents[2] / "data"),
            )) / "mimocode" / "session-map.json"
        )
        try:
            self._session_map: Dict[str, str] = json.loads(self._session_map_path.read_text())
        except Exception:
            self._session_map = {}

        # Register callbacks with the real permission handler
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

        # C1: surface permission prompts through the active turn's SSE stream
        if permission_handler is not None and hasattr(permission_handler, "on_request"):
            permission_handler.on_request(self._surface_permission)

    def set_session_delete_callback(self, callback) -> None:
        self._delete_session_callback = callback

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
        }

    def _bind_canonical_session(
        self,
        mimo_session: str,
        odysseus_session: str,
        owner: str,
    ) -> None:
        self._session_context[mimo_session] = {
            "odysseus_session_id": odysseus_session,
            "owner": owner,
            "workspace": self._cwd,
            "incognito": False,
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
        self._persist_session_state(mimo_session)

    def _persist_session_state(self, mimo_session: str) -> None:
        context = self._session_context.get(mimo_session)
        state = self._session_state.get(mimo_session)
        if not context or state is None:
            return
        try:
            from src.openclank.transcript_projection import save_mimo_state

            saved = save_mimo_state(
                context["odysseus_session_id"],
                state,
                owner=context["owner"] or None,
            )
            state["revision"] = saved["revision"]
        except KeyError:
            pass

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
            raise ValueError(f"MiMo did not advertise config option {config_id!r}")
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
                state["plan_state"]["approved_revision"] = req.plan_revision
            state.pop("pending_question", None)
            self._persist_session_state(req.mimo_session_id)

    async def _handle_session_update(self, mimo_session_id: str, update: dict) -> None:
        """Route a session/update notification to the right session's queue."""
        if update.get("sessionUpdate") == "available_commands_update":
            state = self._session_state.setdefault(mimo_session_id, {})
            state["commands"] = list(update.get("availableCommands") or [])
            self._persist_session_state(mimo_session_id)
        q = self._queues.get(mimo_session_id)
        if q is not None:
            await q.put(update)

    async def open_session(
        self,
        cwd: Optional[str] = None,
        owner: Optional[str] = None,
        odysseus_session: str = "",
        extra_mcp_servers: Optional[list[dict]] = None,
    ) -> str:
        """Create a new mimo session and return the ses_… id directly.

        Also stores the available models from mimo's handshake so the bridge
        can match thesius model names to mimo model IDs later.
        """
        target_cwd = cwd or self._cwd
        mcp_servers = [
            lifetools_mcp_descriptor(
                owner=owner if owner is not None else self._owner,
                session_id=odysseus_session,
                workspace=target_cwd,
            ),
            frankenmemory_mcp_descriptor(
                owner=owner if owner is not None else self._owner,
                session_id=odysseus_session,
            ),
            *(extra_mcp_servers or []),
        ]
        result = await self._client.new_session(target_cwd, mcp_servers=mcp_servers)
        session_id = result["sessionId"]
        self._capture_handshake(session_id, result)

        # Store mimo's available models so we can match thesius model names
        models = result.get("models", {})
        available = models.get("availableModels", [])
        if available:
            self._session_models[session_id] = available
            self.available_models = available
            logger.info("mimo session %s: %d models available", session_id, len(available))

        return session_id

    async def ensure_session(
        self,
        odysseus_session: str,
        cwd: Optional[str] = None,
        owner: Optional[str] = None,
        extra_mcp_servers: Optional[list[dict]] = None,
    ) -> str:
        """Load the mimo session into the ACP agent's memory.

        odysseus_session IS the mimo session id. Calls resume_session to
        ensure the ACP agent knows about it (idempotent — no-op if already
        loaded, re-loads from DB if this is a fresh mimo child).

        Also refreshes the available models from mimo so the bridge always
        has the latest model list (covers session reconnect after restart).
        """
        target = self._session_map.get(odysseus_session)
        if target is None:
            new_id = await self.open_session(
                cwd=cwd,
                owner=owner,
                odysseus_session=odysseus_session,
                extra_mcp_servers=extra_mcp_servers,
            )
            self._session_map[odysseus_session] = new_id
            self._bind_canonical_session(
                new_id,
                odysseus_session,
                owner if owner is not None else self._owner,
            )
            try:
                self._session_map_path.parent.mkdir(parents=True, exist_ok=True)
                self._session_map_path.write_text(json.dumps(self._session_map, indent=1))
            except Exception as exc:
                logger.warning("failed to persist session map: %s", exc)
            return new_id
        mcp_servers = [
            lifetools_mcp_descriptor(
                owner=owner if owner is not None else self._owner,
                session_id=odysseus_session,
                workspace=cwd or self._cwd,
            ),
            frankenmemory_mcp_descriptor(
                owner=owner if owner is not None else self._owner,
                session_id=odysseus_session,
            ),
            *(extra_mcp_servers or []),
        ]
        try:
            result = await self._client.resume_session(target, cwd or self._cwd, mcp_servers=mcp_servers)
        except RPCError as e:
            # mimo doesn't know this session (state predates the isolated
            # MIMOCODE_HOME, or its store was wiped). The full chat history
            # rides in every prompt, so a fresh mimo session continues the
            # conversation seamlessly — remap and persist.
            logger.warning(
                "mimo resume failed for %s (%s) — opening a fresh mimo session", target, e
            )
            new_id = await self.open_session(
                cwd=cwd,
                owner=owner,
                odysseus_session=odysseus_session,
                extra_mcp_servers=extra_mcp_servers,
            )
            self._session_map[odysseus_session] = new_id
            self._bind_canonical_session(
                new_id,
                odysseus_session,
                owner if owner is not None else self._owner,
            )
            try:
                self._session_map_path.parent.mkdir(parents=True, exist_ok=True)
                self._session_map_path.write_text(json.dumps(self._session_map, indent=1))
            except Exception as we:
                logger.warning("failed to persist session map: %s", we)
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
        )

        return target

    async def resume_session(self, odysseus_session: str, mimo_session_id: str) -> None:
        """Re-establish a session after a crash/restart.

        odysseus_session IS the mimo session id. Re-attaches the standard
        MCP servers (lifetools + frankenmemory) so the session has tools.
        """
        mcp_servers = [
            lifetools_mcp_descriptor(
                owner=self._owner,
                session_id=odysseus_session,
                workspace=self._cwd,
            ),
            frankenmemory_mcp_descriptor(
                owner=self._owner,
            ),
        ]
        await self._client.resume_session(mimo_session_id, self._cwd, mcp_servers=mcp_servers)

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
        return self._session_map.get(odysseus_session, odysseus_session)

    def mapped_sessions(self) -> dict[str, str]:
        return dict(self._session_map)

    def forget_session(self, odysseus_session: str) -> None:
        mimo_session = self._session_map.pop(odysseus_session, odysseus_session)
        self._session_models.pop(mimo_session, None)
        self._session_state.pop(mimo_session, None)
        self._session_context.pop(mimo_session, None)
        self._turns.pop(mimo_session, None)
        self._queues.pop(mimo_session, None)
        try:
            self._session_map_path.parent.mkdir(parents=True, exist_ok=True)
            self._session_map_path.write_text(json.dumps(self._session_map, indent=1))
        except Exception as exc:
            logger.warning("failed to persist session map after cleanup: %s", exc)
        try:
            from src.openclank.transcript_projection import delete_projection

            delete_projection(odysseus_session)
        except Exception as exc:
            logger.debug("projection cleanup skipped for %s: %s", odysseus_session, exc)

    async def _maybe_inject_digest(
        self,
        messages: list,
        *,
        owner: Optional[str],
        incognito: bool,
    ) -> list:
        """Prepend one memory index card to turns that don't carry one.

        Odysseus-prefaced turns already have the block (sentinel check keeps
        it single); this covers resume flows and any future mimo-first path.
        Fail-open: no digest, no block, turn proceeds untouched."""
        if incognito or self._memory_provider is None:
            return messages
        if not hasattr(self._memory_provider, "digest"):
            return messages
        from src.memory_digest import DIGEST_SENTINEL

        for message in messages:
            if DIGEST_SENTINEL in _content_text(message.get("content")):
                return messages
        try:
            digest = await asyncio.wait_for(
                self._memory_provider.digest(owner=owner or self._owner), timeout=0.25
            )
        except Exception as exc:
            logger.debug("bridge memory digest unavailable: %s", exc)
            return messages
        block = _format_memory_digest(digest)
        if not block:
            return messages
        from src.prompt_security import untrusted_context_message

        out = list(messages)
        insert_at = next(
            (index for index in range(len(out) - 1, -1, -1) if out[index].get("role") == "user"),
            len(out),
        )
        out.insert(insert_at, untrusted_context_message("memory bank index", block))
        return out

    async def run_turn(
        self,
        odysseus_session: str,
        messages: list,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
        owner: Optional[str] = None,
        turn_envelope: Optional[dict] = None,
    ) -> AsyncGenerator[str, None]:
        """Run a prompt turn via ACP, yielding SSE strings identical to stream_agent_loop.

        This is the drop-in replacement for the agent branch in chat_routes.py.
        cwd: per-chat workspace override (else the bridge's global default).
        """
        if odysseus_session in self._session_map and self._delete_session_callback:
            await self._delete_session_callback(odysseus_session)
        envelope = json.loads(json.dumps(turn_envelope or {}, default=str))
        incognito = bool(envelope.get("incognito"))
        extra_mcp_servers, mcp_disabled = odysseus_mcp_descriptors(
            is_admin=bool(envelope.get("is_admin")) and not incognito
        )
        envelope.setdefault("disabled_tools", []).extend(mcp_disabled)
        if incognito:
            mimo_session = await self.open_session(
                cwd=cwd,
                owner=owner,
                odysseus_session=odysseus_session,
            )
            self._session_context[mimo_session] = {
                "odysseus_session_id": odysseus_session,
                "owner": owner if owner is not None else self._owner,
                "workspace": cwd or self._cwd,
                "incognito": True,
                "is_admin": bool(envelope.get("is_admin")),
            }
        else:
            mimo_session = await self.ensure_session(
                odysseus_session,
                cwd=cwd,
                owner=owner,
                extra_mcp_servers=extra_mcp_servers,
            )
            self._session_context.setdefault(mimo_session, {}).update({
                "workspace": cwd or self._cwd,
                "incognito": False,
                "is_admin": bool(envelope.get("is_admin")),
            })
        turn_id = _turn_source_id(messages)
        control_state = self._session_state.get(mimo_session, {})
        try:
            from src.openclank.transcript_projection import canonical_snapshot, record_projection

            snapshot = canonical_snapshot(
                odysseus_session,
                owner=owner if owner is not None else self._owner,
            )
            record_projection(
                snapshot,
                mimo_session_id=mimo_session,
                workspace=cwd or self._cwd,
                endpoint_url="mimo://acp",
                model=model or "mimo",
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
        desired_mode = desired.get("mode")
        if desired_mode:
            try:
                await self.set_config_option(
                    odysseus_session,
                    "mode",
                    desired_mode,
                    cwd=cwd,
                    owner=owner,
                )
            except (ValueError, RPCError) as exc:
                logger.warning("MiMo mode restore failed (%s): %s", desired_mode, exc)
                yield f'data: {json.dumps({"type": "config_error", "config": "mode", "requested": desired_mode, "error": str(exc)})}\n\n'
                yield f'event: error\ndata: {json.dumps({"error": f"MiMo rejected mode {desired_mode!r}; the prompt was not sent", "status": 409})}\n\n'
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
                yield f'event: error\ndata: {json.dumps({"error": f"MiMo rejected model {model!r}; the prompt was not sent", "status": 409})}\n\n'
                yield "data: [DONE]\n\n"
                return

        # Create a queue for this session's notifications
        q: asyncio.Queue = asyncio.Queue()
        self._queues[mimo_session] = q

        # Build the prompt parts from odysseus messages
        messages = await self._maybe_inject_digest(
            messages, owner=owner, incognito=incognito
        )
        prompt_parts = _build_prompt_parts(
            messages,
            turn_id=turn_id,
            workspace=cwd or self._cwd,
            authoritative_system=str(envelope.get("system_prompt") or ""),
        )
        prompt_meta = {"odysseus": envelope}
        prompt_meta["odysseus"]["tools"] = _mimo_tool_policy(envelope)

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
                yield f'event: error\ndata: {json.dumps({"error": str(e), "status": 500})}\n\n'
                yield "data: [DONE]\n\n"
                return

            state.stop_reason = result.get("stopReason", "end_turn")
            state.usage = result.get("usage")

            # Emit final metrics
            elapsed = time.time() - state.turn_start
            metrics = {
                "response_time": round(elapsed, 2),
                "model": model or "mimo",
                "stop_reason": state.stop_reason,
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
                        logger.warning("failed to delete incognito MiMo session %s: %s", mimo_session, exc)

    def _persist_plan_update(self, mimo_session_id: str, payload: dict) -> dict:
        state = self._session_state.setdefault(mimo_session_id, {})
        previous = state.get("plan_state") or {}
        combined = {
            key: value
            for key, value in previous.items()
            if key not in ("digest", "revision", "approved_revision")
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
            title = update.get("title", "tool")
            if tool_call_id not in state.tool_calls_seen:
                state.tool_calls_seen.add(tool_call_id)
                sses.append(f'data: {json.dumps({"type": "tool_start", "tool": title, "id": tool_call_id, "data": _sanitize_event_value(update)})}\n\n')

        elif update_type == "tool_call_update":
            tool_call_id = update.get("toolCallId", "")
            status = update.get("status", "")
            title = update.get("title", "tool")
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
            sses.append(f'data: {json.dumps({"type": event_type, "tool": title, "id": tool_call_id, "status": status, "output": output_text, "data": _sanitize_event_value(update)})}\n\n')

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

        elif update_type == "current_mode_update":
            sses.append(f'data: {json.dumps({"type": "mode_update", "data": _sanitize_event_value(update)})}\n\n')

        elif update_type == "config_option_update":
            sses.append(f'data: {json.dumps({"type": "config_update", "data": _sanitize_event_value(update)})}\n\n')

        elif update_type == "session_info_update":
            sses.append(f'data: {json.dumps({"type": "session_info", "data": _sanitize_event_value(update)})}\n\n')

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


def _turn_source_id(messages: list) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        metadata = message.get("metadata") or {}
        if metadata.get("_db_id"):
            return str(metadata["_db_id"])
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
    "manage_memory": {"memory", "frankenmemory_*"},
}


def _mimo_tool_policy(envelope: dict) -> dict[str, bool]:
    if envelope.get("incognito") or envelope.get("mode") == "chat":
        return {"*": False}
    policy: dict[str, bool] = {}
    for raw_name in envelope.get("disabled_tools") or []:
        name = str(raw_name)
        normalized = name.replace("mcp__", "").replace(":", "_")
        aliases = _MIMO_TOOL_ALIASES.get(name, {normalized})
        for alias in aliases:
            policy[alias] = False
        policy[f"lifetools_*_{normalized}"] = False
    for raw_name in envelope.get("forced_tools") or []:
        name = str(raw_name)
        for alias in _MIMO_TOOL_ALIASES.get(name, {name}):
            policy.setdefault(alias, True)
    return policy


def _extract_user_text(messages: list) -> str:
    """Extract the last user message text from odysseus messages."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return _content_text(msg.get("content", ""))
    return ""


def _format_memory_digest(digest) -> str:
    """Render the shared index card (untrusted framing comes from the
    untrusted_context_message wrapper at injection, not repeated here)."""
    from src.memory_digest import render_digest

    return render_digest(digest) or ""


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
            raise ValueError("MiMo question must contain 1-16 prompts")
        result: list[dict] = []
        for raw in value:
            if not isinstance(raw, dict):
                raise ValueError("MiMo question prompt must be an object")
            question = str(raw.get("question") or "").strip()[:4_096]
            if not question:
                raise ValueError("MiMo question text is required")
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
        if not mimo_session or not odysseus_session or not owner or context.get("incognito"):
            return {"rejected": True}
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
            logger.warning("failed to surface MiMo question: %s", exc)
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
        from src.openclank.transcript_projection import get_projection

        projection = get_projection(session_id, owner=owner)
        context = self._context_resolver(req.mimo_session_id) if self._context_resolver else {}
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
        self.turn_id = turn_id
        self.revision = revision
        self._future: asyncio.Future[str] = asyncio.get_event_loop().create_future()

    async def wait(self, timeout: Optional[float] = None) -> str:
        """Wait for the human's choice. Returns 'once', 'always', or 'reject'.

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
        options = params.get("options", ["once", "always", "reject"])
        session_id = params.get("sessionId", "")
        filepath = raw_input.get("filepath", "") if isinstance(raw_input, dict) else ""
        context = self._context_resolver(session_id) if self._context_resolver else {}
        owner = str(context.get("owner") or "")
        odysseus_session = str(context.get("odysseus_session_id") or "")
        workspace = str(context.get("workspace") or "")
        incognito = bool(context.get("incognito"))
        is_admin = bool(context.get("is_admin"))

        if incognito or (owner and not is_admin):
            return self._approve("reject")

        # ── safe-dirs auto-approve ──
        # Any request whose target file is inside a configured safe dir is
        # approved immediately so the always-on assistant doesn't block on
        # known workspaces.
        allowed_roots = [workspace] if workspace else self._safe_dirs
        if filepath and allowed_roots and any(
            _path_within(filepath, root) for root in allowed_roots
        ):
            logger.info("auto-approved %s: %s (safe-dirs match)", title, filepath)
            return self._approve()

        # ── stored durable grants ──
        if self._grant_store is not None and self._grant_store.match(
            title,
            filepath=filepath or None,
            owner=owner,
            session_id=odysseus_session,
            workspace=workspace,
        ):
            logger.info("auto-approved %s: %s (stored grant)", title, filepath or "*")
            return self._approve()

        # ── surface to the UI and wait (forever) for the human ──
        if self._on_request is None:
            logger.warning("permission request for %s has no UI to surface to — rejecting", title)
            return self._approve("reject")

        projection = {}
        if owner or odysseus_session:
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

            if option_id == "always" and self._grant_store is not None:
                from src.openclank.permission_grants import derive_pattern
                pattern = derive_pattern(raw_input)
                self._grant_store.add(
                    title,
                    pattern,
                    owner=owner,
                    session_id=odysseus_session,
                    workspace=workspace,
                )
                logger.info("stored durable grant: (%s, %s)", title, pattern)

            return self._approve(option_id)
        finally:
            self.pending_requests.pop(request_id, None)

    def resolve(self, request_id: str, option_id: str) -> bool:
        """Resolve a pending permission request.

        Args:
            request_id: the permission request ID
            option_id: 'once', 'always', or 'reject'

        Returns True if the request was found and resolved, False otherwise.
        """
        req = self.pending_requests.get(request_id)
        if req:
            req.resolve(option_id)
            return True
        return False

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
