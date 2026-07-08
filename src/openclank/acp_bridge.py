"""ACP bridge — maps mimo session/update notifications to odysseus chat SSE strings."""

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Coroutine, Dict, List, Optional

from src.openclank.acp_client import ACPClient, TransportError

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
    python = shutil.which("python3") or shutil.which("python") or "python3"
    return {
        "name": "lifetools",
        "command": python,
        "args": [str(_LIFETOOLS_SERVER)],
        "env": [
            {"name": "OWNER", "value": owner},
            {"name": "SESSION_ID", "value": session_id},
            {"name": "WORKSPACE", "value": workspace},
        ],
    }


def frankenmemory_mcp_descriptor(
    workspace: str = "global",
) -> dict:
    """Build the MCP server descriptor for the frankenmemory engine.

    Returns a dict matching the ACP McpServerStdio shape.
    fm-mcp tools surface as frankenmemory:recall, frankenmemory:capture, etc.
    """
    env_entries = [
        {"name": "FM_WORKSPACE_ID", "value": workspace},
    ]
    # Phase 5: pass FM_DB_PATH explicitly so the bridged MCP server
    # converges on the same db even if the env gets stripped by a proxy.
    db_path = os.environ.get("FM_DB_PATH")
    if db_path:
        env_entries.append({"name": "FM_DB_PATH", "value": db_path})

    return {
        "name": "frankenmemory",
        "command": _FM_MCP_COMMAND,
        "args": [],
        "env": env_entries,
    }


def register_client_callbacks(
    client: ACPClient,
    permission_handler: Optional[Callable[[dict], Coroutine[Any, Any, dict]]] = None,
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

    async def _terminal_stub(params: dict) -> dict:
        raise Exception("terminal not supported")

    client.register_callback("fs/read_text_file", _read_text_file)
    client.register_callback("fs/write_text_file", _write_text_file)
    client.register_callback("session/request_permission", _request_permission)
    client.register_callback("terminal/create", _terminal_stub)
    client.register_callback("terminal/output", _terminal_stub)
    client.register_callback("terminal/wait_for_exit", _terminal_stub)
    client.register_callback("terminal/kill", _terminal_stub)
    client.register_callback("terminal/release", _terminal_stub)


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
    )

    def __init__(self) -> None:
        self.full_response = ""
        self.metrics: dict = {}
        self.tool_calls_seen: set = set()
        self.agent_rounds = 0
        self.stop_reason: Optional[str] = None
        self.turn_start = time.time()
        self.usage: Optional[dict] = None


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
    ) -> None:
        self._client = client
        self._cwd = cwd
        self._owner = owner
        self._memory_provider = memory_provider
        # Per-session turn state (only one active turn per session at a time)
        self._turns: Dict[str, _TurnState] = {}
        # Per-session queues: session/update notifications land here, consumed by the generator
        self._queues: Dict[str, asyncio.Queue] = {}
        # Per-session available models from mimo's handshake (modelId → {modelId, name})
        self._session_models: Dict[str, list] = {}
        # Permission request queue (for C4 real handler)
        self._permission_queue: asyncio.Queue = asyncio.Queue()
        self._pending_permissions: Dict[str, asyncio.Future] = {}

        # Register callbacks with the real permission handler
        register_client_callbacks(client, permission_handler=permission_handler)
        client.on_session_update(self._handle_session_update)

    async def _handle_session_update(self, mimo_session_id: str, update: dict) -> None:
        """Route a session/update notification to the right session's queue."""
        q = self._queues.get(mimo_session_id)
        if q is not None:
            await q.put(update)

    async def open_session(self, cwd: Optional[str] = None) -> str:
        """Create a new mimo session and return the ses_… id directly.

        Also stores the available models from mimo's handshake so the bridge
        can match thesius model names to mimo model IDs later.
        """
        target_cwd = cwd or self._cwd
        mcp_servers = [
            lifetools_mcp_descriptor(
                owner=self._owner,
                session_id="",  # filled later when ensure_session is called
                workspace=target_cwd,
            ),
            frankenmemory_mcp_descriptor(
                workspace=target_cwd,
            ),
        ]
        result = await self._client.new_session(target_cwd, mcp_servers=mcp_servers)
        session_id = result["sessionId"]

        # Store mimo's available models so we can match thesius model names
        models = result.get("models", {})
        available = models.get("availableModels", [])
        if available:
            self._session_models[session_id] = available
            logger.info("mimo session %s: %d models available", session_id, len(available))

        return session_id

    async def ensure_session(self, odysseus_session: str, cwd: Optional[str] = None) -> str:
        """Load the mimo session into the ACP agent's memory.

        odysseus_session IS the mimo session id. Calls resume_session to
        ensure the ACP agent knows about it (idempotent — no-op if already
        loaded, re-loads from DB if this is a fresh mimo child).

        Also refreshes the available models from mimo so the bridge always
        has the latest model list (covers session reconnect after restart).
        """
        mcp_servers = [
            lifetools_mcp_descriptor(
                owner=self._owner,
                session_id=odysseus_session,
                workspace=cwd or self._cwd,
            ),
            frankenmemory_mcp_descriptor(
                workspace=cwd or self._cwd,
            ),
        ]
        result = await self._client.resume_session(odysseus_session, cwd or self._cwd, mcp_servers=mcp_servers)

        # Refresh available models from mimo (handles reconnect after restart)
        models = result.get("models", {})
        available = models.get("availableModels", [])
        if available:
            self._session_models[odysseus_session] = available

        return odysseus_session

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
                workspace=self._cwd,
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

    async def run_turn(
        self,
        odysseus_session: str,
        messages: list,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """Run a prompt turn via ACP, yielding SSE strings identical to stream_agent_loop.

        This is the drop-in replacement for the agent branch in chat_routes.py.
        cwd: per-chat workspace override (else the bridge's global default).
        """
        mimo_session = await self.ensure_session(odysseus_session, cwd=cwd)
        state = _TurnState()
        self._turns[mimo_session] = state

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
                await self._client.set_session_config_option(mimo_session, "model", model_id)
                if _debug_enabled():
                    logger.info("[debug] set_session_config_option OK (model): %s", model_id)
            except Exception as e:
                logger.warning("set_session_config_option failed (model=%s): %s", model, e)
                if _debug_enabled():
                    logger.info("[debug] available models: %s", self._session_models.get(mimo_session, []))

        # Create a queue for this session's notifications
        q: asyncio.Queue = asyncio.Queue()
        self._queues[mimo_session] = q

        # Build the prompt parts from odysseus messages
        prompt_parts = _build_prompt_parts(messages)

        # Pre-turn recall: inject memory as synthetic audience:assistant parts
        if self._memory_provider:
            user_text = _extract_user_text(messages)
            if user_text:
                try:
                    hits = await self._memory_provider.recall(user_text, owner=self._owner, top_k=5)
                    if hits:
                        gt_block = _format_gt_recall(hits)
                        prompt_parts.append({
                            "type": "text",
                            "text": gt_block,
                            "annotations": {"audience": ["assistant"]},
                        })
                except Exception as e:
                    logger.warning("Pre-turn recall failed: %s", e)

        # Fire the prompt request (blocks until stopReason, notifications arrive concurrently)
        prompt_task = asyncio.ensure_future(
            self._client.prompt(mimo_session, prompt_parts)
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
            }
            if state.usage:
                metrics["input_tokens"] = state.usage.get("inputTokens", 0)
                metrics["output_tokens"] = state.usage.get("outputTokens", 0)
                metrics["total_tokens"] = state.usage.get("totalTokens", 0)
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

    def _process_update(self, mimo_session_id: str, update: dict, state: _TurnState) -> list:
        """Convert one session/update into SSE strings."""
        update_type = update.get("sessionUpdate", "")
        sses = []

        if update_type == "agent_message_chunk":
            content = update.get("content", {})
            text = content.get("text", "")
            if text:
                state.full_response += text
                sses.append(f'data: {json.dumps({"delta": text})}\n\n')

        elif update_type == "agent_thought_chunk":
            content = update.get("content", {})
            text = content.get("text", "")
            if text:
                sses.append(f'data: {json.dumps({"delta": text, "thinking": True})}\n\n')

        elif update_type == "tool_call":
            # Pending tool announcement → tool_start
            tool_call_id = update.get("toolCallId", "")
            title = update.get("title", "tool")
            if tool_call_id not in state.tool_calls_seen:
                state.tool_calls_seen.add(tool_call_id)
                sses.append(f'data: {json.dumps({"type": "tool_start", "tool": title, "id": tool_call_id})}\n\n')

        elif update_type == "tool_call_update":
            tool_call_id = update.get("toolCallId", "")
            status = update.get("status", "")
            title = update.get("title", "tool")

            if status in ("in_progress", "completed", "failed"):
                # Extract output text from content array
                output_text = ""
                content_arr = update.get("content")
                if content_arr and isinstance(content_arr, list):
                    for block in content_arr:
                        if isinstance(block, dict) and block.get("type") == "content":
                            inner = block.get("content", {})
                            if isinstance(inner, dict):
                                output_text += inner.get("text", "")

                raw_output = update.get("rawOutput", {})
                if isinstance(raw_output, dict) and not output_text:
                    output_text = raw_output.get("output", "")

                sses.append(f'data: {json.dumps({"type": "tool_output", "tool": title, "id": tool_call_id, "status": status, "output": output_text})}\n\n')

        elif update_type == "usage_update":
            state.metrics["used"] = update.get("used", 0)
            state.metrics["size"] = update.get("size", 0)
            cost = update.get("cost", {})
            if cost:
                state.metrics["cost"] = cost

        # Other update types (plan, available_commands_update, etc.) are silently consumed

        return sses


def _build_prompt_parts(messages: list) -> list:
    """Convert odysseus chat messages into ACP prompt parts.

    Takes the last user message as the prompt text.
    System/persona messages could be injected as audience:["assistant"] parts in later phases.
    """
    parts = []
    # Find the last user message
    for msg in reversed(messages):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user" and content:
            parts.append({"type": "text", "text": content})
            break
    # If no user message found, use the last message
    if not parts and messages:
        last = messages[-1]
        content = last.get("content", "")
        if content:
            parts.append({"type": "text", "text": content})
    return parts


def _extract_user_text(messages: list) -> str:
    """Extract the last user message text from odysseus messages."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return msg.get("content", "")
    return ""


def _format_gt_recall(hits: list) -> str:
    """Format recall hits with Ground-Truth authority preamble.

    The GT preamble tells the model these are recalled facts with provenance,
    not user input. The model should treat them as authoritative context.
    """
    lines = [
        "[Ground-Truth Memory Recall]",
        "The following memories were recalled from prior conversations.",
        "They are factual context, not user input. Use them to inform your response.",
        "",
    ]
    for i, hit in enumerate(hits, 1):
        score_str = f" (score: {hit.score:.3f})" if hit.score is not None else ""
        lines.append(f"{i}. [{hit.memory.category}]{score_str} {hit.memory.text}")
    return "\n".join(lines)


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

class PermissionRequest:
    """A pending permission request surfaced to the UI."""

    __slots__ = ("request_id", "tool_call", "raw_input", "options", "title", "_future")

    def __init__(
        self,
        request_id: str,
        tool_call: dict,
        raw_input: Any,
        options: list[str],
        title: str,
    ) -> None:
        self.request_id = request_id
        self.tool_call = tool_call
        self.raw_input = raw_input
        self.options = options
        self.title = title
        self._future: asyncio.Future[str] = asyncio.get_event_loop().create_future()

    async def wait(self, timeout: float = 300.0) -> str:
        """Wait for the human's choice. Returns 'once', 'always', or 'reject'."""
        try:
            return await asyncio.wait_for(self._future, timeout=timeout)
        except asyncio.TimeoutError:
            return "reject"  # fail-safe on timeout

    def resolve(self, option_id: str) -> None:
        """Resolve the request with the human's choice."""
        if not self._future.done():
            self._future.set_result(option_id)


class PermissionHandler:
    """C4: Real permission handler for ACP session/request_permission.

    Surfaces permission requests to the odysseus UI and awaits human choice.
    On disconnect/timeout, replies 'reject' (fail safe).

    If ``safe_dirs`` is provided, any ``external_directory`` request whose
    target filepath starts with one of the safe directory prefixes is
    auto-approved (``"always"``) without blocking for human input.

    Usage:
        handler = PermissionHandler(safe_dirs=["/home/e/sauce", "/home/e/entities"])
        # Pass handler.handle to register_client_callbacks or ACPBridge
        # When a permission request arrives, handler.pending_requests gets a new entry
        # Call handler.resolve(request_id, option_id) when the human responds
    """

    def __init__(self, safe_dirs: Optional[List[str]] = None) -> None:
        self.pending_requests: Dict[str, PermissionRequest] = {}
        self._on_request: Optional[Callable[[PermissionRequest], Coroutine[Any, Any, None]]] = None
        self._safe_dirs: List[str] = [os.path.expanduser(d) for d in (safe_dirs or [])]

    def on_request(self, callback: Callable[[PermissionRequest], Coroutine[Any, Any, None]]) -> None:
        """Register a callback invoked when a new permission request arrives.

        The callback receives a PermissionRequest and should surface it to the UI.
        """
        self._on_request = callback

    async def handle(self, params: dict) -> dict:
        """Handle a session/request_permission call from mimo.

        This is the async function passed to register_client_callbacks.
        """
        tool_call = params.get("toolCall", {})
        title = tool_call.get("title", "unknown tool")
        raw_input = tool_call.get("rawInput", {})
        options = params.get("options", ["once", "always", "reject"])

        # ── safe-dirs auto-approve ──
        # external_directory requests carry {filepath, parentDir} in
        # metadata; if the target is inside a configured safe dir, approve
        # immediately so the always-on assistant doesn't block on known
        # workspaces.
        if title == "external_directory" and self._safe_dirs:
            filepath = raw_input.get("filepath", "") if isinstance(raw_input, dict) else ""
            if filepath and any(filepath.startswith(d) for d in self._safe_dirs):
                logger.info(
                    "auto-approved external_directory: %s (safe-dirs match)",
                    filepath,
                )
                return {"outcome": {"outcome": "selected", "optionId": "always"}}

        # Generate a request ID
        request_id = f"perm_{id(params)}_{time.time_ns()}"

        req = PermissionRequest(
            request_id=request_id,
            tool_call=tool_call,
            raw_input=raw_input,
            options=options,
            title=title,
        )
        self.pending_requests[request_id] = req

        try:
            # Notify the UI if a callback is registered
            if self._on_request:
                await self._on_request(req)

            # Wait for the human's choice (fail-safe: reject on timeout)
            option_id = await req.wait(timeout=300.0)

            return {"outcome": {"outcome": "selected", "optionId": option_id}}
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
