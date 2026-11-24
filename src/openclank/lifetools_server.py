"""lifetools_server.py — MCP bridge exposing Open Clank life-tools.

Per-session spawned server (env-baked context). Exposes all FUNCTION_TOOL_SCHEMAS
minus direct process/unbrokered native overlaps and ACP-internal tools. File
read/write/edit/list/search are the Rust-backed AgentScope lane and surface as
lifetools:<tool>; mimo's direct OS implementations stay disabled.
"""

import asyncio
import base64
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# This file is launched directly by the per-session MCP descriptor.  In that
# mode Python adds ``src/openclank`` (not the repository root) to sys.path, so
# every ``src.*`` import below would fail unless the root is installed first.
_OPEN_CLANK_ROOT = Path(__file__).resolve().parents[2]
if _OPEN_CLANK_ROOT.exists() and str(_OPEN_CLANK_ROOT) not in sys.path:
    sys.path.insert(0, str(_OPEN_CLANK_ROOT))

from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, Tool, TextContent, ImageContent

from src.openclank.mcp_tool_server import ToolServer

from src.tool_schemas import (
    FUNCTION_TOOL_SCHEMAS,
    OPENTHESIUS_BRIDGE_EXCLUDED_TOOLS,
)
from src.memery_provider import BROKER_MEMORY_TOOLS

logger = logging.getLogger(__name__)

# Additional tools excluded from the bridge beyond the 9 coding-overlap.
# These overlap with mimo native capabilities or are ACP-internal.
_BRIDGE_EXTRA_EXCLUDED = {
    "web_search",    # mimo has its own web_search
    "manage_rag",    # document administration executes in the authenticated host
    "web_fetch",     # mimo has its own web_fetch
    "ask_user",      # mimo handles via ACP permission ask
    "update_plan",   # mimo plan agent handles natively
}

_ALL_EXCLUDED = OPENTHESIUS_BRIDGE_EXCLUDED_TOOLS | _BRIDGE_EXTRA_EXCLUDED | {"manage_memory", "recall_memory"} | set(BROKER_MEMORY_TOOLS)

# Session context from env (baked at spawn time)
_SESSION_ID = os.environ.get("SESSION_ID", "")
_OWNER = os.environ.get("OWNER", "")
_SKILL_OWNER = (
    os.environ.get("OPEN_CLANK_SKILL_OWNER", "").strip()
    if "OPEN_CLANK_SKILL_OWNER" in os.environ
    else _OWNER.strip()
)
_WORKSPACE = os.environ.get("WORKSPACE", "")
_AUTHORITY_WORKSPACE_ID = os.environ.get(
    "OPEN_CLANK_AUTHORITY_WORKSPACE_ID", ""
).strip()
_BROWSER_CONTROL_ENABLED = os.environ.get("OPEN_CLANK_BROWSER_CONTROL_ENABLED") == "1"
_BROWSER_MUTATIONS_ENABLED = os.environ.get("OPEN_CLANK_BROWSER_CONTROL_MUTATIONS_ENABLED") == "1"
_COPAL_WORKSPACE = os.environ.get("COPAL_WORKSPACE", "default").strip() or "default"
_MEMORY_PROVIDER = None
_MEMORY_PROVIDER_LOCK = None

_PROJECT_MUTATION_POLICY_TOOL = "project_mutation_policy"

_EXPLICIT_SKILLS_DIR = os.getenv("OPEN_CLANK_SKILLS_DIR", "").strip()
_DATA_DIR = str(
    Path(_EXPLICIT_SKILLS_DIR).parent
    if _EXPLICIT_SKILLS_DIR
    else Path(
        os.getenv("OPEN_CLANK_DATA_DIR")
        or _OPEN_CLANK_ROOT / "data"
    )
)

server = ToolServer("lifetools")


def _build_tool_list() -> list[Tool]:
    """Convert FUNCTION_TOOL_SCHEMAS minus exclusion set to mcp.types.Tool."""
    tools = []
    for schema in FUNCTION_TOOL_SCHEMAS:
        func = schema.get("function", {})
        name = func.get("name", "")
        if not name or name in _ALL_EXCLUDED:
            continue
        if name in {"browser_status", "browser_observe"} and not _BROWSER_CONTROL_ENABLED:
            continue
        if name == "browser_control" and not (_BROWSER_CONTROL_ENABLED and _BROWSER_MUTATIONS_ENABLED):
            continue
        tools.append(
            Tool(
                name=name,
                description=func.get("description", ""),
                inputSchema=func.get("parameters", {"type": "object", "properties": {}}),
            )
        )
    return tools


_BRIDGED_TOOLS = _build_tool_list()

# A1.3: dedicated usage-recording tool — not from FUNCTION_TOOL_SCHEMAS.
_RECORD_USAGE_TOOL = Tool(
    name="record_skill_usage",
    description="Validate and record an exact active Open Clank skill revision.",
    inputSchema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The skill name (slug)"},
            "skill_id": {"type": "string"},
            "revision": {"type": "integer", "minimum": 1},
            "content_hash": {"type": "string"},
            "asset_profile": {"type":"string", "enum":["external-skill-source-v1"]},
            "source_sha256": {"type":"string", "pattern":"^[0-9a-f]{64}$"},
        },
        "required": ["name", "skill_id", "revision", "content_hash"],
    },
)

async def _ensure_code_provider():
    """Bind search_code to its authenticated host code-query broker."""
    global _MEMORY_PROVIDER, _MEMORY_PROVIDER_LOCK
    if not _OWNER.strip() or not os.environ.get("OPEN_CLANK_MEMORY_BROKER_URL"):
        raise RuntimeError("Code query broker unavailable")
    if _MEMORY_PROVIDER_LOCK is None:
        _MEMORY_PROVIDER_LOCK = asyncio.Lock()
    async with _MEMORY_PROVIDER_LOCK:
        if _MEMORY_PROVIDER is None:
            from src.memery_provider import MemeryProvider
            from src.memory_scope import chat_workspace
            from src.ai_interaction import set_memory_manager

            workspace = os.environ.get("FM_WORKSPACE_ID", "").strip() or chat_workspace()
            provider = MemeryProvider(workspace_id=workspace,
                env={"FM_SCOPE_AUTHORITY": "trusted-caller", "FM_OWNER": _OWNER, "FM_WORKSPACE_ID": workspace})
            await provider.initialize()
            set_memory_manager(None, provider=provider)
            _MEMORY_PROVIDER = provider
    return _MEMORY_PROVIDER


def _record_usage(
    name: str,
    *,
    skill_id: str = "",
    revision: int | None = None,
    content_hash: str = "",
    asset_profile: str | None = None,
    source_sha256: str | None = None,
    _asset_authorized: bool = False,
) -> dict:
    """Record usage; fixed-source reads require a separate turn proof."""
    # Raw MCP calls do not prove the native skill-use admission.
    if (asset_profile is not None or source_sha256 is not None) and _asset_authorized is not True: return {"ok": False}
    try:
        if not _OWNER.strip() or not str(name or "").strip():
            return {"ok": False}
        from services.memory.skills import SkillsManager

        manager = SkillsManager(_DATA_DIR)
        requested = str(name).strip()
        expected_id = str(skill_id or "").strip()
        expected_hash = str(content_hash or "").strip()
        if not expected_id or revision is None or not expected_hash:
            return {"ok": False}
        skill = next(
            (
                row for row in manager.load_published(owner=_SKILL_OWNER)
                if requested in (
                    str(row.get("name") or ""),
                    str(row.get("skill_id") or ""),
                )
            ),
            None,
        )
        if skill is None:
            return {"ok": False}
        if (
            expected_id != str(skill.get("skill_id") or "")
        ) or (
            int(revision) != int(skill["revision"])
        ) or (
            expected_hash != str(skill.get("content_hash") or "")
        ):
            return {"ok": False}
        result = manager.record_active_use(
            str(skill.get("skill_id") or skill["name"]),
            owner=_SKILL_OWNER,
            revision=int(revision),
            content_hash=expected_hash,
        )
        if not result or not result.get('ok'): return {"ok": False}
        if asset_profile is None and source_sha256 is None: return result
        if asset_profile != 'external-skill-source-v1' or type(source_sha256) is not str:
            return {"ok": False}
        asset = manager.read_published_external_skill_source(name=requested,owner=_SKILL_OWNER,
            skill_id=expected_id,revision=revision,content_hash=expected_hash,source_sha256=source_sha256)
        if asset is None: return {"ok": False}
        return {**result,'source_asset':{'profile':asset_profile,'sha256':source_sha256,'text':asset}}
    except Exception as e:
        logger.debug("record_skill_usage failed (non-fatal): %s", e)
        return {"ok": False}


_PYTHON_KERNEL_TOOL = Tool(
    name="python_kernel",
    description="Run Python cells in an admitted session-scoped process. Optional explicit PNG output uses an authorized Files destination; emit_artifact(bytes, 'image/png') selects one image.",
    inputSchema={"type":"object", "additionalProperties":False, "required":["operation"],
        "properties":{"operation":{"type":"string","enum":["start","status","execute","interrupt","reset","close"]},
        "kernel_id":{"type":"string","minLength":1,"maxLength":128},"generation":{"type":"integer","minimum":1},
        "cell_id":{"type":"string","minLength":1,"maxLength":128},"code":{"type":"string","maxLength":65536},
        "timeout":{"type":"number","minimum":0.1,"maximum":120},"output_limit":{"type":"integer","minimum":0,"maximum":262144},
        "artifact_output":{"type":"object","additionalProperties":False,"required":["destination_ref","name"],"properties":{"destination_ref":{"type":"string","minLength":1,"maxLength":16384},"name":{"type":"string","minLength":1,"maxLength":240}}}}},
)


async def _qualify_project_policy(meta: Any) -> Optional[dict[str, Any]]:
    """Optional policy-only use of the existing authenticated loopback broker."""
    try:
        from urllib.parse import urlparse
        import httpx
        from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_OWNER_HEADER, INTERNAL_TOOL_WORKSPACE_HEADER
        from src.memory_scope import chat_workspace
        if type(meta) is not dict or set(meta)!={'identity','invocationID','toolName','toolCallID'}: return None
        url=os.environ.get('OPEN_CLANK_MEMORY_BROKER_URL','').strip()
        token=os.environ.get('OPEN_CLANK_MEMORY_BROKER_TOKEN','').strip()
        workspace_id=os.environ.get('FM_WORKSPACE_ID','').strip() or chat_workspace()
        parsed=urlparse(url)
        if parsed.scheme!='http' or parsed.hostname not in {'127.0.0.1','::1','localhost'} or not token: return None
        async with httpx.AsyncClient(timeout=3.0,trust_env=False,follow_redirects=False) as client:
            response=await client.post(url,headers={INTERNAL_TOOL_HEADER:token,INTERNAL_TOOL_OWNER_HEADER:_OWNER.strip(),
                INTERNAL_TOOL_WORKSPACE_HEADER:workspace_id},json={'name':'qualified_project_policy',
                'arguments':{**meta,'workspace':_WORKSPACE}})
        if response.status_code!=200 or len(response.content)>8192: return None
        result=response.json()
        if type(result) is not dict or result.get('version')!=1 or result.get('identity')!=meta['identity'] or any(result.get(k)!=meta[k] for k in ('invocationID','toolName','toolCallID')): return None
        return result
    except Exception:
        return None


async def _qualify_skill_use(meta: Any) -> Optional[dict[str, Any]]:
    """Optional policy-only use of the existing authenticated loopback broker."""
    try:
        from urllib.parse import urlparse
        import httpx
        from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_OWNER_HEADER, INTERNAL_TOOL_WORKSPACE_HEADER
        from src.memory_scope import chat_workspace
        if type(meta) is not dict or set(meta)!={'identity','invocationID','toolName','toolCallID'} or meta.get('toolName')!='skill': return None
        url=os.environ.get('OPEN_CLANK_MEMORY_BROKER_URL','').strip()
        token=os.environ.get('OPEN_CLANK_MEMORY_BROKER_TOKEN','').strip()
        workspace_id=os.environ.get('FM_WORKSPACE_ID','').strip() or chat_workspace()
        parsed=urlparse(url)
        if parsed.scheme!='http' or parsed.hostname not in {'127.0.0.1','::1','localhost'} or not token: return None
        async with httpx.AsyncClient(timeout=3.0,trust_env=False,follow_redirects=False) as client:
            response=await client.post(url,headers={INTERNAL_TOOL_HEADER:token,INTERNAL_TOOL_OWNER_HEADER:_OWNER.strip(),
                INTERNAL_TOOL_WORKSPACE_HEADER:workspace_id},json={'name':'qualified_skill_use',
                'arguments':{**meta,'workspace':_WORKSPACE}})
        if response.status_code!=200 or len(response.content)>8192: return None
        result=response.json()
        if type(result) is not dict or result.get('version')!=1 or result.get('identity')!=meta['identity'] or any(result.get(k)!=meta[k] for k in ('invocationID','toolName','toolCallID')): return None
        return result
    except Exception:
        return None


async def _broker_control(op: str, identity: Any, extra: dict) -> Any:
    """POST one desktop-control op to the Host loopback broker; the parsed JSON object or None (fail closed)."""
    try:
        from urllib.parse import urlparse
        import httpx
        from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_OWNER_HEADER, INTERNAL_TOOL_WORKSPACE_HEADER
        from src.memory_scope import chat_workspace
        fields = ('sessionID', 'rootOperationID', 'actorID', 'executionRevision', 'innerTurnID')
        if type(identity) is not dict or not all(f in identity for f in fields): return None
        identity = {f: identity[f] for f in fields}
        url = os.environ.get('OPEN_CLANK_MEMORY_BROKER_URL', '').strip()
        token = os.environ.get('OPEN_CLANK_MEMORY_BROKER_TOKEN', '').strip()
        workspace_id = os.environ.get('FM_WORKSPACE_ID', '').strip() or chat_workspace()
        parsed = urlparse(url)
        if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1', 'localhost'} or not token: return None
        async with httpx.AsyncClient(timeout=40.0 if op == "control_selected_snapshot" else 25.0 if op == "control_capture_observation" else 5.0, trust_env=False, follow_redirects=False) as client:
            response = await client.post(url, headers={INTERNAL_TOOL_HEADER: token, INTERNAL_TOOL_OWNER_HEADER: _OWNER.strip(),
                INTERNAL_TOOL_WORKSPACE_HEADER: workspace_id}, json={'name': op, 'arguments': {'identity': identity, **extra}})
        if response.status_code != 200 or len(response.content) > 8192: return None
        result = response.json()
        return result if type(result) is dict and result.get('version') == 1 and result.get('identity') == identity else None
    except Exception:
        return None


async def _consume_control_approval(identity: Any, digest: str, tool: str = 'control_desktop') -> bool:
    """Ask the Host (loopback broker) to consume one human approval bound to this exact call."""
    result = await _broker_control('consume_control_approval', identity, {'toolName': tool, 'argsSHA256': digest})
    return result is not None and result.get('argsSHA256') == digest and result.get('approved') is True


class _RemoteHandle:
    """The Host target state (leases, observations, holds, takeover epoch) reached through the loopback broker `control_lease` op.
    Fails closed: when the broker is unavailable every check refuses and the epoch read raises."""

    def __init__(self, identity: Any) -> None:
        import secrets
        self.identity = identity
        self.helper_gen = secrets.token_hex(8)

    async def _call(self, op: str, **fields: Any) -> dict | None:
        return await _broker_control('control_lease', self.identity, {'request': {'op': op, **fields}})

    async def epoch(self) -> int:
        r = await self._call('epoch')
        if r is None or type(r.get('epoch')) is not int: raise RuntimeError('control state unavailable')
        return r['epoch']

    async def register_helper(self, pid: int, kind: str) -> str:
        r = await self._call('register_helper', pid=pid, kind=kind)
        if not r or r.get('ok') is not True or not isinstance(r.get('registration'), str):
            raise RuntimeError('control helper registration refused')
        return r['registration']

    async def reserve_recording_stream(self, registration: str) -> dict | None:
        r = await self._call("reserve_recording_stream", registration=registration)
        return {k:r[k] for k in ("path", "secret", "host_pid", "host_start")} if r and r.get("ok") is True and all(k in r for k in ("path", "secret", "host_pid", "host_start")) else None

    async def reserve_control_frame(self, registration: str) -> dict | None:
        r=await self._call("reserve_control_frame",registration=registration)
        fields=("path","secret","host_pid","host_start","frame_token")
        return {k:r[k] for k in fields} if r and r.get("ok") is True and all(k in r for k in fields) else None

    async def helper_capabilities(self, registration: str, shortcut: str | None, overlay: bool | None, target: dict | None = None) -> bool:
        r = await self._call('helper_capabilities', registration=registration, shortcut=shortcut, overlay=overlay, target=target)
        return bool(r and r.get('ok') is True and r.get('accepted') is True)

    async def helper_takeover(self, registration: str) -> bool:
        r = await self._call('helper_takeover', registration=registration)
        return bool(r and r.get('ok') is True and r.get('accepted') is True)

    async def reserve_windows_launch(self, kind: str) -> dict:
        r = await self._call('reserve_windows_launch', kind=kind)
        if not r or r.get('ok') is not True:
            raise RuntimeError('control helper launch unavailable')
        return r

    async def complete_windows_launch(self, reservation: str) -> None:
        r = await self._call('complete_windows_launch', reservation=reservation)
        if not r or r.get('ok') is not True:
            raise RuntimeError('control helper proof refused')

    async def cancel_windows_launch(self, reservation: str) -> None:
        await self._call('cancel_windows_launch', reservation=reservation)

    async def reserve_macos_launch(self, kind: str) -> dict:
        r = await self._call('reserve_macos_launch', kind=kind)
        if not r or r.get('ok') is not True: raise RuntimeError('control helper launch unavailable')
        return r

    async def complete_macos_launch(self, reservation: str) -> None:
        r = await self._call('complete_macos_launch', reservation=reservation)
        if not r or r.get('ok') is not True: raise RuntimeError('control helper proof refused')

    async def cancel_macos_launch(self, reservation: str) -> None:
        await self._call('cancel_macos_launch', reservation=reservation)

    async def end_proved_helper(self, reservation: str) -> bool:
        r = await self._call('end_proved_helper', reservation=reservation)
        if not r or r.get('ok') is not True: raise RuntimeError('control helper termination unavailable')
        return r.get('ended') is True

    async def unregister_helper(self, pid: int) -> None:
        await self._call('unregister_helper', pid=pid)

    async def window_observed(self, snapshot_id: str, app: dict, window: dict) -> dict | None:
        r = await self._call('window_observed', snapshot_id=snapshot_id, app=app, window=window, helper_gen=self.helper_gen)
        return r.get('lease') if r else None

    async def capture_observed(self, width: int, height: int, resource: str = '', target: dict | None = None) -> dict | None:
        r = await self._call('capture_observed', width=width, height=height, resource=resource, target=target)
        return r.get('observation') if r else None

    async def precheck(self, call: dict, settings: dict | None) -> str | None:
        r = await self._call('precheck', call=call, settings=settings)
        if r is None: return 'control_state_unavailable'
        return r.get('code') if type(r.get('code')) is str else None

    async def consume_observation(self, call: dict) -> None:
        await self._call('consume_observation', call=call)

    async def selected_snapshot_current(self, registration: str) -> bool:
        r = await _broker_control('control_selected_current', self.identity, {'request': {'registration': registration}})
        return bool(r and r.get('current') is True)

    async def selected_snapshot(self, registration: str, resource_ref: str, expected_revision: dict) -> dict | None:
        r = await _broker_control('control_selected_snapshot', self.identity, {'request': {
            'registration': registration, 'resource_ref': resource_ref, 'expected_revision': expected_revision}})
        result = r.get('result') if isinstance(r, dict) else None
        if (type(result) is not dict or set(result) != {'path', 'lease_max_seconds'}
            or type(result.get('path')) is not str or type(result.get('lease_max_seconds')) is not int):
            return None
        return result

    async def open_resource(self, resource_ref: str, app_id: str) -> dict:
        r = await _broker_control('control_open_resource', self.identity, {'request': {'resource_ref': resource_ref, 'app_id': app_id}})
        return r.get('result') if r and type(r.get('result')) is dict else {'ok': False, 'code': 'files_open_unavailable'}


async def _control_stream_capture(identity: Any) -> Any:
    """Private full-frame capture from the currently registered control helper."""
    from src.openclank.computer_control import linux as control
    from src.openclank.computer_control import protocol as control_protocol
    if not sys.platform.startswith("linux") or not control.S.active():return None
    async with control.S.lock:
        if not control.S.active():return None
        state=_RemoteHandle(identity);registration=control.S.registration
        if not registration:return {"state":"unavailable","code":"control_frame_unavailable"}
        reservation=await state.reserve_control_frame(registration)
        if not reservation:return {"state":"unavailable","code":"control_frame_unavailable"}
        frame_token=reservation.pop("frame_token")
        admitted=False
        try:
            reply=await control.S.exchange(control_protocol.build_frame_handoff(reservation),"frame_handoff",8)
            if not reply or reply.get("ok") is not True:
                if reply is None or reply.get("code")=="invalid_reply":await control.S.close(False)
                return {"state":"unavailable","code":"control_frame_unavailable"}
            result=await _broker_control("control_capture_observation",identity,{"request":{"frame_token":frame_token}})
            body=result.get("result") if isinstance(result,dict) and isinstance(result.get("result"),dict) else {"state":"unavailable","code":"control_frame_unavailable"}
            admitted=body.get("state")=="captured"
            return body
        finally:
            if not admitted:
                try: await state._call("cancel_control_frame",frame_token=frame_token)
                except Exception: pass


async def _note_capture_observation(result: Any, identity: Any) -> Any:
    """After a successful capture_desktop: register it with the Host as an observation and tell the model its id (pixel actions must name it)."""
    # A separate Linux Screenshot chooser cannot identify the active control stream.
    if sys.platform.startswith("linux"):
        return result
    try:
        body = result
        wrapped = None
        if type(result) is dict and type(result.get('output')) is str:
            wrapped, body = result, json.loads(result['output'])
        if type(body) is not dict or body.get('state') != 'captured' or type(identity) is not dict: return result
        size = body.get('pixel_size') or (body.get('resource') or {}).get('pixel_size') or [0, 0]
        w, h = (int(size[0]), int(size[1])) if isinstance(size, (list, tuple)) and len(size) == 2 else (0, 0)
        resource = body.get('resource') if isinstance(body.get('resource'), dict) else {}
        ref = str(resource.get('resource_ref') or resource.get('resource_key') or '')
        if len(ref) > 16384: return result
        captured = body.get('target') if isinstance(body.get('target'), dict) else {}
        target = {'source': body.get('source'), 'id': captured.get('id'), 'bounds': captured.get('bounds')}
        note = await _RemoteHandle(identity).capture_observed(max(w, 1), max(h, 1), ref, target)
        if note is None: return result
        body['observation_id'] = note['observationId']; body['observation_valid_s'] = note['maxAgeS']
        if wrapped is not None:
            return {**wrapped, 'output': json.dumps(body)}
        return body
    except Exception:
        return result


async def _control_settings(identity: Any) -> dict | None:
    """The owner's desktop opt-ins as the Host sees them: {'control': bool, 'accessibility': bool}, or None when unavailable."""
    result = await _broker_control('control_settings', identity, {})
    if result is None or type(result.get('control')) is not bool or type(result.get('accessibility')) is not bool: return None
    return {'control': result['control'], 'accessibility': result['accessibility'], 'action_log': result.get('action_log') is True, 'multi_agent': result.get('multi_agent') is True}


async def _publish_requirement_check(identity: Any) -> dict[str, Any] | None:
    """Ask the Host (loopback broker) whether a standing requirement denies a public link; None = unavailable."""
    try:
        from urllib.parse import urlparse
        import httpx
        from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_OWNER_HEADER, INTERNAL_TOOL_WORKSPACE_HEADER
        from src.memory_scope import chat_workspace
        fields = ('sessionID', 'rootOperationID', 'actorID', 'executionRevision', 'innerTurnID')
        if type(identity) is not dict or not all(f in identity for f in fields): return None
        identity = {f: identity[f] for f in fields}
        url = os.environ.get('OPEN_CLANK_MEMORY_BROKER_URL', '').strip()
        token = os.environ.get('OPEN_CLANK_MEMORY_BROKER_TOKEN', '').strip()
        workspace_id = os.environ.get('FM_WORKSPACE_ID', '').strip() or chat_workspace()
        parsed = urlparse(url)
        if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1', 'localhost'} or not token: return None
        async with httpx.AsyncClient(timeout=5.0, trust_env=False, follow_redirects=False) as client:
            response = await client.post(url, headers={INTERNAL_TOOL_HEADER: token, INTERNAL_TOOL_OWNER_HEADER: _OWNER.strip(),
                INTERNAL_TOOL_WORKSPACE_HEADER: workspace_id}, json={'name': 'publish_requirement_check',
                'arguments': {'identity': identity}})
        if response.status_code != 200 or len(response.content) > 8192: return None
        result = response.json()
        if (type(result) is not dict or result.get('version') != 1 or result.get('identity') != identity
                or not {'deny_public', 'receipt'} <= set(result) <= {'version', 'identity', 'store', 'deny_public', 'receipt'}):
            return None
        return {k: v for k, v in result.items() if k != 'identity'}
    except Exception:
        return None


@server.list_tools()
async def list_tools() -> list[Tool]:
    return _BRIDGED_TOOLS + [_RECORD_USAGE_TOOL] + ([_PYTHON_KERNEL_TOOL] if os.environ.get("OPEN_CLANK_MANAGED_PYTHON_KERNEL") == "1" else [])


@server.call_tool()
async def call_tool(
    name: str, arguments: dict
) -> list[TextContent] | CallToolResult:
    if name == "shared_text":
        # Only the managed Host interception carries the original actor authority.
        return [TextContent(type="text", text=json.dumps({"error":"Managed shared text unavailable", "exit_code":1}))]
    if name == "media_job":
        # Only the managed Host interception carries the original C2 authority.
        return [TextContent(type="text", text=json.dumps({"error":"Managed media jobs unavailable", "exit_code":1}))]
    if name == "python_kernel":
        return [TextContent(type="text", text=json.dumps({"error":"Managed Python kernel unavailable", "exit_code":1}))]
    if name == "theme_emoji_assets":
        # This private adapter derives identity from the server-owned session,
        # never from model arguments. Mutating operations stay unavailable until
        # the consent-fenced acquisition service is connected.
        args = arguments if isinstance(arguments, dict) else {}
        if set(args) != {'operation'} or args['operation'] not in {'status', 'discover', 'download', 'cancel'}:
            return [TextContent(type='text', text=json.dumps({'error':'Invalid operation', 'exit_code':1}))]
        if args['operation'] != 'status':
            return [TextContent(type='text', text=json.dumps({'error':'Emoji acquisition is unavailable', 'exit_code':1}))]
        # Consent is owned by the app data root, not this process's private
        # data directory. Host dispatch must be connected before status is authoritative.
        return [TextContent(type='text', text=json.dumps({'error':'Emoji status unavailable', 'exit_code':1}))]
    if name == "_host_candidate_review":
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({"error": "host-private operation unavailable", "exit_code": 1}))], isError=True)
    if name in {"browser_status", "browser_observe", "browser_control"}:
        if name in {"browser_status", "browser_observe"} and not _BROWSER_CONTROL_ENABLED:
            return [TextContent(type="text", text=json.dumps({"error": "Browser is unavailable for this managed session", "exit_code": 1}))]
        if name == "browser_control" and not (_BROWSER_CONTROL_ENABLED and _BROWSER_MUTATIONS_ENABLED):
            return [TextContent(type="text", text=json.dumps({"error": "Browser actions are disabled for this managed session", "exit_code": 1, "blocked": True}))]
        try:
            from src.openclank.browser_control_broker import BrowserControlBrokerClient
            meta = server.request_context.meta
            payload = (getattr(meta, "model_extra", None) or {}).get("com.xiaomi.mimo/turn-lifecycle")
            fields = ("sessionID", "rootOperationID", "actorID", "executionRevision", "innerTurnID")
            identity = {key: payload[key] for key in fields} if isinstance(payload, dict) and all(key in payload for key in fields) else None
            result = await BrowserControlBrokerClient().call(name, dict(arguments or {}), managed_identity=identity)
        except Exception:
            result = {"error": "Browser broker is unavailable", "exit_code": 1}
        images = []
        if isinstance(result, dict):
            result = dict(result)
            nested = result.get("result")
            if isinstance(nested, dict):
                nested = dict(nested)
                raw_images = nested.pop("images", [])
                result["result"] = nested
                if isinstance(raw_images, list):
                    for row in raw_images[:4]:
                        if (isinstance(row, dict) and isinstance(row.get("data"), str)
                                and row.get("mimeType") in {"image/png", "image/jpeg", "image/webp"}
                                and len(row["data"]) <= 12 * 1024 * 1024):
                            images.append(ImageContent(type="image", data=row["data"], mimeType=row["mimeType"]))
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False)), *images]

    # Private runtime bridge used by MiMo's native mutators.  It is deliberately
    # absent from list_tools(): the model cannot select scope or invoke policy;
    # the session-bound tool implementations submit exact candidate bytes.
    if name == _PROJECT_MUTATION_POLICY_TOOL:
        try:
            if not _OWNER.strip() or not _WORKSPACE.strip():
                raise PermissionError(
                    "project policy requires an authenticated owner and workspace"
                )
            mode = str((arguments or {}).get("mode") or "").strip()
            from src.constants import FM_DB_PATH

            if mode == "files":
                from src.project_hex import validate_registered_project_candidates

                rows = (arguments or {}).get("candidates")
                if not isinstance(rows, list) or not rows:
                    raise ValueError("file policy requires exact candidates")
                candidates = {}
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError("file policy candidate must be an object")
                    path = str(row.get("path") or "").strip()
                    if not path or path in candidates:
                        raise ValueError("file policy candidate path is missing or duplicated")
                    if row.get("deleted") is True:
                        if row.get("content_base64") is not None:
                            raise ValueError("deleted candidate cannot include content")
                        candidates[path] = None
                        continue
                    encoded = row.get("content_base64")
                    if not isinstance(encoded, str):
                        raise ValueError("file policy candidate content is missing")
                    candidates[path] = base64.b64decode(encoded, validate=True)
                from src.project_hex import project_policy_recovery_snapshot
                meta = (getattr(server.request_context.meta, 'model_extra', None) or {}).get('com.openclank.policy-invocation')
                qualified = await _qualify_project_policy(meta) if meta is not None else None
                before = None
                if qualified is not None:
                    try: before = project_policy_recovery_snapshot(owner=_OWNER.strip(),workspace=_WORKSPACE,db_path=FM_DB_PATH,candidates=candidates)
                    except Exception: pass
                result = validate_registered_project_candidates(
                    owner=_OWNER.strip(), workspace=_WORKSPACE, db_path=FM_DB_PATH, candidates=candidates,
                )
                if qualified is not None and before is not None and result.get('allowed') is True:
                    try:
                        after = project_policy_recovery_snapshot(owner=_OWNER.strip(),workspace=_WORKSPACE,db_path=FM_DB_PATH,candidates=candidates)
                        again = await _qualify_project_policy(meta)
                        if before == after and qualified == again:
                            import hashlib
                            rows = [[path,None if data is None else hashlib.sha256(data).hexdigest()] for path,data in candidates.items()]
                            input_hash = hashlib.sha256(json.dumps(rows,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
                            result = {**result,'recovery_evidence':{'version':1,'qualification':qualified,'policy':before,'inputSHA256':input_hash}}
                    except Exception:
                        pass
            elif mode == "shell":
                from src.project_hex import inspect_registered_project_mutation_policy

                result = inspect_registered_project_mutation_policy(
                    owner=_OWNER.strip(),
                    workspace=_WORKSPACE,
                    db_path=FM_DB_PATH,
                )
                result = {
                    **result,
                    "allowed": not bool(result.get("enforced")),
                    "reason": (
                        "active project policy blocks MiMo shell execution; use native file tools"
                        if result.get("enforced")
                        else ""
                    ),
                }
            else:
                raise ValueError("project policy mode must be files or shell")
            from src.project_hex import global_policy_context

            # This is a policy/system projection, never a memory or RAG hit.
            # Keep it alongside the private mutation response so the agent can
            # explain the canonical boundary without being able to alter it.
            result = {
                **result,
                "policy_context": global_policy_context(
                    owner=_OWNER.strip(),
                    workspace=_WORKSPACE,
                    db_path=FM_DB_PATH,
                ),
            }
        except Exception as exc:
            logger.error("lifetools project policy dispatch failed: %s", exc)
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {"error": "project policy unavailable", "exit_code": 1}
                        ),
                    )
                ],
                isError=True,
            )
        return [TextContent(type="text", text=json.dumps(result))]

    # A1.3: handle usage recording directly (not via execute_tool_block)
    if name == "record_skill_usage":
        asset_requested = arguments.get('asset_profile') is not None or arguments.get('source_sha256') is not None
        if asset_requested:
            meta=(getattr(server.request_context.meta,'model_extra',None) or {}).get('com.openclank.skill-invocation')
            before=await _qualify_skill_use(meta)
            if before is None: return [TextContent(type="text",text=json.dumps({"ok":False}))]
        result = _record_usage(
            arguments.get("name", ""), skill_id=arguments.get("skill_id", ""),
            revision=arguments.get("revision"), content_hash=arguments.get("content_hash", ""),
            asset_profile=arguments.get("asset_profile"), source_sha256=arguments.get("source_sha256"),
            _asset_authorized=asset_requested,
        )
        if asset_requested and (not result.get('ok') or await _qualify_skill_use(meta) != before):
            result={"ok":False}
        return [TextContent(type="text", text=json.dumps(result))]

    if name in {"read_copal", "manage_copal"}:
        scoped = dict(arguments or {})
        requested_workspace = str(scoped.get("workspace") or "").strip()
        if requested_workspace and requested_workspace != _COPAL_WORKSPACE:
            return [TextContent(text=json.dumps({
                "error": "Copal workspace does not match the execution's pinned workspace",
                "code": "copal_workspace_mismatch",
                "expected": _COPAL_WORKSPACE,
                "received": requested_workspace,
                "exit_code": 1,
            }))]
        scoped["workspace"] = _COPAL_WORKSPACE
        arguments = scoped

    if name in {"control_desktop", "control_desktop_status", "inspect_desktop"}:
        from src.openclank.computer_control import linux as control
        if name == "control_desktop_status":
            result = await control.control_status(arguments)
        else:
            payload = (getattr(server.request_context.meta, "model_extra", None) or {}).get("com.xiaomi.mimo/turn-lifecycle")
            async def approve(digest: str) -> bool:
                return await _consume_control_approval(payload, digest, name)
            # The owner's settings decide first, with a typed reason; None (broker unavailable) leaves the consume check to refuse.
            allow = await _control_settings(payload)
            handle = _RemoteHandle(payload) if type(payload) is dict else None
            if name == "inspect_desktop":
                from src.openclank.computer_control import a11y_client
                result = await a11y_client.inspect_action(arguments, approve, allow, handle)
            else:
                result = await control.control_action(arguments, approve, allow, handle)
        return [TextContent(type="text", text=json.dumps(result))]

    from src.tool_execution import execute_tool_block

    if name in _ALL_EXCLUDED:
        return [TextContent(
            type="text",
            text=json.dumps({"error": f"Tool '{name}' is not available via the bridge.", "exit_code": 1}),
        )]

    outline_holder = outline_token = publish_token = None
    try:
        if name == "publish_file":
            # Only a public link is ever checked (PublishFileTool); the identity is the engine's, never the model's.
            from src.tool_execution import PUBLISH_REQUIREMENT_CHECKER
            payload = (getattr(server.request_context.meta, "model_extra", None) or {}).get("com.xiaomi.mimo/turn-lifecycle")
            async def _publish_check():
                return await _publish_requirement_check(payload)
            publish_token = PUBLISH_REQUIREMENT_CHECKER.set(_publish_check)
        from src.agent_tools import ToolBlock
        block = ToolBlock(tool_type=name, content=json.dumps(arguments))
        if name == "search_code":
            await _ensure_code_provider()
            meta = getattr(server.request_context.meta, "model_extra", None) or {}
            requested = meta.get("openclank.source-outline-request.v1")
            if isinstance(requested, dict):
                from src.agent_tools.code_tools import begin_source_outline_request
                outline_holder, outline_token = begin_source_outline_request(requested)
        frame_context_token=None
        if name == "capture_desktop" and sys.platform.startswith("linux"):
            from src.desktop_capture import CONTROL_FRAME_CAPTURE
            frame_identity=(getattr(server.request_context.meta, "model_extra", None) or {}).get("com.xiaomi.mimo/turn-lifecycle")
            frame_context_token=CONTROL_FRAME_CAPTURE.set(lambda: _control_stream_capture(frame_identity))
        try:
            description, result = await execute_tool_block(
            block,
            session_id=_SESSION_ID,
            disabled_tools=set(),  # mimo is the sole permission gate
            owner=_OWNER,
            workspace=_WORKSPACE,
            authority_workspace_id=_AUTHORITY_WORKSPACE_ID,
            copal_workspace=_COPAL_WORKSPACE,
            )
        finally:
            if frame_context_token is not None:
                CONTROL_FRAME_CAPTURE.reset(frame_context_token)
            if publish_token is not None:
                from src.tool_execution import PUBLISH_REQUIREMENT_CHECKER
                PUBLISH_REQUIREMENT_CHECKER.reset(publish_token)
            if outline_holder is not None:
                from src.agent_tools.code_tools import end_source_outline_request
                end_source_outline_request(outline_holder, outline_token)
    except Exception as e:
        if name == "search_code":
            from src.openclank.code_query import finalize_error
            result = {"output": json.dumps(finalize_error(arguments, "unavailable", "Code query unavailable"), separators=(",", ":")), "exit_code": 1}
            logger.error("lifetools search_code dispatch unavailable")
        else:
            logger.error("lifetools dispatch error for %s: %s", name, e, exc_info=True)
            result = {"error": str(e), "exit_code": 1}

    if name == "capture_desktop":
        result = await _note_capture_observation(result, (getattr(server.request_context.meta, "model_extra", None) or {}).get("com.xiaomi.mimo/turn-lifecycle"))
    search_hits = []
    if name == "grep" and result.get("exit_code") == 0:
        file_result = (result.get("metadata") or {}).get("file") or {}
        if file_result.get("contract") == "open-clank.file-result/v1" and file_result.get("operation") == "grep":
            for item in file_result.get("items") or []:
                hit = item.pop("search_hit", None)
                if hit is not None:
                    search_hits.append({"label": hit["label"], "ref": hit["ref"],
                                        "line": hit["line"], "fingerprint": hit["revision"]["value"]})
    text = json.dumps(result)
    if search_hits:
        # MCP _meta stays out of the tool's model-facing text. The renderer
        # receives only opaque Files refs; opening still reauthorizes the hit.
        return CallToolResult(content=[TextContent(type="text", text=text)],
                              _meta={"openclank.grep-links.v1": {"version": 1, "hits": search_hits}})
    if name == "search_code" and outline_holder is not None and isinstance(outline_holder.get("sidecar"), dict) and result.get("exit_code") == 0:
        import hashlib
        requested = outline_holder["request"]
        candidate = outline_holder["sidecar"]
        sidecar = {"version": 1, "toolCallID": requested.get("toolCallID"),
                   "outputSHA256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                   "outline": candidate.get("result"), "extraCost": candidate.get("extraCost")}
        return CallToolResult(content=[TextContent(type="text", text=text)], _meta={"openclank.source-outline.v1": sidecar})
    return [TextContent(type="text", text=text)]


async def main():
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options(experimental_capabilities={"com.xiaomi.mimo/turn-lifecycle": {"version": 1, "notifications": False}}))
    finally:
        if _MEMORY_PROVIDER is not None:
            await _MEMORY_PROVIDER.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
