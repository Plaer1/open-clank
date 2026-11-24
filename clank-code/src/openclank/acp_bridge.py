"""ACP bridge — maps mimo session/update notifications to odysseus chat SSE strings."""

import asyncio
import base64
import copy
import contextvars
import binascii
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import secrets
import sys
import time
import threading
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Coroutine, Dict, List, Optional

from src.memory_scope import chat_workspace, memory_owner
from src.openclank.acp_client import ACPClient, RPCError, TransportError
from src.openclank.chat_routing import MANAGED_ENGINE_PUBLIC_URL
from src.openclank.permission_grants import derive_pattern
from src.openclank.session_map import OwnerSessionMap, _EXPECTED_UNSET

logger = logging.getLogger(__name__)


_maintenance_finalizer_context = contextvars.ContextVar('maintenance_finalizer', default=None)


class _MaintenanceFinalizer:
    """One invocation's final fresh recall, never a reusable authority."""
    def __init__(self, bridge, identity, original, boundary, request, deadline):
        self.args = (bridge, identity, original, boundary, request)
        self.snapshots = copy.deepcopy((identity, original, boundary, request))
        self.task = asyncio.current_task()
        self.deadline = deadline
        self.active = True
        self.claimed = False
        self.checked = False

    def current(self):
        return (self.active and asyncio.current_task() is self.task
                and asyncio.get_running_loop().time() < self.deadline
                and self.args[1:] == self.snapshots)

    async def finish(self):
        if not self.current() or not self.claimed or self.checked:
            raise ValueError('maintenance finalizer unavailable')
        bridge, identity, original, _, _ = self.args
        # Seal only a successful fresh resolve of this exact original turn.
        if await bridge.resolve_managed_recall_turn(identity) != original or not self.current():
            raise ValueError('maintenance source changed')
        self.checked = True


def _claim_maintenance_finalizer(bridge, identity, original, boundary, request):
    marker = _maintenance_finalizer_context.get()
    if marker is None:
        return None
    if (not marker.current() or marker.claimed
            or any(actual is not expected for actual, expected in zip(
                (bridge, identity, original, boundary, request), marker.args))):
        raise ValueError('maintenance finalizer unavailable')
    marker.claimed = True
    return marker.finish


def _adaptive_style_admission(*, bridge, state, lease, preference, style, owner, account, chat, session, root, route, wire, auth, incognito, auxiliary):
    """Optional original-turn evidence; grants no permission or model authority."""
    from src.agent_settings import AdmittedGcfPreference
    from src.default_persona import read_style_turn
    from src.openclank.mimo_supervisor import AgentWorkerLease
    try:
        if (type(preference) is not AdmittedGcfPreference or preference.adaptive_verbosity_requested is not True
                or type(lease) is not AgentWorkerLease or lease._released or lease.owner != owner
                or getattr(lease.worker, '_bridge', None) is not bridge or lease._pool.worker_for_owner(owner) is not lease.worker
                or not lease.worker.is_alive() or getattr(lease.worker, '_proc', None) is None
                or getattr(lease.worker, '_client', None) is not bridge._client
                or getattr(lease.worker, 'installed_generation', None) != lease.generation
                or getattr(getattr(lease.worker, '_managed_callbacks', None), 'holder_id', None) != bridge._managed_provider_generation
                or not bridge._managed_provider_generation or lease._pool._owner_lifecycle_epoch(owner) != lease._owner_epoch
                or incognito or auxiliary or not account or auth is None
                or bridge._turns.get(session) is not state or state.root_operation_id != root
                or state.owner_account_id != account
                or (bridge._session_context.get(session) or {}).get('root_operation_id') != root
                or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                or preference.provider_id not in (wire.get('providerID'), wire.get('connectionID'))
                or preference.model_id != wire.get('modelID')):
            return None
        manual = read_style_turn(style, owner=owner, account_id=account, chat_id=chat, root_operation_id=root)
        if not manual or manual['mode'] != 'normal': return None
        route_key = json.dumps([route.provider_id, route.adapter_id, route.connection_kind, route.billing_lane], ensure_ascii=False, separators=(',', ':'))
        result = {'version':1, 'requested':True, 'sessionID':session, 'rootOperationID':root,
                  'hostAccountID':account, 'holderID':bridge._managed_provider_generation, 'generation':lease.generation,
                  'settingsRevision':preference.revision, 'providerID':wire.get('providerID'), 'modelID':wire.get('modelID'),
                  'connectionID':wire.get('connectionID'), 'modelRouteID':wire.get('modelRouteID'), 'routeMethodKey':route_key,
                  'manualMode':'normal', 'manualRevision':manual['revision']}
        strings = ('sessionID','rootOperationID','hostAccountID','holderID','providerID','modelID','connectionID','modelRouteID','routeMethodKey')
        if (any(type(result[k]) is not str or not 0 < len(result[k]) <= 256 for k in strings)
                or any(type(result[k]) is not int or not 0 <= result[k] <= 9007199254740991 for k in ('generation','manualRevision'))
                or type(result['settingsRevision']) is not str or re.fullmatch(r'[0-9a-f]{64}',result['settingsRevision']) is None): return None
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
        return result
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError, OverflowError):
        return None


class _ActorLoggingOutcomeUnknown(RuntimeError):
    pass


_PROVIDER_TRANSACTION_SEAL = object()


class _ProviderTransactionCancelled(asyncio.CancelledError):
    """The worker settled after cancellation; preserve its exact outcome."""
    def __init__(self, *, returned, result=None, error=None):
        super().__init__('managed provider transaction cancelled')
        self.returned, self.result, self.error = returned, result, error


@dataclass(frozen=True)
class _ManagedProviderTransactionAdmission:
    owner: str
    account_id: str
    chat_id: str
    session_id: str
    root_id: str
    actor_id: str
    inner_turn_id: str
    execution_revision: int
    holder_id: str
    generation: int
    owner_epoch: int
    provider_id: str
    model_id: str
    option_context_json: str
    _callback: object = field(repr=False, compare=False)
    _current: Callable[[], bool] = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)
    _root: object = field(default=None, repr=False, compare=False)
    _attachment: object = field(default=None, repr=False, compare=False)

    def assert_current(self):
        if self._seal is not _PROVIDER_TRANSACTION_SEAL or self._current() is not True:
            raise ValueError('managed provider transaction authority changed')


@dataclass(frozen=True)
class _ManagedProviderRootTransactionAdmission:
    owner: str
    account_id: str
    chat_id: str
    session_id: str
    root_id: str
    holder_id: str
    generation: int
    owner_epoch: int
    option_context_json: str
    _root: object = field(repr=False, compare=False)
    _callback: object = field(repr=False, compare=False)
    _current: Callable[[], bool] = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def assert_current(self):
        if self._seal is not _PROVIDER_TRANSACTION_SEAL or self._current() is not True:
            raise ValueError('managed provider root authority changed')

# Debug switch — gate verbose bridge logging when OPEN_CLANK_DEBUG is set.
# Import inline to avoid circular dependency at module load.
def _debug_enabled() -> bool:
    try:
        from src.constants import is_openthesius_debug
        return is_openthesius_debug()
    except Exception:
        return False

# Path to the lifetools MCP server script
_LIFETOOLS_SERVER = Path(__file__).resolve().parent / "lifetools_server.py"

_MEMORY_BROKER_URL = ""
_MEMORY_BROKER_TOKEN = ""
_COPAL_BROKER_URL = ""
_COPAL_BROKER_TOKEN = ""

_NATIVE_MEDIA_SOURCE_BYTES = 150 * 1024 * 1024
_NATIVE_MEDIA_PAYLOAD_BYTES = 50 * 1024 * 1024


def configure_memery_broker(url: str, token: str) -> None:
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


def memery_broker_token(
    master_token: str, owner: str, workspace_id: str
) -> str:
    owner = str(owner or "").strip()
    workspace_id = str(workspace_id or "").strip()
    if not master_token or not owner or not workspace_id:
        raise ValueError("memory broker scope is incomplete")
    payload = f"open-clank-memory-broker-v1\0{owner}\0{workspace_id}".encode()
    return hmac.new(master_token.encode(), payload, hashlib.sha256).hexdigest()


def memery_child_env() -> dict[str, str]:
    """Project only Memery runtime configuration into child processes."""
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

    return env


def _lifetools_launch_account(owner: str) -> str:
    """Capture a verified immutable human subject before launching LifeTools."""
    try:
        from core.models import get_session_manager_instance
        manager = get_session_manager_instance()
        auth = getattr(getattr(manager, 'lifecycle_context', None), 'auth_manager', None)
        username = str(owner or '').strip().lower()
        account = auth.account_id(username) if auth and username else None
        if not account or auth.username_for_account_id(account) != username:
            return ''
        return str(account)
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return ''


def lifetools_mcp_descriptor(
    owner: str = "",
    session_id: str = "",
    workspace: str = "",
    authority_workspace_id: str = "",
    memory_workspace_id: str = "",
    memory_enabled: bool = True,
    browser_control_enabled: bool = False,
    browser_mutations_enabled: bool = False,
    copal_workspace: str = "default",
    engine_session_aliases: Optional[list[str]] = None,
    binding_revision: int = 0,
    map_revision: int = 0,
    mapping_revision: int = 0,
    authority_account_id: str = "",
    python_kernel_enabled: bool = False,
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
        "OPEN_CLANK_AUTHORITY_ACCOUNT_ID": str(authority_account_id or ""),
        "OPEN_CLANK_MANAGED_PYTHON_KERNEL": "1" if python_kernel_enabled else "0",
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
        **memery_child_env(),
    }
    # The managed engine and its lifetools process use an owner-private data
    # directory. Stable Workspace authority, however, lives in the app's
    # canonical policy/auth stores. Project their paths explicitly so the
    # trusted bridge can re-resolve the current immutable owner + Workspace on
    # every tool call instead of trusting the baked physical cwd.
    from src.constants import APP_DB, AUTH_FILE

    authority_db = os.environ.get("OPEN_CLANK_AUTHORITY_DB_PATH") or APP_DB
    authority_auth = os.environ.get("OPEN_CLANK_AUTHORITY_AUTH_PATH") or AUTH_FILE
    if not os.path.isabs(authority_db) or not os.path.isabs(authority_auth):
        raise ValueError("authority store paths must be absolute")
    child_env.update(
        {
            "OPEN_CLANK_AUTHORITY_DB_PATH": authority_db,
            "OPEN_CLANK_AUTHORITY_AUTH_PATH": authority_auth,
            "OPEN_CLANK_MANAGED_AGENT_HISTORY_REQUIRED": "1",
        }
    )
    files_binary = os.environ.get("OPEN_CLANK_FILES_SERVICE_BIN")
    if files_binary:
        if not os.path.isabs(files_binary):
            raise ValueError("Files service path must be absolute")
        child_env["OPEN_CLANK_FILES_SERVICE_BIN"] = files_binary
    # The lifetools process owns its Copal adapter, so carry the app's
    # partitioned store into that process as well.  Without this, a
    # disposable/partitioned app falls back to the repository default and
    # collides with another running Copal bridge before the first tool call.
    copal_root = os.environ.get("COPAL_LOOSE_ROOT", "").strip()
    if copal_root:
        child_env["COPAL_LOOSE_ROOT"] = copal_root
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
        from src.openclank.code_query import code_binding_token
        child_env.update(
            {
                "OPEN_CLANK_MEMORY_BROKER_URL": _MEMORY_BROKER_URL,
                "OPEN_CLANK_MEMORY_BROKER_TOKEN": memery_broker_token(
                    _MEMORY_BROKER_TOKEN, owner, workspace_id
                ),
                **({"OPEN_CLANK_CODE_BINDING_TOKEN": code_binding_token(_MEMORY_BROKER_TOKEN, owner=owner, session_id=session_id, workspace_id=authority_workspace_id, binding_revision=max(0, int(binding_revision)))} if session_id and authority_workspace_id else {}),
            }
        )
    from src.openclank.browser_control_broker import configured_browser_control_descriptor_env
    child_env.update(configured_browser_control_descriptor_env(
        owner=owner,
        host_session_id=session_id,
        authority_workspace_id=authority_workspace_id,
        binding_revision=max(0, int(binding_revision)),
        enabled=bool(browser_control_enabled),
        mutations_enabled=bool(browser_mutations_enabled),
    ))
    return {
        "name": f"lifetools_{scope}",
        "command": sys.executable,
        "args": [str(_LIFETOOLS_SERVER)],
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


class _BindingCompensationNeedsFence(RuntimeError):
    """A failed binding compensation must be fenced before surfacing its error."""

    def __init__(self, chat_id: str, reason: str, original: Exception):
        super().__init__(reason)
        self.chat_id = chat_id
        self.reason = reason
        self.original = original


class _ThemeEmojiReservation:
    """One host-private authorization consumed at the active-turn boundary."""
    __slots__ = ('_identity', '_owner', '_account', '_operation', '_policy', '_consumed', '_claimed')

    def __init__(self, identity, owner, account, operation, policy):
        self._identity = tuple(identity)
        self._owner, self._account, self._operation, self._policy = owner, account, operation, policy
        self._consumed = False
        self._claimed = False

    def consume(self):
        if self._consumed:
            raise ValueError('theme emoji reservation already consumed')
        self._consumed = True

    def assert_committed(self, owner, account_id, operation):
        if (not self._consumed or self._claimed
                or (owner, account_id, operation) != (self._owner, self._account, self._operation)):
            raise ValueError('theme emoji reservation unavailable')
        self._claimed = True


def _current_media_offer(entry, holder):
    try:
        value=entry.get('mediaSourcesV1'); current=entry.get('originalCredentialCurrent')
        return value if value and entry.get('state')=='active' and entry.get('holderID')==holder and callable(current) and current() is True else None
    except Exception:
        return None


def _current_media_derivative_identity(bridge, entry, holder):
    """Validate current private identity without encoding derivative bytes."""
    try:
        if (entry.get('state') != 'active' or entry.get('holderID') != holder
                or entry.get('originalIngressRequired') is True or entry.get('actorID') != 'main'
                or entry.get('sessionID') != entry.get('rootSessionID')
                or entry.get('ownerAdmissionStopped') or getattr(entry.get('ownerAdmission'), 'released', True)
                or not callable(entry.get('originalCredentialCurrent')) or entry['originalCredentialCurrent']() is not True):
            return None
        context = entry.get('_mediaViewContext')
        if type(context) is not dict: return None
        root = context['root']
        if (bridge._turns.get(entry['rootSessionID']) is not root or root.provider_root_closing
                or root.privacy_capture_veto or not root.forward_pair_eligible
                or root.root_operation_id != entry['rootOperationID'] or root.owner_account_id != entry['ownerAccountID']
                or root.original_credential_current is not entry['originalCredentialCurrent']): return None
        from src.openclank.media_source_evidence import _read_image_views
        views = _read_image_views(context['token'], owner=entry['owner'], account=entry['ownerAccountID'],
            chat=entry['stableChatID'], root=entry['rootOperationID'])
        if not views: return None
        identities=[]
        for receipt,data in views:
            if context['originals'].get(receipt['uploadID']) != receipt['originalSHA256']: return None
            identities.append((receipt['uploadID'],receipt['originalSHA256'],receipt['derivedSHA256'],receipt['derivedLength']))
        return (context['token'],tuple(identities))
    except Exception:
        return None


def _current_media_derivative_offer(bridge, entry, holder):
    identity = _current_media_derivative_identity(bridge,entry,holder)
    if identity is None: return None
    try:
        from src.openclank.media_source_evidence import _read_image_views
        views = _read_image_views(identity[0],owner=entry['owner'],account=entry['ownerAccountID'],
            chat=entry['stableChatID'],root=entry['rootOperationID'])
        if not views or _current_media_derivative_identity(bridge,entry,holder) != identity: return None
        return {'version':1,'rootOperationID':entry['rootOperationID'],
            'items':[{**receipt,'derivedBase64':base64.b64encode(data).decode('ascii')} for receipt,data in views]}
    except Exception:
        return None


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
        "raw_policy_ceiling",
        "root_operation_id",
        "owner_account_id",
        "original_credential_current",
        "connector_ingress",
        "foreground_attachment",
        "theme_status_only",
        "code_activity_workspace_id",
        "maintenance_prompt_boundary",
        "media_source_offer",
        "media_view_offer",
        "generated_summary_boundary",
        "main_option_candidate",
        "privacy_capture_veto",
        "forward_pair_eligible",
        "forward_pair_witness",
        "terminal_error_witness",
        "terminal_error_finish",
        "factory_persona_source",
        "provider_root_closing",
        "provider_root_callback",
        "provider_root_holder",
        "provider_root_nonce",
        "row_view_preference",
        "browser_snapshot_preference",
        "shell_rewrite_preference",
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
        self.raw_policy_ceiling = None
        self.root_operation_id = None
        self.owner_account_id = None
        self.original_credential_current = None
        self.connector_ingress = False
        self.foreground_attachment = None
        self.theme_status_only = False
        self.code_activity_workspace_id = None
        self.maintenance_prompt_boundary = None
        self.media_source_offer = None
        self.media_view_offer = None
        self.generated_summary_boundary = None
        self.main_option_candidate = None
        self.privacy_capture_veto = False
        self.forward_pair_eligible = False
        self.forward_pair_witness = None
        self.terminal_error_witness = None
        self.terminal_error_finish = None
        self.factory_persona_source = None
        self.provider_root_closing = False
        self.provider_root_callback = None
        self.provider_root_holder = None
        self.provider_root_nonce = None
        self.row_view_preference = None
        self.browser_snapshot_preference = None
        self.shell_rewrite_preference = None


_S07_BOUNDARY_SEAL = object()


class _S07RecallBoundary:
    """Callback-scoped negative currentness, never a permission or wire token."""
    def __init__(self, seal, bridge, identity, entry, before, deadline, recall_prefs=None):
        from types import MappingProxyType
        if seal is not _S07_BOUNDARY_SEAL:
            raise ValueError('S07 boundary unavailable')
        self._bridge = bridge
        self._identity = MappingProxyType(dict(identity))
        self.snapshot = MappingProxyType(dict(before))
        self.recall_prefs = MappingProxyType(dict(recall_prefs)) if recall_prefs is not None else None
        self._entry = entry
        self._holder = bridge._managed_provider_generation
        self._task = asyncio.current_task()
        self._deadline = deadline
        self._active = True

    async def assert_current(self, bridge, identity):
        if (not self._active or bridge is not self._bridge or identity != self._identity
                or asyncio.current_task() is not self._task or asyncio.get_running_loop().time() >= self._deadline
                or not self._holder or bridge._managed_provider_generation != self._holder):
            raise ValueError('S07 boundary changed')
        async with bridge._managed_execution_lock:
            entry = bridge._managed_actor_turns.get(bridge._managed_turn_key(dict(self._identity)))
            if (not self._active or entry is not self._entry or entry.get('state') != 'active'
                    or bridge._managed_provider_generation != self._holder
                    or any(entry.get(field) != self.snapshot.get(field) for field in
                           ('owner','stableChatID','authorityWorkspaceID','mappingRevision','workspaceRevision','mapRevision','policyRevision','ownerAccountID'))
                    or asyncio.get_running_loop().time() >= self._deadline):
                raise ValueError('S07 boundary changed')
        return True

    async def assert_current_policy(self, bridge, identity):
        """Re-evaluate the existing recall policy; this witness only denies."""
        await self.assert_current(bridge, identity)
        resolver = bridge._managed_tool_policy_resolver
        if not callable(resolver) or await resolver(dict(self._identity), self._entry, 'recall_memory', 'memory_read', dict(self.snapshot)) is not True:
            raise ValueError('S07 recall policy changed')
        await self.assert_current(bridge, identity)
        return True

    def close(self):
        self._active = False


async def _retire_unissued_files_preparations(prepared):
    """Drain known-unsent original Files claims without racing issued work."""
    cancelled = False
    for owner in {id(item): item for item in prepared if item is not None}.values():
        task = asyncio.create_task(asyncio.to_thread(owner.abort_unissued))
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        try:
            task.result()
        except (Exception, asyncio.CancelledError):
            pass
    return cancelled


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
        managed_provider_generation: str | None = None,
        session_workspace_adapter: Any = None,
        lifecycle_epoch: str | None = None,
        managed_actor_execution_reader: Any = None,
    ) -> None:
        self._client = client
        self._cwd = cwd
        self._owner = owner
        self._memory_provider = memory_provider
        self._managed_provider_context = managed_provider_context
        # Set by the owner-bound callback authority when the future fixed
        # search recipe is installed.  A missing generation fails closed.
        self._stdin_issue_lock = threading.RLock()
        self._managed_provider_generation = managed_provider_generation
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
        # In-process credential callbacks never enter copied session context.
        self._receipt_origin_registry: Dict[str, dict] = {}
        self._session_context_locks: Dict[str, asyncio.Lock] = {}
        self._session_admission_locks: Dict[tuple[str, str], asyncio.Lock] = {}
        self._managed_actor_execution_reader = managed_actor_execution_reader
        self._managed_root_actor_reader = None
        self._managed_execution_lock = asyncio.Lock()
        self._provider_root_sync_guard = threading.Lock()
        self._kernel_scopes = set()
        self._kernel_retired_sessions = set()
        self._kernel_retired_chats = set()
        self._kernel_retired = False
        self._managed_actor_turns: Dict[tuple[str, str, str, int, str], dict] = {}
        self._managed_actor_executions: Dict[tuple[str, str, str, int], dict] = {}
        self._direct_foreground_attachments = {}
        self._managed_logging_uncertain: set[tuple[str, str, str, int]] = set()
        self._managed_finished_executions: set[tuple[str, str, str, int]] = set()
        self._managed_finished_turns: Dict[tuple[str, str, str, int, str], str] = {}
        self._prepared_managed_handoffs: Dict[tuple[str, str, str, str, str, int], dict] = {}
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
        # state is gone (e.g. created before the OPENCLANK_HOME isolation).
        # Persisted so the remap survives server restarts.
        if session_map_path is not None:
            self._session_map_path = session_map_path
        else:
            runtime_root = (
                Path(
                    os.environ.get("OPEN_CLANK_DATA_DIR")
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
        self.question_handler.set_context_resolver(self._plan_question_context)
        self.question_handler.on_request(self._surface_question)
        self.question_handler.on_resolved(self._clear_question)
        client.register_callback("_odysseus/question", self.question_handler.handle)
        client.register_callback("_openclank/session/v1/cwd/change", self._handle_session_cwd_change)
        client.register_callback("_openclank/session/v1/git-index/recheck", self._handle_git_index_recheck)
        client.register_callback("_openclank/session/v1/git-index/finish", self._handle_git_index_finish)
        client.register_callback("_openclank/session/v1/git-commit/recheck", self._handle_git_commit_recheck)
        client.register_callback("_openclank/session/v1/git-commit/finish", self._handle_git_commit_finish)
        client.register_callback("_openclank/session/v1/git-checkout/phase", self._handle_git_checkout_phase)
        client.register_callback("_openclank/session/v1/git-worktree/phase", self._handle_git_worktree_stage_phase)
        client.register_callback("_openclank/session/v1/git-operation/recheck", self._handle_git_operation_recheck)
        client.register_callback("_openclank/session/v1/git-operation/finish", self._handle_git_operation_finish)
        client.register_callback("_openclank/session/v1/git-fetch/recheck", self._handle_git_fetch_recheck)
        client.register_callback("_openclank/session/v1/git-fetch/finish", self._handle_git_fetch_finish)
        client.register_callback("_openclank/session/v1/git-push/recheck", self._handle_git_push_recheck)
        client.register_callback("_openclank/session/v1/git-push/finish", self._handle_git_push_finish)
        client.register_callback("_openclank/session/v1/git-cleanup/recheck", self._handle_git_cleanup_recheck)
        client.register_callback("_openclank/session/v1/git-cleanup/finish", self._handle_git_cleanup_finish)
        client.register_callback("_openclank/session/v1/binding/read", self._handle_session_binding_read)
        from src.openclank.managed_rag import METHOD as rag_method, operate as manage_rag
        client.register_callback(rag_method, lambda params: manage_rag(self, params))
        client.register_callback("_openclank/session/v1/memory/manage", self._handle_memery_manage)
        client.register_callback("_openclank/session/v1/memory/search", self._handle_memery_search)
        client.register_callback("_openclank/session/v1/code-context/read", self._handle_code_context_read)
        client.register_callback("_openclank/session/v1/goal/completed", self._handle_goal_completed)
        client.register_callback("_openclank/session/v1/execution/finish", self._handle_managed_execution_finish)
        client.register_callback("_openclank/session/v1/tool/admit", self._handle_managed_tool_admit)
        from src.openclank.managed_protocol import HOST_CALLBACK_METHODS
        if "_openclank/session/v1/worker-naming/request" in HOST_CALLBACK_METHODS:
            client.register_callback("_openclank/session/v1/worker-naming/request", self._handle_worker_naming)
        client.register_callback("_openclank/session/v1/evolve/operate", self._handle_evolve_operate)
        client.register_callback("_openclank/session/v1/background-job/start", self._handle_managed_background_start)
        client.register_callback("_openclank/session/v1/files/mutate", self._handle_managed_files_mutate)
        client.register_callback("_openclank/session/v1/task/create", self._handle_managed_task_create)
        client.register_callback("_openclank/session/v1/task/read", self._handle_managed_task_read)
        client.register_callback("_openclank/session/v1/task/control", self._handle_managed_task_control)
        client.register_callback("_openclank/session/v1/task/edit", self._handle_managed_task_edit)
        client.register_callback("_openclank/session/v1/task/run", self._handle_managed_task_run)
        client.register_callback("_openclank/session/v1/task/status", self._handle_managed_task_status)
        client.register_callback("_openclank/session/v1/background-job/read", self._handle_managed_background_read)
        client.register_callback("_openclank/session/v1/background-job/terminate", self._handle_managed_background_terminate)
        client.register_callback("_openclank/session/v1/background-job/write", self._handle_managed_background_write)
        client.register_callback("_openclank/session/v1/background-job/retire", self._handle_managed_background_retire)
        client.register_callback("_openclank/session/v1/recall/admitted", self._handle_managed_recall_admitted)
        client.register_callback("_openclank/session/v1/maintenance/stage", self._handle_managed_maintenance_stage)
        client.register_callback("_openclank/session/v1/maintenance/ack", self._handle_managed_maintenance_ack)
        client.register_callback("_openclank/session/v1/maintenance/source", self._handle_managed_maintenance_source)
        client.register_callback("_openclank/session/v1/attachment/source", self._handle_generated_attachment_source)
        client.register_callback("_openclank/session/v1/media-job/call", self._handle_managed_media_job_call)
        client.register_callback("_openclank/session/v1/shared-text/call", self._handle_managed_shared_text_call)
        client.register_callback("_openclank/session/v1/actor-frame/call", self._handle_managed_actor_frame_call)
        client.register_callback("_openclank/session/v1/theme-emoji/call", self._handle_managed_theme_emoji_call)
        client.register_callback("_openclank/session/v1/python-kernel/call", self._handle_managed_python_kernel_call)
        client.register_callback("_openclank/session/v1/native-analysis/source", self._handle_native_analysis_source)
        client.register_callback("_openclank/session/v1/context/augment", self._handle_managed_context_augment)
        client.register_callback("_openclank/session/v1/memory/prepared", self._handle_managed_prepared_read)
        client.register_callback("_openclank/session/v1/skill/metadata-current", self._handle_skill_metadata_current)
        client.register_callback("_openclank/session/v1/context/delivered", self._handle_managed_context_delivered)
        client.register_callback("_openclank/session/v1/dispatch/admit", self._handle_connector_dispatch_admit)
        client.register_callback("_openclank/session/v1/turn/start", self._handle_managed_turn_start)
        client.register_callback("_openclank/session/v1/turn/status", self._handle_managed_turn_status)
        client.register_callback("_openclank/session/v1/turn/finish", self._handle_managed_turn_finish)
        client.register_callback("_openclank/session/v1/queued-source/forward", self._handle_queued_source_forward)
        client.register_callback("_openclank/session/v1/queued-source/fence", self._handle_queued_source_fence)
        client.register_callback("_openclank/session/v1/code-activity/observe", self._handle_code_activity_observe)
        client.register_callback("_openclank/session/v1/input-policy/resolve", self._handle_input_policy_resolve)
        client.register_callback("_openclank/session/v1/input-origin/sync", self._handle_input_origin_sync)
        client.register_callback("_openclank/session/v1/memory/inferred-proposal", self._handle_memory_inferred_proposal)
        client.register_callback("_openclank/session/v1/memory/inferred-proposal/status", self._handle_memory_inferred_status)
        client.register_callback("_openclank/session/v1/workflow/artifact", self._handle_workflow_artifact)
        client.register_callback("_openclank/session/v1/workflow/artifact/status", self._handle_workflow_artifact_status)
        client.register_callback("_openclank/session/v1/workflow/artifact/read", self._handle_workflow_artifact_read)
        client.register_callback("_openclank/session/v1/workflow/snapshot/read", self._handle_workflow_snapshot_read)
        # History callbacks derive both account and stable chat from this
        # admitted engine session. The engine never receives an owner selector.
        from src.openclank.conversation_archive import get_conversation_archive
        from src.openclank.managed_history import ManagedHistoryCallbacks

        self._managed_history_callbacks = ManagedHistoryCallbacks(
            get_conversation_archive(), self._managed_history_binding, self._managed_history_tool_timing,
            self._managed_history_tombstone
        )
        client.register_callback(
            "_openclank/history/v1/query",
            self._managed_history_query,
        )
        client.register_callback(
            "_openclank/history/v1/mutate",
            lambda params: self._managed_history_callbacks.dispatch("_openclank/history/v1/mutate", params),
        )
        if permission_handler is not None and hasattr(permission_handler, "on_request"):
            permission_handler.on_request(self._surface_permission)

    def set_managed_provider_generation(self, generation: str) -> None:
        """Bind the bridge's trusted search context to one worker generation."""
        value = str(generation or "").strip()
        if not value:
            raise ValueError("managed provider generation is required")
        with self._stdin_issue_lock: self._managed_provider_generation = value

    def set_managed_actor_execution_reader(self, reader) -> None:
        """Install the supervisor's private complete children/ActorRegistry read."""
        if not callable(reader):
            raise ValueError("managed actor execution reader is required")
        self._managed_actor_execution_reader = reader
        # Replacing the full reader invalidates any previously paired root reader.
        self._managed_root_actor_reader = None

    def set_managed_root_actor_reader(self, reader) -> None:
        """Install the same worker's private fresh root-only ActorRegistry read."""
        if (not callable(reader) or getattr(reader, '__self__', None) is None
            or getattr(reader, '__self__', None) is not getattr(self._managed_actor_execution_reader, '__self__', None)):
            raise ValueError("managed root actor reader requires the same worker")
        self._managed_root_actor_reader = reader

    def set_managed_input_policy_resolver(self, resolver) -> None:
        """Install the shared request-policy admission resolver."""
        if not callable(resolver):
            raise ValueError("managed input policy resolver is required")
        self._managed_input_policy_resolver = resolver

    async def _retain_receipt_origin(self, *, owner, account, chat, root_session, session, actor, request_id,
                                     nonce, acknowledgement, receipt, current, issued_at, expected_context, expected_holder):
        """Keep the winning first-ingress callable private to this live bridge."""
        if (owner != self._owner or not account or not callable(current) or current() is not True
                or type(issued_at) not in (int,float) or not 0 <= time.monotonic() - issued_at < 3600
                or not isinstance(nonce, str) or re.fullmatch(r'[0-9a-f]{32}', nonce) is None
                or type(acknowledgement) is not dict or type(receipt) is not dict
                or acknowledgement.get('firstIngressNonce') != nonce or receipt.get('firstIngressNonce') != nonce):
            return False
        names = ('requestID', 'sessionID', 'actorID', 'payloadHash', 'mapRevision', 'mappingRevision', 'workspaceRevision')
        if (any(acknowledgement.get(name) != receipt.get(name) for name in names)
                or receipt.get('requestID') != request_id or receipt.get('sessionID') != session
                or receipt.get('actorID') != actor or receipt.get('owner') != owner
                or receipt.get('stableChatID') != chat or receipt.get('state') not in ('persisted', 'pending-boundary', 'consumed')
                or not isinstance(receipt.get('payloadHash'), str)
                or re.fullmatch(r'[0-9a-f]{64}', receipt['payloadHash']) is None):
            return False
        binding = await self._handle_session_binding_read({'sessionID':root_session})
        if (binding.get('owner') != owner or binding.get('stableChatID') != chat
                or any(binding.get(name) != receipt.get(name) for name in ('mapRevision','mappingRevision','workspaceRevision'))):
            return False
        key = (owner, account, chat, session, actor, request_id)
        async with self._managed_execution_lock:
            context = self._session_context.get(root_session)
            if (not isinstance(context, dict) or context is not expected_context or not expected_holder
                    or self._managed_provider_generation != expected_holder or current() is not True):
                return False
            registry = self._receipt_origin_registry
            owner_state = registry.get(root_session)
            if owner_state is None or owner_state['context'] is not context:
                if owner_state is None and len(registry) >= 512:
                    return False
                owner_state = {'context':context, 'slots':{}, 'retired':{}, 'fullUntil':0.0}
                registry[root_session] = owner_state
            slots = owner_state['slots']
            retired = owner_state['retired']
            now = time.monotonic()
            for old in [k for k,v in slots.items() if v['expires'] <= now]:
                slots.pop(old, None)
            for old in [k for k,v in retired.items() if v <= now]:
                retired.pop(old, None)
            identity = (owner,chat,session,actor,request_id)
            if (owner_state['fullUntil'] > now or identity in retired or key in slots
                    or len(slots) >= 512 or len(retired) >= 512 or not 0 <= now - issued_at < 3600):
                return False
            slots[key] = {'nonce':nonce, 'payloadHash':receipt['payloadHash'], 'binding':dict(binding),
                'holder':self._managed_provider_generation, 'current':current, 'expires':issued_at + 3600}
            return True

    async def _forget_receipt_origin(self, *, owner, chat, root_session, session, actor, request_id):
        async with self._managed_execution_lock:
            context = self._session_context.get(root_session)
            if not isinstance(context, dict):
                return
            registry = self._receipt_origin_registry
            owner_state = registry.get(root_session)
            if owner_state is None or owner_state['context'] is not context:
                if owner_state is None and len(registry) >= 512:
                    return
                owner_state = {'context':context, 'slots':{}, 'retired':{}, 'fullUntil':0.0}
                registry[root_session] = owner_state
            slots = owner_state['slots']
            identity = (owner,chat,session,actor,request_id)
            retired = owner_state['retired']
            now = time.monotonic()
            for old in [k for k,v in retired.items() if v <= now]:
                retired.pop(old, None)
            if len(retired) < 512 or identity in retired:
                retired[identity] = now + 3600
            else:
                # A full fence outlives every already-issued nonce, so no
                # delayed response can reopen a cancelled origin.
                owner_state['fullUntil'] = now + 3600
                slots.clear()
            for key in [key for key in slots if (key[0],key[2],key[3],key[4],key[5]) == identity]:
                slots.pop(key, None)

    async def prepare_managed_handoff(
        self,
        *,
        owner: str,
        host_chat_id: str,
        engine_session_id: str,
        root_operation_id: str,
        request_id: str,
        root_session_id: str | None = None,
        handoff_id: str,
        admission_ceiling: dict | None = None,
        turn_policy: dict | None = None,
        input_text: str = "",
        ingress_account_id: str | None = None,
        human_ingress=None,
        human_runtime=None,
        recovery_human=None,
        actor_id: str,
        expected_execution_revision: int,
    ) -> dict:
        """Reserve exactly one already-admitted O08 handoff before native CAS.

        The reservation is in-memory and fail-closed: it is never aged into
        permission for another root. Unknown outcomes remain reserved until
        the same handoff is reconciled or the owner runtime retires.
        """
        values = [owner, host_chat_id, engine_session_id, root_operation_id, request_id, handoff_id, actor_id]
        normalized = [str(value or "").strip() for value in values]
        if any(not value for value in normalized) or owner != self._owner:
            raise ValueError("managed handoff identity is incomplete or owner-mismatched")
        if isinstance(expected_execution_revision, bool) or not isinstance(expected_execution_revision, int) or expected_execution_revision < 0:
            raise ValueError("managed handoff execution revision is invalid")
        _, chat_id, session_id, root_id, request_key, handoff_key, actor_key = normalized
        root_session = str(root_session_id or session_id).strip()
        binding = await self._handle_session_binding_read({"sessionID": root_session})
        if binding.get("stableChatID") != chat_id or binding.get("owner") != owner:
            raise ValueError("managed handoff is not bound to the requested current chat")
        tree = await self._read_managed_actor_tree(root_session)
        actor = self._find_actor_in_tree(tree, session_id, actor_key)
        if actor is None:
            raise ValueError("managed handoff target is not in the native actor tree")
        current, seen = actor, set()
        while current.get("mode") != "main":
            edge = (current.get("sessionID"), current.get("actorID"))
            if edge in seen or len(seen) >= 64:
                raise ValueError("managed handoff lineage is cyclic or too deep")
            seen.add(edge)
            parent_id = current.get("parentActorID")
            current_session = current.get("sessionID")
            parent_session = (tree["sessions"].get(current_session) or {}).get("parentID")
            candidates = [row for row in (
                self._find_actor_in_tree(tree, current_session, parent_id),
                self._find_actor_in_tree(tree, parent_session, parent_id) if parent_session else None,
            ) if row is not None]
            if len(candidates) != 1:
                raise ValueError("managed handoff parent is missing or ambiguous")
            current = candidates[0]
        if current.get("sessionID") != root_session or current.get("parentActorID") not in (None, ""):
            raise ValueError("managed handoff does not descend from the mapped root")
        current_revision = self._actor_revision(actor)
        if current_revision != expected_execution_revision:
            raise ValueError("managed handoff actor revision changed before reservation")
        # A resumed handoff may inherit only the original signed receipt's
        # immutable subject, never the current username alone. Absence does
        # not block the ordinary handoff.
        source_account = None
        source_receipt_hash = None
        recovery_origin = recovery_human is not None
        if recovery_origin:
            from core.models import get_session_manager_instance
            manager=get_session_manager_instance(); state=getattr(manager,'lifecycle_context',None) if manager else None
            auth=getattr(state,'auth_manager',None); barrier=getattr(state,'account_request_barrier',None)
            if (type(recovery_human) is not tuple or len(recovery_human)!=5 or actor_key!='main'
                    or session_id!=root_session or ingress_account_id is not None or human_ingress is not None
                    or binding.get('authority') or auth is None or barrier is None):
                raise ValueError('recovery human authority unavailable')
            account,witness_barrier,admission,current,preflight=recovery_human
            if (type(account) is not str or not account or witness_barrier is not barrier or not callable(current) or not callable(preflight)
                    or current() is not True or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
                    or not await barrier.is_live(owner,admission)):
                raise ValueError('recovery human authority changed')
            barrier.assert_live_admission(owner,admission)
            if current() is not True: raise ValueError('recovery human authority changed')
            source_account=account
        if (actor_key == 'main' and session_id == root_session and isinstance(ingress_account_id,str)
            and ingress_account_id):
            try:
                from core.models import get_session_manager_instance
                from src.openclank.managed_protocol import validate_engine_method_result
                from src.openclank.queued_source import match_queued_boundary
                manager=get_session_manager_instance();state=getattr(manager,'lifecycle_context',None) if manager else None
                auth=getattr(state,'auth_manager',None)
                worker=getattr(self._managed_actor_execution_reader,'__self__',None)
                holder=getattr(self._managed_provider_context,'__self__',None)
                if (auth is None or auth.account_id(owner) != ingress_account_id or
                    auth.username_for_account_id(ingress_account_id) != owner or worker is None or
                    getattr(worker,'_bridge',None) is not self or not self._managed_provider_generation or
                    getattr(holder,'holder_id',None) != self._managed_provider_generation):
                    raise ValueError('queued source holder unavailable')
                method='_openclank/session/v1/queued-source/read'
                receipt=await worker.managed_engine_call(method,{'owner':owner,'stableChatID':chat_id,'sessionID':session_id,'requestID':request_key})
                receipt=validate_engine_method_result(method,receipt)
                if (receipt.get('state') != 'ready' or
                    receipt.get('text') != input_text or receipt.get('turnPolicy') != turn_policy or
                    receipt.get('messageID') is not None or receipt.get('partID') is not None or
                    receipt.get('mappingRevision') != binding.get('mappingRevision') or
                    receipt.get('workspaceRevision') != binding.get('workspaceRevision')):
                    raise ValueError('queued source receipt changed')
                matched=match_queued_boundary(receipt.get('proof'),owner=owner,account_id=ingress_account_id,
                    chat_id=chat_id,session_id=session_id,actor_id=actor_key,workspace_id=binding['authorityWorkspaceID'],receipt=receipt)
                if (not matched or await self._handle_session_binding_read({'sessionID':root_session}) != binding or
                    auth.account_id(owner) != ingress_account_id or auth.username_for_account_id(ingress_account_id) != owner or
                    getattr(holder,'holder_id',None) != self._managed_provider_generation):
                    raise ValueError('queued source authority changed')
                source_account=ingress_account_id
                source_receipt_hash=receipt['payloadHash']
            except Exception:
                pass
        raw_ceiling = None
        if admission_ceiling is not None:
            from src.tool_policy import conservative_legacy_turn_policy, normalize_turn_policy_snapshot
            if not isinstance(admission_ceiling, dict) or type(admission_ceiling.get("blockAllToolCalls")) is not bool:
                raise ValueError("managed handoff policy ceiling is invalid")
            allowed = admission_ceiling.get("allowedTools")
            disabled = admission_ceiling.get("disabledTools")
            hidden = admission_ceiling.get("hiddenTools")
            if (allowed is not None and (not isinstance(allowed, list) or any(not isinstance(x, str) for x in allowed))
                or not isinstance(disabled, list) or any(not isinstance(x, str) for x in disabled)
                or not isinstance(hidden, list) or any(not isinstance(x, str) for x in hidden)
                or not isinstance(input_text, str)):
                raise ValueError("managed handoff policy ceiling is invalid")
            choices = normalize_turn_policy_snapshot(turn_policy or conservative_legacy_turn_policy())
            raw_ceiling = {"allowedTools": sorted(set(allowed)) if allowed is not None else None,
                "disabledTools": sorted(set(disabled)), "hiddenTools": sorted(set(hidden)),
                "blockAllToolCalls": admission_ceiling["blockAllToolCalls"],
                "choiceSnapshot": choices, "inputText": input_text}
        key = (owner, chat_id, session_id, root_id, handoff_key, expected_execution_revision)
        record = {
            "owner": owner, "stableChatID": chat_id, "sessionID": session_id,
            "rootSessionID": root_session, "rootOperationID": root_id, "requestID": request_key,
            "handoffID": handoff_key, "actorID": actor_key,
            "expectedExecutionRevision": expected_execution_revision,
            "mapRevision": binding["mapRevision"],
            "mappingRevision": binding["mappingRevision"],
            "workspaceRevision": binding["workspaceRevision"],
            "rawPolicyCeiling": raw_ceiling,
            "ownerAccountID": source_account,
            "recoveryOrigin": recovery_origin,
            "queuedReceiptHash": source_receipt_hash,
            "state": "prepared",
        }
        proof = None
        if (actor_key != 'main' and ingress_account_id and isinstance(human_ingress, tuple) and len(human_ingress) == 2
                and isinstance(human_runtime, tuple) and len(human_runtime) == 2):
            barrier, original = human_ingress
            lease, worker = human_runtime
            from core.models import get_session_manager_instance
            manager = get_session_manager_instance()
            lifecycle = getattr(manager, 'lifecycle_context', None) if manager else None
            auth = getattr(lifecycle, 'auth_manager', None)
            pool = getattr(lease, '_pool', None)
            process = getattr(worker, '_proc', None)
            if (barrier is getattr(lifecycle, 'account_request_barrier', None) and auth is not None and pool is not None
                    and getattr(lease, 'worker', None) is worker and worker._bridge is self and process is not None
                    and worker.is_alive() and self._managed_provider_generation
                    and auth.account_id(owner) == ingress_account_id and auth.username_for_account_id(ingress_account_id) == owner
                    and await barrier.is_live(owner, original)):
                pool._assert_owner_lifecycle(owner, lease._owner_epoch)
                child_admission = await barrier.acquire(owner)
                proof = {'barrier': barrier, 'admission': child_admission, 'original': original, 'account': ingress_account_id,
                         'auth': auth, 'pool': pool, 'worker': worker, 'process': process, 'generation': worker.installed_generation,
                         'epoch': lease._owner_epoch, 'holder': self._managed_provider_generation}
        try:
            if proof is not None and (not await proof['barrier'].is_live(owner, proof['original'])
                    or proof['auth'].account_id(owner) != proof['account'] or proof['auth'].username_for_account_id(proof['account']) != owner):
                raise ValueError('original human admission changed')
            async with self._managed_execution_lock:
                if recovery_origin:
                    # Recovery is rare: retain this lock while the owned, drained
                    # canonical check completes. Driver I/O can delay the lock.
                    if await preflight() is not True:
                        raise ValueError("recovery human authority changed")
                    barrier.assert_live_admission(owner, admission)
                    if current() is not True:
                        raise ValueError("recovery human authority changed")
                if any(existing_key[:5] == key[:5] and existing_key[5] != key[5]
                    for existing_key in self._prepared_managed_handoffs):
                    raise ValueError("managed handoff identity already reserved at another revision")
                existing = self._prepared_managed_handoffs.get(key)
                if existing is not None:
                    if {k: v for k, v in existing.items() if k not in {"state", "claimedExecutionRevision", "displayOwnerAdmission"}} != {k: v for k, v in record.items() if k != "state"}:
                        raise ValueError("managed handoff reservation conflicts with an existing claim")
                    return {"prepared": True, "replayed": True}
                if len(self._prepared_managed_handoffs) >= 512:
                    raise ValueError("managed handoff reservation capacity is exhausted")
                if proof is not None:
                    record['displayOwnerAdmission'] = proof
                self._prepared_managed_handoffs[key] = record
                proof = None  # reservation owns the distinct admission
        finally:
            if proof is not None:
                await self._release_owner_admission(proof['barrier'], proof['admission'])
        return {"prepared": True, "replayed": False}

    async def clear_managed_handoff(
        self, *, owner: str, host_chat_id: str, engine_session_id: str,
        root_operation_id: str, handoff_id: str, expected_execution_revision: int, confirmed_unissued: bool = False,
    ) -> bool:
        key = (str(owner), str(host_chat_id), str(engine_session_id), str(root_operation_id), str(handoff_id), int(expected_execution_revision))
        async with self._managed_execution_lock:
            record = self._prepared_managed_handoffs.get(key)
            if record is None or record.get("state") != "prepared":
                return False
            if record.get('displayOwnerAdmission') is not None and confirmed_unissued is not True:
                return False  # an unobserved native issue cannot be cleared as unissued
            del self._prepared_managed_handoffs[key]
            proof = record.get('displayOwnerAdmission')
        if proof is not None:
            await self._release_owner_admission(proof['barrier'], proof['admission'])
        return True

    async def _display_owner_admission_current(self, proof) -> bool:
        try:
            from core.models import get_session_manager_instance
            manager = get_session_manager_instance()
            lifecycle = getattr(manager, 'lifecycle_context', None) if manager else None
            pool, worker, account = proof['pool'], proof['worker'], proof['account']
            key = pool._key(self._owner)
            pool._assert_owner_lifecycle(key, proof['epoch'])
            state = pool._states.get(key)
            return bool(getattr(lifecycle, 'auth_manager', None) is proof['auth']
                and getattr(lifecycle, 'account_request_barrier', None) is proof['barrier']
                and proof['auth'].account_id(self._owner) == account
                and proof['auth'].username_for_account_id(account) == self._owner
                and state is not None and state.active is worker and state.generation == proof['generation']
                and worker._bridge is self and worker._proc is proof['process'] and worker.is_alive()
                and self._managed_provider_generation == proof['holder']
                and await proof['barrier'].is_live(self._owner, proof['admission']))
        except Exception:
            return False

    async def _read_managed_actor_tree(self, root_session_id: str) -> dict:
        reader = self._managed_actor_execution_reader
        if reader is None:
            raise ValueError("managed ActorRegistry reader is unavailable")
        tree = await reader(root_session_id)
        if not isinstance(tree, dict) or tree.get("rootSessionID") != root_session_id or not isinstance(tree.get("sessions"), dict):
            raise ValueError("managed ActorRegistry tree is invalid")
        return tree

    @staticmethod
    def _actor_revision(actor: dict) -> int:
        value = actor.get("executionRevision")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("managed actor execution revision is invalid")
        return value

    @staticmethod
    def _find_actor_in_tree(tree: dict, session_id: str, actor_id: str) -> dict | None:
        session = tree.get("sessions", {}).get(session_id)
        if not isinstance(session, dict):
            return None
        actors = session.get("actors")
        if not isinstance(actors, list):
            return None
        return next((row for row in actors if isinstance(row, dict) and row.get("sessionID") == session_id and row.get("actorID") == actor_id), None)

    async def resolve_managed_actor_execution(
        self,
        session_id: str,
        root_operation_id: str,
        actor_id: str,
        execution_revision: int | None = None,
        *, require_running: bool = True, _recall_timings: dict | None = None,
    ) -> dict:
        """Resolve actor authority from owner chat mapping and native lineage.

        Root authority comes from an active normal root, an exact prepared O08
        reservation, or a still-active previously admitted actor in that root.
        Child membership and parentActorID are read from native state; model
        arguments and hostContext are never lineage authority.
        """
        session = str(session_id or "").strip()
        root = str(root_operation_id or "").strip()
        actor_key = str(actor_id or "").strip()
        if not session or not root or not actor_key or not self._owner:
            raise ValueError("managed actor identity is incomplete")
        # An admitted execution remains authoritative until its exact finish is
        # acknowledged; a newer native revision cannot displace an uncertain one.
        if execution_revision is not None and any(
            entry.get("sessionID") == session and entry.get("actorID") == actor_key
            and (entry.get("rootOperationID") != root or entry.get("executionRevision") != execution_revision)
            for entry in self._managed_actor_executions.values()
        ):
            raise ValueError("a prior managed actor execution requires reconciliation")
        root_session = None
        context = self._session_context.get(session) or {}
        if context.get("root_operation_id") == root and session in self._turns:
            root_session = session
        if root_session is None:
            for item in self._prepared_managed_handoffs.values():
                if item.get("owner") == self._owner and item.get("rootOperationID") == root and item.get("sessionID") == session:
                    root_session = item.get("rootSessionID") or session
                    break
        if root_session is None:
            for item in self._managed_actor_executions.values():
                if item.get("owner") == self._owner and item.get("rootOperationID") == root:
                    root_session = item.get("rootSessionID")
                    if root_session:
                        break
        if root_session is None:
            raise ValueError("managed actor root is not active or reserved")
        binding = await self._handle_session_binding_read({"sessionID": root_session})
        if binding.get("authority") and (session != root_session or actor_key != "main"):
            raise ValueError("private child and detached execution authority is unavailable")
        if binding.get("owner") != self._owner:
            raise ValueError("managed actor root owner changed")
        tree_started = asyncio.get_running_loop().time() if _recall_timings is not None else None
        try:
            if (session == root_session and actor_key == "main"
                    and self._managed_root_actor_reader is not None):
                if binding.get('authority'):
                    tree = await self._managed_root_actor_reader(root_session, private_binding=binding)
                else:
                    tree = await self._managed_root_actor_reader(root_session)
                if (not isinstance(tree, dict) or tree.get('rootSessionID') != root_session
                        or type(tree.get('sessions')) is not dict or set(tree['sessions']) != {root_session}):
                    raise ValueError("managed root actor snapshot is invalid")
            else:
                tree = await self._read_managed_actor_tree(root_session)
        finally:
            if _recall_timings is not None:
                _recall_timings['native_tree'] = round((asyncio.get_running_loop().time() - tree_started) * 1000, 3)
        actor = self._find_actor_in_tree(tree, session, actor_key)
        if actor is None:
            raise ValueError("managed actor is not in the current native session tree")
        revision = self._actor_revision(actor)
        if execution_revision is not None:
            if isinstance(execution_revision, bool) or not isinstance(execution_revision, int) or revision != execution_revision:
                raise ValueError("managed actor execution revision is stale")
            if require_running and actor.get("status") != "running":
                raise ValueError("managed actor execution is no longer running")
        chain = []
        seen = set()
        current = actor
        while True:
            key = (current.get("sessionID"), current.get("actorID"))
            if key in seen or len(chain) >= 64:
                raise ValueError("managed actor lineage is cyclic or too deep")
            seen.add(key)
            chain.append(current)
            if current.get("mode") == "main":
                if current.get("sessionID") != root_session or current.get("parentActorID") not in (None, ""):
                    raise ValueError("managed actor lineage terminates at a non-root main actor")
                break
            parent_id = current.get("parentActorID")
            if not isinstance(parent_id, str) or not parent_id:
                raise ValueError("managed child actor has no registered parent")
            current_session = current.get("sessionID")
            session_info = tree["sessions"].get(current_session) or {}
            parent_session_id = session_info.get("parentID")
            candidates = []
            same = self._find_actor_in_tree(tree, current_session, parent_id)
            if same is not None:
                candidates.append(same)
            if parent_session_id:
                parent = self._find_actor_in_tree(tree, parent_session_id, parent_id)
                if parent is not None and parent not in candidates:
                    candidates.append(parent)
            if len(candidates) != 1:
                raise ValueError("managed actor parent is missing or ambiguous in native lineage")
            current = candidates[0]
        if actor.get("mode") != "main" and not any(
            item.get("rootOperationID") == root and item.get("sessionID") == session
            and item.get("actorID") == actor_key and item.get("state") in {"prepared", "claimed"}
            for item in self._prepared_managed_handoffs.values()
        ):
            lineage_keys = {
                (row.get("sessionID"), row.get("actorID"), row.get("executionRevision"))
                for row in chain if row.get("status") == "running"
            }
            if not any(
                entry.get("rootOperationID") == root
                and (entry.get("sessionID"), entry.get("actorID"), entry.get("executionRevision")) in lineage_keys
                for entry in self._managed_actor_executions.values()
            ):
                raise ValueError("managed actor is not descended from a currently active execution")
        model = actor.get("effectiveModel")
        if not isinstance(model, dict) or not isinstance(model.get("providerID"), str) or not isinstance(model.get("modelID"), str):
            raise ValueError("managed actor effective model is unavailable")
        return {
            "owner": self._owner,
            "holderID": str(self._managed_provider_generation or ""),
            "stableChatID": binding["stableChatID"],
            "rootOperationID": root,
            "sessionID": session,
            "actorID": actor_key,
            "parentActorID": actor.get("parentActorID"),
            "modelRequestOptions": actor.get("modelRequestOptions"),
            "mainOptionCandidate": (self._turns[root_session].main_option_candidate
                if actor_key == "main" and session == root_session
                and context.get("root_operation_id") == root
                and root_session in self._turns
                and self._turns[root_session].root_operation_id == root else None),
            "effectiveModel": dict(model),
            "executionRevision": revision,
            "rootSessionID": root_session,
            "authorityWorkspaceID": binding["authorityWorkspaceID"],
            **({"authority": binding["authority"]} if binding.get("authority") else {
                "mapRevision": binding["mapRevision"], "mappingRevision": binding["mappingRevision"],
                "workspaceRevision": binding["workspaceRevision"]}),
            "lineage": [
                {"sessionID": row.get("sessionID"), "actorID": row.get("actorID"),
                 "executionRevision": row.get("executionRevision"), "mode": row.get("mode"),
                 "status": row.get("status")}
                for row in chain
            ],
        }

    @staticmethod
    def _managed_turn_key(identity: dict) -> tuple[str, str, str, int, str]:
        return (
            str(identity["sessionID"]), str(identity["rootOperationID"]),
            str(identity["actorID"]), int(identity["executionRevision"]),
            str(identity["innerTurnID"]),
        )

    def _managed_actor_foreground_connection(self, identity, resolved):
        return self._managed_actor_provider_binding(identity, resolved)['connection_id']

    def _managed_actor_provider_binding(self, identity, resolved):
        """Read the actual host-issued binding, never a model-supplied route."""
        from core.provider_models import ProviderOperationBinding
        callback = getattr(self._managed_provider_context, '__self__', None)
        if (callback is None or getattr(callback, 'owner', None) != self._owner
            or getattr(callback, 'holder_id', None) != resolved.get('holderID')):
            raise ValueError('managed provider generation unavailable')
        factory = getattr(callback, '_session_factory', None)
        if not callable(factory):
            raise ValueError('managed provider authority unavailable')
        model = resolved.get('effectiveModel')
        if not isinstance(model, dict):
            raise ValueError('managed actor model unavailable')
        db = factory()
        try:
            rows = db.query(ProviderOperationBinding).filter_by(owner=self._owner,
                root_operation_id=identity['rootOperationID'], engine_session_id=identity['sessionID'],
                actor_id=identity['actorID'], execution_revision=identity['executionRevision'],
                model_id=model.get('modelID')).filter(ProviderOperationBinding.state.in_(['uncommitted','committed'])).all()
            expected_private = ({**resolved['authority'], 'holderID': resolved['holderID']}
                if resolved.get('authority') else None)
            if (len(rows) != 1 or not rows[0].connection_id or not rows[0].model_route_id
                or (rows[0].policy_snapshot or {}).get('private_authority') != expected_private
                or model.get('providerID') not in (rows[0].connection_id, rows[0].provider_id)):
                raise ValueError('managed actor provider binding unavailable')
            row = rows[0]
            if row.selected_account_id is None and row.credential_revision is None:
                # The existing provider owner issues a keyless binding only for
                # a local connection. Re-read that exact owned route; do not
                # manufacture an account or accept this shape for paid lanes.
                from src.openclank.provider_store import _KEYLESS_CONNECTION_KINDS
                store = getattr(callback, '_store', None)
                if (store is None or row.source != 'keyless' or row.billing_lane != 'local'
                        or row.credential_owner != self._owner or row.grant_id is not None
                        or row.preferred_account_id is not None):
                    raise ValueError('managed actor keyless binding unavailable')
                connection = store._require_connection(db, row.credential_owner, row.connection_id)
                route = store._require_route(db, row.credential_owner, row.model_route_id)
                if (connection.enabled is not True or connection.kind not in _KEYLESS_CONNECTION_KINDS
                        or connection.billing_lane != 'local' or route.enabled is not True
                        or route.connection_id != connection.id or route.provider_model_id != row.model_id):
                    raise ValueError('managed actor keyless route unavailable')
            elif not row.selected_account_id or row.credential_revision is None:
                raise ValueError('managed actor provider binding unavailable')
            return {'id':row.id, 'revision':row.revision, 'model_route_id':row.model_route_id,
                    'model_id':row.model_id, 'connection_id':row.connection_id, 'provider_id':row.provider_id,
                    'account_id':row.selected_account_id, 'credential_revision':row.credential_revision,
                    'billing_lane':row.billing_lane, 'grant_id':row.grant_id}
        finally:
            db.close()

    async def _attach_direct_foreground(self, lease, barrier, admission, account, runtime, handle, *, account_only=False):
        """Bind host-only direct work to this exact worker generation before send."""
        worker = lease.worker
        process = getattr(worker, '_proc', None)
        holder = self._managed_provider_generation
        if ((account_only is True and (runtime is not None or handle is not None
                    or getattr(lease,'_pool',None) is None or getattr(lease._pool,'_s05_foreground_runtime',None) is not None))
                or (account_only is not True and (runtime is None or handle is None))
                or getattr(worker, '_bridge', None) is not self or process is None or not holder
                or getattr(worker, 'installed_generation', None) != lease.generation
                or not account or not await barrier.is_live(lease.owner, admission)):
            raise ValueError('direct foreground authority unavailable')
        async with self._managed_execution_lock:
            if (getattr(self, '_ownerAdmissionHolderStopped', False) or lease._released
                    or getattr(worker, '_proc', None) is not process
                    or getattr(worker, '_bridge', None) is not self
                    or self._managed_provider_generation != holder):
                raise ValueError('direct foreground holder changed')
            key = id(lease)
            if key in self._direct_foreground_attachments:
                raise ValueError('direct foreground already attached')
            entry = dict(lease=lease, worker=worker, process=process, holder=holder,
                         generation=lease.generation, owner_epoch=lease._owner_epoch,
                         account=account, barrier=barrier, admission=admission,
                         runtime=runtime, handle=handle, account_only=account_only is True)
            self._direct_foreground_attachments[key] = entry
            return entry

    async def _bind_direct_provider_policy(self, entry, selection):
        """Attach only a sealed original-human policy to the already-owned lease."""
        from src.openclank.provider_human_policy import CapturedProviderPolicy
        if type(selection) is not CapturedProviderPolicy or not isinstance(entry, dict):
            raise ValueError('captured provider policy unavailable')
        selection.assert_current()
        barrier = entry.get('barrier'); lease = entry.get('lease')
        if (barrier is not selection._admission._barrier or lease is None
                or selection.owner != lease.owner or selection.account_id != entry.get('account')
                or not await barrier.is_live(selection.owner, selection._admission._admission)
                or not await barrier.is_live(selection.owner, entry.get('admission'))):
            raise ValueError('captured provider policy authority changed')
        async with self._managed_execution_lock:
            selection.assert_current()
            if (self._direct_foreground_attachments.get(id(lease)) is not entry or lease._released
                    or entry.get('provider_policy_selection') is not None
                    or entry.get('holder') != self._managed_provider_generation
                    or getattr(entry.get('worker'), '_bridge', None) is not self
                    or getattr(entry.get('worker'), '_proc', None) is not entry.get('process')):
                raise ValueError('captured provider policy holder changed')
            entry['provider_policy_selection'] = selection

    async def _bind_direct_provider_policy_intent(self, entry, intent):
        from src.openclank.provider_human_policy import CapturedProviderPolicyIntent
        if type(intent) is not CapturedProviderPolicyIntent or not isinstance(entry,dict):
            raise ValueError('captured provider policy intent unavailable')
        intent.assert_current()
        barrier=entry.get('barrier');lease=entry.get('lease')
        if (barrier is not intent.admission._barrier or lease is None or intent.admission.owner!=lease.owner
                or intent.admission.account_id!=entry.get('account')
                or not await barrier.is_live(intent.admission.owner,intent.admission._admission)
                or not await barrier.is_live(intent.admission.owner,entry.get('admission'))):
            raise ValueError('captured provider policy intent changed')
        async with self._managed_execution_lock:
            intent.assert_current()
            if (self._direct_foreground_attachments.get(id(lease)) is not entry or lease._released
                    or entry.get('provider_policy_intent') is not None
                    or entry.get('provider_policy_selection') is not intent.selection
                    or entry.get('holder')!=self._managed_provider_generation
                    or getattr(entry.get('worker'),'_bridge',None) is not self
                    or getattr(entry.get('worker'),'_proc',None) is not entry.get('process')):
                raise ValueError('captured provider policy intent holder changed')
            entry['provider_policy_intent']=intent

    async def reserve_direct_main_policy_preflight(self, context, expected_revision, original_root):
        """Reserve the exact original foreground intent in the existing handoff owner."""
        from src.openclank.provider_human_policy import CapturedProviderPolicyIntent
        if (not isinstance(context,dict) or context.get('actorID')!='main'
                or context.get('sessionID')!=context.get('rootSessionID')
                or type(expected_revision) is not int or expected_revision<0):
            return
        session=context['sessionID'];root_id=context['rootOperationID']
        async with self._managed_execution_lock:
            if not self._provider_root_sync_guard.acquire(blocking=False):
                raise ValueError('provider root lifecycle unavailable')
            try:
                root=self._turns.get(session);attachment=getattr(root,'foreground_attachment',None)
                if root is not original_root or root is None or root.root_operation_id!=root_id or root.provider_root_closing:
                    raise ValueError('provider root changed during preflight')
                intent=attachment.get('provider_policy_intent') if type(attachment) is dict else None
                if intent is None: return  # trusted non-applicable path, no policy authority
                if type(intent) is not CapturedProviderPolicyIntent: raise ValueError('provider policy intent unavailable')
                intent.assert_current()
                if (root.owner_account_id!=intent.admission.account_id or context.get('holderID')!=self._managed_provider_generation
                        or not self._direct_foreground_current(attachment,owner=self._owner,host_session=context['stableChatID'],
                            native_session=session,root=root_id,holder=context['holderID'])):
                    raise ValueError('provider policy root changed')
                digest=hashlib.sha256(json.dumps([self._owner,context['stableChatID'],session,root_id,expected_revision],separators=(',',':')).encode()).hexdigest()
                handoff='p08main_'+digest;key=(self._owner,context['stableChatID'],session,root_id,handoff,expected_revision)
                record={'owner':self._owner,'stableChatID':context['stableChatID'],'sessionID':session,'rootSessionID':session,
                    'rootOperationID':root_id,'requestID':root_id,'handoffID':handoff,'actorID':'main',
                    'expectedExecutionRevision':expected_revision,'mapRevision':context['mapRevision'],
                    'mappingRevision':context['mappingRevision'],'workspaceRevision':context['workspaceRevision'],
                    'rawPolicyCeiling':root.raw_policy_ceiling,'ownerAccountID':root.owner_account_id,'recoveryOrigin':False,
                    'queuedReceiptHash':None,'state':'prepared','directMainPolicyIntent':intent,'directMainRoot':root,
                    'directMainAttachment':attachment,'directMainHolder':context['holderID']}
                prior=self._prepared_managed_handoffs.get(key)
                if prior is not None:
                    if (prior.get('state')!='prepared' or prior.get('directMainPolicyIntent') is not intent
                            or prior.get('directMainRoot') is not root or prior.get('directMainAttachment') is not attachment
                            or any(prior.get(field)!=record.get(field) for field in
                                ('owner','stableChatID','sessionID','rootSessionID','rootOperationID','actorID',
                                 'expectedExecutionRevision','mapRevision','mappingRevision','workspaceRevision','directMainHolder'))):
                        raise ValueError('provider policy preflight changed')
                    return
                if len(self._prepared_managed_handoffs)>=512: raise ValueError('managed handoff capacity exhausted')
                self._prepared_managed_handoffs[key]=record
            finally:
                self._provider_root_sync_guard.release()

    def _direct_foreground_current(self, entry, *, owner, host_session, native_session, root, holder):
        if not isinstance(entry, dict) or self._direct_foreground_attachments.get(id(entry.get('lease'))) is not entry:
            return False
        worker = entry['worker']
        lease = entry['lease']
        selection = entry.get('provider_policy_selection')
        if selection is not None:
            try:
                from src.openclank.provider_human_policy import CapturedProviderPolicy
                if (type(selection) is not CapturedProviderPolicy or selection.owner != owner
                        or selection.chat_id != host_session or selection.account_id != entry.get('account')
                        or selection._admission._barrier is not entry.get('barrier')):
                    return False
                selection.assert_current()
            except Exception:
                return False
        pool = getattr(lease, '_pool', None)
        try:
            epoch_current = (pool is not None and pool._owner_lifecycle_epoch(owner) == entry['owner_epoch']
                             and not lease._released
                             and not (entry.get('account_only') is True and getattr(pool,'_s05_foreground_runtime',None) is not None))
        except Exception:
            return False
        context = self._session_context.get(native_session) or {}
        private = entry.get('private_authority')
        mapping_current = (context.get('odysseus_session_id') == host_session
            and context.get('owner') == owner and context.get('private_authority') == private) if private else (
            self._session_map.get(host_session) == native_session)
        return bool(entry.get('root_binding') == (owner, host_session, native_session, root)
            and entry['holder'] == holder == self._managed_provider_generation
            and getattr(worker, '_bridge', None) is self and getattr(worker, '_proc', None) is entry['process']
            and getattr(worker, 'installed_generation', None) == entry['generation']
            and lease._owner_epoch == entry['owner_epoch'] and epoch_current and not entry['admission'].released
            and not getattr(self, '_ownerAdmissionHolderStopped', False)
            and mapping_current)

    async def _actor_owner_prompt_metadata(self, state, *, owner, host_session, native_session, root):
        """Project only the current verified host user, never provider identity."""
        path = 'none'
        def report(reason):
            try: logger.debug('actor owner metadata path=%s result=%s',path,reason)
            except Exception: pass
        try:
            from core.models import get_session_manager_instance
            manager = get_session_manager_instance()
            authority = getattr(getattr(manager, 'lifecycle_context', None), 'auth_manager', None) if manager else None
            host_account = state.owner_account_id
            attachment = state.foreground_attachment
            path = 'root' if host_account is not None else 'attachment' if attachment is not None else 'none'
            if host_account is None and attachment is not None:
                if (not self._direct_foreground_current(attachment, owner=owner, host_session=host_session,
                        native_session=native_session, root=root, holder=self._managed_provider_generation)
                        or not await attachment['barrier'].is_live(owner, attachment['admission'])):
                    report('attachment_unavailable'); return None
                host_account = attachment.get('account')
            if attachment is not None and host_account is not None and attachment.get('account') != host_account:
                report('account_mismatch'); return None
            if not host_account:
                report('account_missing'); return None
            if not authority:
                report('auth_missing'); return None
            if authority.account_id(owner) != host_account:
                report('account_changed'); return None
            if authority.username_for_account_id(host_account) != owner:
                report('owner_changed'); return None
            if self._turns.get(native_session) is not state or state.root_operation_id != root:
                report('root_changed'); return None
            if attachment is not None and not self._direct_foreground_current(attachment, owner=owner,
                    host_session=host_session, native_session=native_session, root=root, holder=self._managed_provider_generation):
                report('attachment_changed'); return None
            report('ready')
            return {'version':1,'sessionID':native_session,'rootOperationID':root,'hostChatID':host_session,'ownerAccountID':host_account}
        except Exception:
            report('error')
        return None

    async def _bind_direct_foreground_root(self, entry, *, owner, host_session, native_session, root):
        from core.models import get_session_manager_instance
        manager = get_session_manager_instance()
        state = getattr(manager, 'lifecycle_context', None) if manager else None
        auth = getattr(state, 'auth_manager', None)
        if (not isinstance(entry, dict) or auth is None or owner != self._owner
                or auth.account_id(owner) != entry.get('account')
                or auth.username_for_account_id(entry.get('account')) != owner
                or not await entry['barrier'].is_live(owner, entry['admission'])):
            raise ValueError('direct foreground account unavailable')
        context = self._session_context.get(native_session) or {}
        private = context.get('private_authority')
        validator = getattr(self, '_private_ghost_is_current', None)
        if private and (not callable(validator) or not await asyncio.to_thread(validator, host_session, owner, self._managed_provider_generation)):
            raise ValueError('direct private foreground unavailable')
        async with self._managed_execution_lock:
            if entry.get('root_binding') is not None:
                raise ValueError('direct foreground root already bound')
            if any(other is not entry and other.get('root_binding') == (owner, host_session, native_session, root)
                    for other in self._direct_foreground_attachments.values()):
                raise ValueError('direct foreground root is ambiguous')
            if private:
                entry['private_authority'] = dict(private)
            entry['root_binding'] = (owner, host_session, native_session, root)
            if not self._direct_foreground_current(entry, owner=owner, host_session=host_session,
                    native_session=native_session, root=root, holder=self._managed_provider_generation):
                entry.pop('root_binding', None)
                raise ValueError('direct foreground root unavailable')
        if (not await entry['barrier'].is_live(owner, entry['admission'])
                or auth.account_id(owner) != entry['account']
                or auth.username_for_account_id(entry['account']) != owner
                or (private and not await asyncio.to_thread(validator, host_session, owner, self._managed_provider_generation))):
            raise ValueError('direct foreground account changed')
        async with self._managed_execution_lock:
            if not self._direct_foreground_current(entry, owner=owner, host_session=host_session,
                    native_session=native_session, root=root, holder=self._managed_provider_generation):
                raise ValueError('direct foreground root changed')
        return entry

    async def _finish_direct_foreground(self, entry, *, terminal):
        """Only exact terminal/known-unsent may settle; unknown retains attachment."""
        async with self._managed_execution_lock:
            key = id(entry['lease'])
            if self._direct_foreground_attachments.get(key) is not entry:
                return False
            if not terminal:
                return False
            worker = entry['worker']
            if (self._managed_provider_generation != entry['holder']
                    or getattr(worker, '_bridge', None) is not self
                    or getattr(worker, '_proc', None) is not entry['process']
                    or getattr(worker, 'installed_generation', None) != entry['generation']
                    or entry['lease']._owner_epoch != entry['owner_epoch']):
                return False
            if entry.get('account_only') is True:
                if (entry['runtime'] is not None or entry['handle'] is not None
                        or getattr(getattr(entry['lease'],'_pool',None),'_s05_foreground_runtime',None) is not None):
                    return False
            else:
                if entry['runtime'] is None or entry['handle'] is None:
                    return False
                entry['runtime'].leave(entry['handle'])
            self._direct_foreground_attachments.pop(key)
        await self._release_owner_admission(entry['barrier'], entry['admission'])
        return True

    async def _release_owner_admissions_after_child_exit(self, holder):
        """Release local ownership only after the supervisor waited this exact child."""
        if not holder or holder != self._managed_provider_generation:
            raise ValueError('managed holder changed')
        async with self._managed_execution_lock:
            with self._stdin_issue_lock: self._ownerAdmissionHolderStopped = True
            tasks=[]; approvals=[]; file_approvals=[]; files_prepared=[]
            for entry in self._managed_actor_turns.values():
                if entry.get('holderID') != holder: continue
                with self._stdin_issue_lock: entry['backgroundStartClosing']=True
                for frame in entry.get('actorFrameCalls',{}).values():
                    frame['invalidated'].set()
                    if isinstance(frame.get('task'),asyncio.Task):tasks.append(frame['task'])
                tasks.extend(record['task'] for record in entry.get('backgroundStartAdmitted',{}).values()
                    if isinstance(record,dict) and isinstance(record.get('task'),asyncio.Task))
                approvals.extend(record['shellApproval'] for record in entry.get('backgroundStartAdmitted',{}).values()
                    if isinstance(record,dict) and isinstance(record.get('shellApproval'),tuple))
                file_approvals.extend(record['fileApproval'] for record in entry.get('backgroundStartAdmitted',{}).values()
                    if isinstance(record,dict) and isinstance(record.get('fileApproval'),tuple))
                files_prepared.extend(record.get('ordinaryPrepared') for record in entry.get('backgroundStartAdmitted',{}).values()
                    if isinstance(record,dict) and record.get('ordinaryPrepared') is not None)
        from src.agent_tools.filesystem_tools import resolve_file_approval
        for request_id,owner,chat,account,pending in file_approvals:
            resolve_file_approval(request_id,'reject',owner=owner,session_id=chat,account_id=account,expected_pending=pending)
        from src.shell_policy import resolve_shell_approval
        for request_id,owner,chat,account in approvals:
            resolve_shell_approval(request_id,'reject',owner=owner,session_id=chat,account_id=account)
        cancelled=False
        for task in tasks:
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: cancelled=True
                except Exception: break
            try: task.result()
            except (Exception,asyncio.CancelledError): pass
        async with self._managed_execution_lock:
            for entry in self._managed_actor_turns.values():
                if entry.get('holderID') == holder:
                    files_prepared.extend(record.get('ordinaryPrepared') for record in entry.get('backgroundStartAdmitted',{}).values()
                        if isinstance(record,dict) and record.get('ordinaryPrepared') is not None)
        cancelled = await _retire_unissued_files_preparations(files_prepared) or cancelled
        async with self._managed_execution_lock:
            if holder != self._managed_provider_generation:
                raise ValueError('managed holder changed')
            entries = []
            for key, direct in list(self._direct_foreground_attachments.items()):
                if (direct['holder'] == holder and direct['worker']._bridge is self
                        and getattr(direct['worker'], '_proc', None) in (None, direct['process'])
                        and getattr(direct['worker'], 'installed_generation', None) == direct['generation']
                        and direct['lease']._owner_epoch == direct['owner_epoch']
                        and direct['process'].returncode is not None):
                    self._direct_foreground_attachments.pop(key)
                    entries.append((direct['barrier'], direct['admission']))
            for entry in self._managed_actor_executions.values():
                if entry.get('holderID') == holder and entry.get('ownerAdmission') is not None:
                    entry['ownerAdmissionStopped'] = True
                    entries.append((entry.get('ownerAdmissionBarrier'), entry.get('ownerAdmission')))
            for record in self._prepared_managed_handoffs.values():
                proof = record.get('displayOwnerAdmission')
                if (proof is not None and proof.get('holder') == holder and proof['worker']._bridge is self
                        and getattr(proof['worker'], '_proc', None) in (None, proof['process'])
                        and getattr(proof['worker'], 'installed_generation', None) == proof['generation']
                        and getattr(proof['process'], 'returncode', None) is not None):
                    record.pop('displayOwnerAdmission')
                    entries.append((proof['barrier'], proof['admission']))
        for barrier, admission in entries:
            await self._release_owner_admission(barrier, admission)
        if cancelled: raise asyncio.CancelledError

    @staticmethod
    async def _release_owner_admission(barrier, admission):
        task = asyncio.create_task(barrier.release(admission))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _session_environment_offer(self, identity, entry):
        """Issue optional managed reminder facts from the current Host owner."""
        non_git_proof = None
        outcome = 'initial_scope'
        def refused(reason, condition):
            nonlocal outcome
            if condition: outcome = reason
            return condition
        try:
            if (refused('initial_actor', identity['actorID'] != 'main') or refused('initial_session', identity['sessionID'] != entry.get('rootSessionID'))
                    or refused('initial_holder_stopped', getattr(self,'_ownerAdmissionHolderStopped',False)) or refused('initial_admission_stopped', entry.get('ownerAdmissionStopped'))
                    or refused('initial_authority', entry.get('authority')) or refused('initial_account_missing', not entry.get('ownerAccountID'))): return None
            outcome = 'owner_services'
            from core.models import get_session_manager_instance
            from src.openclank.workspace_policy_service import resolve_owned_workspace
            manager=get_session_manager_instance(); lifecycle=getattr(manager,'lifecycle_context',None) if manager else None
            auth=getattr(lifecycle,'auth_manager',None) if lifecycle else None
            repository=getattr(lifecycle,'files_policy_repository',None) if lifecycle else None
            supervisor=getattr(lifecycle,'mimo_supervisor',None) if lifecycle else None
            pool=supervisor.owner_lifecycle_pool() if supervisor else None
            owner=entry['owner']; account=entry['ownerAccountID']; worker=pool.worker_for_owner(owner) if pool else None
            holder=self._managed_provider_generation; process=getattr(worker,'_proc',None)
            outcome = 'owner_process'
            if (refused('auth_missing', not auth) or refused('repository_missing', not repository) or refused('worker_missing', not worker)
                    or refused('worker_bridge_changed', worker._bridge is not self) or refused('worker_dead', not worker.is_alive())
                    or refused('holder_missing', not holder) or refused('holder_changed', entry.get('holderID') != holder)
                    or refused('process_missing', process is None) or refused('account_changed', auth.account_id(owner)!=account)
                    or refused('account_owner_changed', auth.username_for_account_id(account)!=owner)): return None
            outcome = 'binding_read'
            binding=await self._handle_session_binding_read({'sessionID':identity['sessionID']})
            outcome = 'binding_scope'
            if (refused('binding_owner', binding.get('owner')!=owner) or refused('binding_chat', binding.get('stableChatID')!=entry.get('stableChatID'))
                    or refused('binding_workspace', binding.get('authorityWorkspaceID')!=entry.get('authorityWorkspaceID'))
                    or any(refused('binding_'+k, binding.get(k)!=entry.get(k)) for k in ('mapRevision','mappingRevision','workspaceRevision'))): return None
            outcome = 'workspace_and_cwd'
            cwd=binding.get('canonicalCwd')
            if entry['authorityWorkspaceID'] == '':
                # Personal metadata is the already admitted managed binding, not
                # a Files grant or a repository/filesystem observation.
                outcome = 'personal_metadata_scope'
                platform=sys.platform if sys.platform in ('linux','darwin','win32') else None
                root=self._turns.get(entry.get('rootSessionID'))
                credential=entry.get('originalCredentialCurrent')
                barrier=getattr(lifecycle,'account_request_barrier',None)
                admission=entry.get('ownerAdmission')
                key=self._managed_turn_key(identity)
                if (not entry.get('forwardPairV1') or not platform or not isinstance(cwd,str) or not 0<len(cwd)<=4096
                        or any(ord(c)<32 or ord(c)==127 or c in '<>' for c in cwd)
                        or not os.path.isabs(cwd) or os.path.normpath(cwd)!=cwd
                        or root is None or root.root_operation_id!=identity['rootOperationID']
                        or root.owner_account_id!=account or not root.forward_pair_eligible or root.privacy_capture_veto
                        or not callable(credential) or credential() is not True
                        or barrier is None or entry.get('ownerAdmissionBarrier') is not barrier
                        or entry.get('ownerAdmissionAccount')!=account or entry.get('state')!='active'
                        or self._managed_actor_executions.get(key[:4]) is not entry): return None
                outcome = 'personal_admission_live'
                if not await barrier.is_live(owner,admission): return None
                outcome = 'personal_binding_recheck'
                current=await self._handle_session_binding_read({'sessionID':identity['sessionID']})
                if (current!=binding or self._managed_actor_turns.get(key) is not entry
                        or self._managed_actor_executions.get(key[:4]) is not entry or entry.get('state')!='active'
                        or getattr(self,'_ownerAdmissionHolderStopped',False) or entry.get('ownerAdmissionStopped')
                        or self._managed_provider_generation!=holder or pool.worker_for_owner(owner) is not worker
                        or worker._bridge is not self or worker._proc is not process or not worker.is_alive()
                        or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
                        or self._turns.get(entry.get('rootSessionID')) is not root
                        or root.root_operation_id!=identity['rootOperationID'] or root.owner_account_id!=account
                        or not root.forward_pair_eligible or root.privacy_capture_veto or credential() is not True): return None
                text='\n'.join(('<system-reminder>','Session environment:',f'- Session directory: {cwd}',
                    '- Repository status: unobserved',f'- Platform: {platform}',
                    'Relative paths in tool calls resolve against the session directory.','</system-reminder>'))
                outcome = 'offered_v3'
                return {'version':3,'constructor':'host-session-personal-env.v1','directory':cwd,
                    'platform':platform,'repositoryStatus':'unobserved',
                    'textSHA256':hashlib.sha256(text.encode('utf-8')).hexdigest()}
            workspace=resolve_owned_workspace(repository,workspace_id=entry['authorityWorkspaceID'],owner_username=owner,
                auth_manager=auth,purpose='agent_workspace')
            if (not isinstance(cwd,str) or not 0<len(cwd)<=4096 or any(ord(c)<32 or ord(c)==127 or c in '<>' for c in cwd)
                    or str(Path(cwd).resolve())!=cwd):return None
            Path(cwd).relative_to(Path(workspace.path).resolve())
            outcome = 'platform'
            platform=sys.platform if sys.platform in ('linux','darwin','win32') else None
            if platform is None:return None
            # This is a read-only Host repository inspection. No Git recovery is used.
            import shutil
            outcome = 'git_availability'
            git=shutil.which('git')
            if not git:return None
            env={'PATH':os.path.dirname(git),'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull}
            outcome = 'git_probe'
            proc=await asyncio.create_subprocess_exec(git,'-C',cwd,'rev-parse','--show-toplevel',
                stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=env)
            try: output,_=await asyncio.wait_for(proc.communicate(),timeout=1.0)
            except BaseException:
                if proc.returncode is None: proc.kill()
                await proc.wait()
                raise
            non_git = False
            if proc.returncode == 0:
                if len(output)>4097:return None
                root=output.decode('utf-8').strip()
                if (not 0<len(root)<=4096 or any(ord(c)<32 or ord(c)==127 or c in '<>' for c in root)
                        or str(Path(root).resolve())!=root):return None
                Path(cwd).relative_to(Path(root));Path(root).relative_to(Path(workspace.path).resolve())
            elif proc.returncode == 128 and output == b'':
                # An error alone is not proof.  The bounded second observation
                # also pins every ancestor and checks that no .git exists.
                from src.openclank.session_environment_probe import capture_non_git
                outcome = 'non_git_proof'
                non_git_proof=await capture_non_git(cwd,git)
                if non_git_proof is None:return None
                root=None;non_git=True
            else:return None
            text='\n'.join(('<system-reminder>','Session environment:',f'- Session directory: {cwd}',
                '- Not inside a git repository' if non_git else f'- Git repository root: {root}',f'- Platform: {platform}',
                'Relative paths in tool calls resolve against the session directory.','</system-reminder>'))
            outcome = 'post_binding_read'
            current=await self._handle_session_binding_read({'sessionID':identity['sessionID']})
            outcome = 'post_owner_currentness'
            if (refused('post_binding_changed', current!=binding) or refused('post_turn_changed', self._managed_actor_turns.get(self._managed_turn_key(identity)) is not entry)
                    or refused('post_holder_stopped', getattr(self,'_ownerAdmissionHolderStopped',False)) or refused('post_admission_stopped', entry.get('ownerAdmissionStopped'))
                    or refused('post_holder_changed', self._managed_provider_generation!=holder) or refused('post_worker_changed', pool.worker_for_owner(owner) is not worker)
                    or refused('post_bridge_changed', worker._bridge is not self) or refused('post_process_changed', worker._proc is not process)
                    or refused('post_worker_dead', not worker.is_alive()) or refused('post_account_changed', auth.account_id(owner)!=account)
                    or refused('post_account_owner_changed', auth.username_for_account_id(account)!=owner)):return None
            if non_git:
                outcome = 'non_git_admission_currentness'
                barrier=getattr(lifecycle,'account_request_barrier',None)
                admission=entry.get('ownerAdmission')
                key=self._managed_turn_key(identity)
                if (refused('barrier_missing', barrier is None) or refused('barrier_changed', entry.get('ownerAdmissionBarrier') is not barrier)
                        or refused('admission_account_changed', entry.get('ownerAdmissionAccount')!=account) or refused('admission_not_active', entry.get('state')!='active')
                        or refused('execution_changed', self._managed_actor_executions.get(key[:4]) is not entry)):return None
                outcome = 'non_git_admission_live'
                if not await barrier.is_live(owner,admission):return None
                outcome = 'non_git_workspace_currentness'
                if resolve_owned_workspace(repository,workspace_id=entry['authorityWorkspaceID'],owner_username=owner,
                        auth_manager=auth,purpose='agent_workspace')!=workspace:return None
                outcome = 'non_git_post_await_currentness'
                if (refused('final_turn_changed', self._managed_actor_turns.get(key) is not entry) or refused('final_state_inactive', entry.get('state')!='active')
                        or refused('final_holder_stopped', getattr(self,'_ownerAdmissionHolderStopped',False)) or refused('final_admission_stopped', entry.get('ownerAdmissionStopped'))
                        or refused('final_holder_changed', self._managed_provider_generation!=holder) or refused('final_bridge_changed', worker._bridge is not self)
                        or refused('final_process_changed', worker._proc is not process) or refused('final_worker_dead', not worker.is_alive())
                        or refused('final_account_changed', auth.account_id(owner)!=account) or refused('final_account_owner_changed', auth.username_for_account_id(account)!=owner)):return None
                outcome = 'non_git_ancestry_currentness'
                if not non_git_proof.current():return None
            outcome = 'offered_v2' if non_git else 'offered_v1'
            return {'version':2 if non_git else 1,'constructor':'host-session-env-reminder.v1','directory':cwd,'gitRoot':root,
                'platform':platform,'textSHA256':hashlib.sha256(text.encode('utf-8')).hexdigest()}
        except Exception:
            outcome = outcome + '_exception'
            return None
        finally:
            try: logger.debug('session_environment_offer outcome=%s', outcome)
            except Exception: pass
            if non_git_proof is not None:
                non_git_proof.close()

    async def _handle_connector_dispatch_admit(self, params: dict) -> dict:
        """Recheck the original ingress for the exact still-active managed turn."""
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/dispatch/admit'
        request = validate_managed_method_request(method, params)
        key = self._managed_turn_key(request)
        try:
            entry = self._managed_actor_turns.get(key)
            if (not isinstance(entry, dict) or entry.get('state') != 'active'
                    or entry.get('holderID') != self._managed_provider_generation
                    or self._managed_actor_executions.get(key[:4]) is not entry):
                raise ValueError('original ingress unavailable')
            connector = entry.get('originalIngressRequired') is True
            media = None if connector else _current_media_derivative_identity(self,entry,self._managed_provider_generation)
            if not connector and media is None:
                raise ValueError('media admission unavailable')
            current = entry.get('originalCredentialCurrent')
            if not callable(current) or current() is not True:
                raise ValueError('original ingress changed')
            resolved = await self.resolve_managed_actor_execution(
                request['sessionID'], request['rootOperationID'], request['actorID'], request['executionRevision'])
            if (self._managed_actor_turns.get(key) is not entry
                    or self._managed_actor_executions.get(key[:4]) is not entry
                    or any(resolved.get(field) != entry.get(field) for field in (
                        'owner', 'holderID', 'stableChatID', 'rootSessionID', 'sessionID',
                        'rootOperationID', 'actorID', 'executionRevision', 'workspaceRevision', 'mappingRevision'))
                    or entry.get('state') != 'active' or entry.get('originalCredentialCurrent') is not current
                    or current() is not True
                    or (not connector and _current_media_derivative_identity(self,entry,self._managed_provider_generation) != media)):
                raise ValueError('original ingress changed')
            result = {'state': 'current'}
        except Exception:
            result = {'state': 'stale'}
        return validate_managed_method_result(method, result)

    async def _handle_managed_turn_start(self, params: dict) -> dict:
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import validate_managed_method_request
        request = validate_managed_method_request('_openclank/session/v1/turn/start', params)
        key = self._managed_turn_key(request)
        existing = self._managed_actor_executions.get(key[:4])
        manager = get_session_manager_instance()
        state = getattr(manager, 'lifecycle_context', None) if manager else None
        barrier = getattr(state, 'account_request_barrier', None)
        auth = getattr(state, 'auth_manager', None)
        admission = None
        candidate = False
        account = None
        if existing is not None:
            barrier = existing.get('ownerAdmissionBarrier')
            admission = existing.get('ownerAdmission')
            account = existing.get('ownerAdmissionAccount')
            if admission is not None and (barrier is None or not await barrier.is_live(self._owner, admission)):
                raise ValueError('managed execution owner admission unavailable')
        elif barrier is not None and auth is not None:
            account = auth.account_id(self._owner)
            if not account or auth.username_for_account_id(account) != self._owner:
                raise ValueError('managed execution account unavailable')
            admission = await barrier.acquire(self._owner)
            candidate = True
        try:
            if admission is not None and (auth is None or auth.account_id(self._owner) != account
                    or auth.username_for_account_id(account) != self._owner):
                raise ValueError('managed execution account changed')
            return await self._start_with_owner_admission(params, existing, barrier, admission, account)
        finally:
            if candidate and not any(item.get('ownerAdmission') is admission for item in self._managed_actor_executions.values()):
                await self._release_owner_admission(barrier, admission)

    async def _start_with_owner_admission(self, params: dict, original_execution, admission_barrier, admission, admission_account) -> dict:
        from src.openclank.managed_protocol import (
            validate_managed_method_request,
            validate_managed_method_result,
        )

        method = "_openclank/session/v1/turn/start"
        request = validate_managed_method_request(method, params)
        fields = ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')
        identity = {field:request[field] for field in fields}
        key = self._managed_turn_key(identity)
        async def replay(prior):
            resolver = getattr(self, '_managed_replay_native_policy_resolver', None)
            try:
                native = await resolver(identity, prior) if callable(resolver) else None
            except Exception:
                native = None
            async with self._managed_execution_lock:
                if (self._managed_actor_turns.get(key) is not prior or prior.get('state') != 'active'
                        or prior.get('ownerAdmissionStopped') or getattr(self, '_ownerAdmissionHolderStopped', False)):
                    raise ValueError('managed replay turn unavailable')
                try:
                    from core.models import get_session_manager_instance
                    manager = get_session_manager_instance()
                    lifecycle = getattr(manager, 'lifecycle_context', None) if manager else None
                    auth = getattr(lifecycle, 'auth_manager', None)
                    owner, account = prior.get('owner'), prior.get('ownerAccountID')
                    current_native = (native[0] if isinstance(native, tuple) and len(native) == 5
                        and auth and owner and account and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                        and getattr(lifecycle, 'files_policy_repository', None) is native[2]
                        and prior.get('policyRevision') == native[1] and native[2].generation() == native[3]
                        and prior.get('originalCredentialCurrent') is native[4] and callable(native[4]) and native[4]()
                        and not getattr(prior.get('ownerAdmission'), 'released', True)
                        and prior.get('holderID') == self._managed_provider_generation else None)
                except Exception:
                    current_native = None
                return validate_managed_method_result(method, {"accepted": True, "replayed": True, "originalIngressRequired": bool(prior.get("originalIngressRequired")), **identity,
                    **({'nativeToolPolicy':current_native} if isinstance(current_native, dict) else {}),
                    **({'forwardPairV1':True} if prior.get('forwardPairV1') else {}),
                    **({'sessionEnvironment':prior['sessionEnvironment']} if prior.get('sessionEnvironment') else {}),
                    **({'mediaSourcesV1':value} if (value:=_current_media_offer(prior,self._managed_provider_generation)) else {}),
                    **({'mediaDerivativesV1':value} if (value:=_current_media_derivative_offer(self,prior,self._managed_provider_generation)) else {}),
                    **({'themeStatusOnly':True} if prior.get('themeStatusOnly') else {}),
                    **({'codeActivityEnabled':True} if prior.get('codeActivityEnabled') else {})})
        async with self._managed_execution_lock:
            if getattr(self, '_ownerAdmissionHolderStopped', False):
                raise ValueError('managed execution holder stopped')
            prior = self._managed_actor_turns.get(key)
            if prior is None and key in self._managed_finished_turns:
                raise ValueError("managed actor turn identity has already finished")
        if prior is not None:
            return await replay(prior)
        resolved = await self.resolve_managed_actor_execution(
            identity["sessionID"], identity["rootOperationID"],
            identity["actorID"], identity["executionRevision"],
        )
        if not resolved.get("holderID"):
            raise ValueError("managed provider generation is unavailable")
        prepared_key = None
        for candidate, record in self._prepared_managed_handoffs.items():
            if (
                record.get("owner") == self._owner
                and record.get("sessionID") == identity["sessionID"]
                and record.get("rootOperationID") == identity["rootOperationID"]
                and record.get("actorID") == identity["actorID"]
                and record.get("expectedExecutionRevision") + 1 == identity["executionRevision"]
                and record.get("state") == "prepared"
            ):
                if (any(record.get(field) != resolved.get(field) for field in (
                    "stableChatID", "rootSessionID", "mappingRevision", "workspaceRevision"
                )) or record.get("mapRevision", -1) > resolved.get("mapRevision", -1)):
                    raise ValueError("managed handoff authority changed before start")
                prepared_key = candidate
                break
        display_proof = self._prepared_managed_handoffs.get(prepared_key, {}).get('displayOwnerAdmission') if prepared_key is not None else None
        if display_proof is not None and (display_proof['holder'] != resolved.get('holderID')
                or not await self._display_owner_admission_current(display_proof)):
            raise ValueError('original display owner admission changed')
        admitted_execution = self._managed_actor_executions.get(key[:4])
        if prepared_key is None and admitted_execution is None and resolved["actorID"] != "main" and resolved["parentActorID"]:
            # The resolver already verified native parent edges. Every child
            # admission must additionally descend from a still-active actor
            # execution under this root, not merely from a historical root.
            lineage = {(row["sessionID"], row["actorID"], row["executionRevision"]) for row in resolved["lineage"] if row.get("status") == "running"}
            if not any(
                entry.get("rootOperationID") == identity["rootOperationID"]
                and (entry.get("sessionID"), entry.get("actorID"), entry.get("executionRevision")) in lineage
                for entry in self._managed_actor_executions.values()
            ):
                raise ValueError("managed child turn has no active admitted actor ancestor")
        if prepared_key is None and admitted_execution is None and not any(
            context.get("root_operation_id") == identity["rootOperationID"]
            and session_id in self._turns
            for session_id, context in self._session_context.items()
        ) and not any(
            entry.get("rootOperationID") == identity["rootOperationID"]
            for entry in self._managed_actor_executions.values()
        ):
            raise ValueError("managed turn root is no longer active or reserved")
        # Preserve the authenticated root ceiling with the execution, never
        # reacquire it from a later foreground turn. Descendants inherit the
        # nearest admitted ancestor's immutable host upper bound.
        execution_key = key[:4]
        existing_execution = self._managed_actor_executions.get(execution_key)
        raw_ceiling = existing_execution.get("rawPolicyCeiling") if existing_execution else None
        if raw_ceiling is None and resolved["sessionID"] == resolved["rootSessionID"] and resolved["actorID"] == "main":
            root_state = self._turns.get(resolved["rootSessionID"])
            raw_ceiling = getattr(root_state, "raw_policy_ceiling", None)
        if raw_ceiling is None:
            lineage = [(row["sessionID"], row["actorID"], row["executionRevision"]) for row in resolved["lineage"][1:]]
            for ancestor in lineage:
                match = next((item for item in self._managed_actor_executions.values()
                    if item.get("rootOperationID") == identity["rootOperationID"]
                    and (item.get("sessionID"), item.get("actorID"), item.get("executionRevision")) == ancestor), None)
                if match and match.get("rawPolicyCeiling") is not None:
                    raw_ceiling = match["rawPolicyCeiling"]
                    break
        if raw_ceiling is None and prepared_key is not None:
            raw_ceiling = self._prepared_managed_handoffs[prepared_key].get("rawPolicyCeiling")
        # The current input receipt is host-signed and bound to this root. Children
        # inherit only an already-admitted ancestor's frozen owner account.
        owner_account = existing_execution.get('ownerAccountID') if existing_execution else None
        if owner_account is None and prepared_key is not None:
            owner_account = self._prepared_managed_handoffs[prepared_key].get('ownerAccountID')
        root_state = self._turns.get(resolved['rootSessionID'])
        if owner_account is None and root_state is not None and root_state.root_operation_id == identity['rootOperationID']:
            owner_account = root_state.owner_account_id
        if owner_account is None:
            lineage = {(row['sessionID'], row['actorID'], row['executionRevision']) for row in resolved['lineage'][1:]}
            owner_account = next((item.get('ownerAccountID') for item in self._managed_actor_executions.values()
                if item.get('rootOperationID') == identity['rootOperationID']
                and (item.get('sessionID'), item.get('actorID'), item.get('executionRevision')) in lineage
                and item.get('ownerAccountID')), None)
        # The credential witness belongs to this exact original root or its
        # admitted ancestry; a later foreground turn must never replace it.
        original_credential = existing_execution.get('originalCredentialCurrent') if existing_execution else None
        if original_credential is None and prepared_key is not None:
            original_credential = self._prepared_managed_handoffs[prepared_key].get('originalCredentialCurrent')
        if (original_credential is None and root_state is not None
                and root_state.root_operation_id == identity['rootOperationID']
                and root_state.owner_account_id == owner_account
                and (prepared_key is None or self._prepared_managed_handoffs[prepared_key].get('directMainRoot') is root_state)):
            original_credential = root_state.original_credential_current
        if original_credential is None and prepared_key is None:
            lineage = {(row['sessionID'], row['actorID'], row['executionRevision']) for row in resolved['lineage'][1:]}
            original_credential = next((item.get('originalCredentialCurrent') for item in self._managed_actor_executions.values()
                if item.get('state') == 'active' and item.get('rootOperationID') == identity['rootOperationID']
                and item.get('ownerAccountID') == owner_account
                and (item.get('sessionID'), item.get('actorID'), item.get('executionRevision')) in lineage
                and item.get('originalCredentialCurrent')), None)
        recovery_origin = bool((existing_execution and existing_execution.get('recoveryOrigin')) or
            (prepared_key is not None and self._prepared_managed_handoffs[prepared_key].get('recoveryOrigin')))
        if not recovery_origin:
            lineage = {(row['sessionID'], row['actorID'], row['executionRevision']) for row in resolved['lineage'][1:]}
            recovery_origin = any(item.get('recoveryOrigin') and item.get('rootOperationID')==identity['rootOperationID']
                and (item.get('sessionID'),item.get('actorID'),item.get('executionRevision')) in lineage
                for item in self._managed_actor_executions.values())
        if recovery_origin and not owner_account:
            raise ValueError('managed recovery account unavailable')
        foreground_direct = None
        foreground_ancestor = None
        foreground_account = existing_execution.get('foregroundAccountID') if existing_execution else None
        if foreground_account is None and display_proof is not None:
            foreground_account = display_proof['account']
        if foreground_account is None and prepared_key is not None:
            foreground_account = self._prepared_managed_handoffs[prepared_key].get('foregroundAccountID')
        if foreground_account is None and owner_account is not None:
            foreground_account = owner_account
        if foreground_account is None and resolved['actorID'] == 'main' and root_state is not None:
            direct = root_state.foreground_attachment
            if (root_state.root_operation_id == identity['rootOperationID'] and direct is not None
                    and self._direct_foreground_current(direct, owner=self._owner,
                        host_session=resolved['stableChatID'], native_session=resolved['rootSessionID'],
                        root=identity['rootOperationID'], holder=resolved['holderID'])):
                foreground_account = direct['account']
                foreground_direct = direct
        if foreground_account is None:
            for ancestor in resolved['lineage'][1:]:
                if ancestor.get('status') != 'running':
                    continue
                key_tuple = (ancestor['sessionID'], ancestor['actorID'], ancestor['executionRevision'])
                candidates = [item for item in self._managed_actor_executions.values()
                    if item.get('state') == 'active' and item.get('rootOperationID') == identity['rootOperationID']
                    and item.get('holderID') == resolved['holderID']
                    and (item.get('sessionID'), item.get('actorID'), item.get('executionRevision')) == key_tuple
                    and item.get('foregroundAccountID')]
                if len(candidates) > 1:
                    raise ValueError('managed foreground ancestry is ambiguous')
                if candidates:
                    foreground_ancestor = candidates[0]
                    foreground_account = foreground_ancestor['foregroundAccountID']
                    break
        entry = {
            **resolved,
            "forwardPairV1": bool(root_state is not None and root_state.root_operation_id == identity['rootOperationID']
                and root_state.forward_pair_eligible and owner_account and not resolved.get('authority') and resolved['actorID'] == 'main'
                and resolved['sessionID'] == resolved['rootSessionID'] and (prepared_key is None or
                    self._prepared_managed_handoffs[prepared_key].get('directMainPolicyIntent') is not None)),
            "foregroundAccountID": foreground_account,
            "ownerAccountID": owner_account,
            "originalCredentialCurrent": original_credential,
            "originalIngressRequired": bool((root_state is not None and root_state.root_operation_id == identity['rootOperationID'] and root_state.connector_ingress)
                or (existing_execution and existing_execution.get('originalIngressRequired'))
                or any(item.get('originalIngressRequired') and item.get('rootOperationID') == identity['rootOperationID']
                    and item.get('owner') == self._owner and item.get('state') == 'active'
                    for item in self._managed_actor_executions.values())),
            "recoveryOrigin": recovery_origin,
            "queuedHandoff": ({'requestID':self._prepared_managed_handoffs[prepared_key].get('requestID'),
                'payloadHash':self._prepared_managed_handoffs[prepared_key].get('queuedReceiptHash')}
                if prepared_key is not None and self._prepared_managed_handoffs[prepared_key].get('queuedReceiptHash') else None),
            "themeStatusOnly": bool((existing_execution and existing_execution.get('themeStatusOnly')) or
                (root_state is not None and root_state.root_operation_id == identity['rootOperationID'] and root_state.theme_status_only)),
            "codeActivityEnabled": bool(owner_account and root_state is not None
                and root_state.root_operation_id == identity['rootOperationID']
                and root_state.owner_account_id == owner_account
                and root_state.code_activity_workspace_id == resolved['authorityWorkspaceID']),
            "maintenancePromptBoundary": (root_state.maintenance_prompt_boundary
                if root_state is not None and root_state.root_operation_id == identity['rootOperationID']
                and resolved['actorID'] == 'main' else None),
            "generatedSummaryBoundary": (root_state.generated_summary_boundary
                if root_state is not None and root_state.root_operation_id == identity['rootOperationID']
                and resolved['actorID'] == 'main' else None),
            "rawPolicyCeiling": json.loads(json.dumps(raw_ceiling)) if raw_ceiling is not None else None,
            "innerTurnID": identity["innerTurnID"],
            "executionRevision": identity["executionRevision"],
            "state": "active",
            "directMainPolicyHandoff": (prepared_key if prepared_key is not None and
                self._prepared_managed_handoffs[prepared_key].get('directMainPolicyIntent') is not None else
                (existing_execution.get('directMainPolicyHandoff') if existing_execution else None)),
        }
        async with self._managed_execution_lock:
            if getattr(self, '_ownerAdmissionHolderStopped', False):
                raise ValueError('managed execution holder stopped')
            prior = self._managed_actor_turns.get(key)
            if prior is None:
                if key in self._managed_finished_turns:
                    raise ValueError("managed actor turn identity has already finished")
                if len(self._managed_actor_turns) >= 4096:
                    raise ValueError("managed actor execution capacity is exhausted")
                if prepared_key is not None:
                    reservation = self._prepared_managed_handoffs.get(prepared_key)
                    if reservation is None or reservation.get("state") != "prepared":
                        raise ValueError("managed handoff reservation changed before start")
                    if reservation.get('displayOwnerAdmission') is not display_proof:
                        raise ValueError('original display owner reservation changed')
                execution_key = key[:4]
                if any(other_key[0] == key[0] and other_key[2] == key[2] and other_key != execution_key
                       for other_key in self._managed_actor_executions):
                    raise ValueError("a prior managed actor execution requires reconciliation")
                if any(turn[:4] == execution_key for turn in self._managed_actor_turns):
                    raise ValueError("managed actor execution already has an active inner turn")
                if execution_key in self._managed_finished_executions:
                    raise ValueError("managed actor execution has already settled")
                runtime = getattr(self, '_s05_foreground_runtime', None)
                previous_execution = self._managed_actor_executions.get(execution_key)
                if admission is not None:
                    from core.models import get_session_manager_instance
                    manager = get_session_manager_instance()
                    current_state = getattr(manager, 'lifecycle_context', None) if manager else None
                    current_auth = getattr(current_state, 'auth_manager', None)
                    if (current_auth is None or current_auth.account_id(self._owner) != admission_account
                            or current_auth.username_for_account_id(admission_account) != self._owner):
                        raise ValueError('managed execution account changed')
                if previous_execution is not None and previous_execution.get('ownerAdmissionStopped'):
                    raise ValueError('managed execution holder stopped')
                if original_execution is not None and previous_execution is not original_execution:
                    raise ValueError('managed execution admission changed')
                if previous_execution is not None:
                    for field in ('ownerAdmissionBarrier','ownerAdmission','ownerAdmissionAccount'):
                        if field in previous_execution:
                            entry[field] = previous_execution[field]
                elif admission is not None:
                    if owner_account is not None and owner_account != admission_account:
                        raise ValueError('managed execution account changed')
                    entry.update(ownerAdmissionBarrier=admission_barrier, ownerAdmission=admission,
                                 ownerAdmissionAccount=admission_account)
                if display_proof is not None:
                    pool, worker, account = display_proof['pool'], display_proof['worker'], display_proof['account']
                    pool._assert_owner_lifecycle(pool._key(self._owner), display_proof['epoch'])
                    state = pool._states.get(pool._key(self._owner))
                    if (previous_execution is not None or account != admission_account or state is None or state.active is not worker
                            or state.generation != display_proof['generation'] or worker._bridge is not self
                            or worker._proc is not display_proof['process'] or not worker.is_alive()
                            or self._managed_provider_generation != display_proof['holder']
                            or display_proof['auth'].account_id(self._owner) != account
                            or display_proof['auth'].username_for_account_id(account) != self._owner):
                        raise ValueError('original display owner changed before transfer')
                    entry.update(ownerAdmissionBarrier=display_proof['barrier'], ownerAdmission=display_proof['admission'],
                                 ownerAdmissionAccount=account, displayOwnerAccountID=account, displayOwnerProof=display_proof)
                if previous_execution is not None and previous_execution.get('foregroundRuntime') is not runtime:
                    raise ValueError('managed actor occupancy changed')
                if runtime is not None:
                    from src.openclank.memory_job_admission import ForegroundRuntime
                    if foreground_direct is not None and not self._direct_foreground_current(
                            foreground_direct, owner=self._owner, host_session=resolved['stableChatID'],
                            native_session=resolved['rootSessionID'], root=identity['rootOperationID'], holder=resolved['holderID']):
                        raise ValueError('managed foreground root changed')
                    if foreground_ancestor is not None and not any(
                            item is foreground_ancestor and item.get('state') == 'active'
                            for item in self._managed_actor_executions.values()):
                        raise ValueError('managed foreground ancestry changed')
                    if (type(runtime) is not ForegroundRuntime or not admission_account
                            or foreground_account != admission_account):
                        raise ValueError('managed actor occupancy unavailable')
                    if previous_execution is not None:
                        if previous_execution.get('foregroundRuntime') is not runtime or not previous_execution.get('foregroundHandle'):
                            raise ValueError('managed actor occupancy changed')
                        entry['foregroundRuntime'] = runtime
                        entry['foregroundHandle'] = previous_execution['foregroundHandle']
                    else:
                        connection_id = self._managed_actor_foreground_connection(identity, resolved)
                        entry['foregroundRuntime'] = runtime
                        from src.memory_scope import memory_owner
                        entry['foregroundHandle'] = runtime.enter(owner=memory_owner(self._owner), account_id=admission_account, connection_id=connection_id)
                if prepared_key is not None:
                    reservation['state'] = 'claimed'
                    reservation['claimedExecutionRevision'] = identity['executionRevision']
                    reservation.pop('displayOwnerAdmission', None)
                self._managed_actor_executions[execution_key] = entry
                self._managed_actor_turns[key] = entry
        if prior is not None:
            return await replay(prior)
        if runtime is None and not resolved.get("authority"):
            from src.openclank.memory_job_admission import observe_shadow
            shadow_id = hashlib.sha256(json.dumps([self._owner, resolved["holderID"], *key[:4]], separators=(",", ":")).encode()).hexdigest()
            observe_shadow(self._memory_provider, operation_id="actor:" + shadow_id, owner=self._owner,
                           connection_id="unknown", generation=resolved["holderID"], event="start", account_id=admission_account)
        # Refresh current host authority against the immutable execution ceiling.
        # Missing or failed authority leaves Browser denied for this exact turn.
        policy_resolver = getattr(self, "_managed_start_policy_resolver", None)
        if policy_resolver is not None and entry.get("rawPolicyCeiling") is not None:
            try:
                await policy_resolver(identity, entry)
            except Exception:
                logger.warning("managed first-turn Browser policy unavailable", exc_info=True)
        if entry.get('themeStatusOnly'):
            try:
                decision = await self._handle_managed_tool_admit({**identity, 'toolName':'theme_emoji_assets',
                    'kind':'tool', 'toolOperation':'status'})
                async with self._managed_execution_lock:
                    if self._managed_actor_turns.get(key) is entry and decision.get('allowed') is True:
                        entry['themeStatusVisible'] = True
            except Exception:
                pass
        current_display_owner = None
        if display_proof is not None and await self._display_owner_admission_current(display_proof):
            async with self._managed_execution_lock:
                if (self._managed_actor_turns.get(key) is entry and entry.get('state') == 'active'
                        and not entry.get('ownerAdmissionStopped') and entry.get('displayOwnerProof') is display_proof
                        and display_proof['pool']._owner_lifecycle_epoch(display_proof['pool']._key(self._owner)) == display_proof['epoch']
                        and getattr(display_proof['pool']._states.get(display_proof['pool']._key(self._owner)), 'active', None) is display_proof['worker']
                        and display_proof['worker']._bridge is self and display_proof['worker']._proc is display_proof['process']
                        and display_proof['worker'].is_alive() and self._managed_provider_generation == display_proof['holder']
                        and display_proof['auth'].account_id(self._owner) == display_proof['account']
                        and display_proof['auth'].username_for_account_id(display_proof['account']) == self._owner):
                    current_display_owner = display_proof['account']
        factory_persona = None
        _persona_trace=logger.isEnabledFor(logging.DEBUG)
        _persona_stage='offer_gate'; _persona_started=time.monotonic() if _persona_trace else 0.0
        _persona_timings={} if _persona_trace else None
        _persona_outcome='unavailable'
        def _persona_phase(name):
            nonlocal _persona_stage, _persona_started
            if _persona_timings is not None:
                now=time.monotonic(); _persona_timings[_persona_stage]=round((now-_persona_started)*1000,3); _persona_started=now
            _persona_stage=name
        if entry.get('forwardPairV1') and root_state is not None and root_state.factory_persona_source is not None:
            try:
                # One budget for this optional read; timed-out thread work may
                # finish independently but cannot publish a late offer.
                async with asyncio.timeout(0.2):
                    # The retained sealed root is historical constructor evidence, not
                    # an authorization; this offer requires fresh current policy/C2.
                    from src.default_persona import read_factory_envelope
                    from src.openclank.memory_policy import STORE
                    from core.models import get_session_manager_instance
                    manager = get_session_manager_instance()
                    lifecycle = getattr(manager, 'lifecycle_context', None) if manager else None
                    auth = getattr(lifecycle, 'auth_manager', None) if lifecycle else None
                    _persona_phase("initial_resolve")
                    before = await self._resolve_standing_guidance_turn(identity)
                    root = self._turns.get(resolved['rootSessionID'])
                    if (entry.get('forwardPairV1') and root is root_state and root is not None and
                            root.root_operation_id == identity['rootOperationID'] and not root.privacy_capture_veto and
                            root.forward_pair_eligible and resolved.get('holderID') == self._managed_provider_generation and
                            owner_account and owner_account == root.owner_account_id and admission is not None and admission_barrier is not None and auth is not None and
                            auth.account_id(self._owner) == owner_account and auth.username_for_account_id(owner_account) == self._owner):
                        privileges = auth.get_privileges(self._owner) or {}
                        memory_privilege = bool(privileges.get('can_manage_memory', True))
                        _persona_phase("policy")
                        policy = await asyncio.to_thread(STORE.capture_policy_snapshot,self._owner,read_privilege=memory_privilege,
                            write_privilege=memory_privilege,incognito=False,no_memory=False,compare_mode=False)
                        _persona_phase("envelope")
                        candidate = read_factory_envelope(root.factory_persona_source,owner=self._owner,account_id=owner_account,
                            chat_id=resolved['stableChatID'],root_operation_id=identity['rootOperationID'])
                        _persona_phase("fresh_policy")
                        fresh = await asyncio.to_thread(STORE.capture_policy_snapshot,self._owner,read_privilege=memory_privilege,
                            write_privilege=memory_privilege,incognito=False,no_memory=False,compare_mode=False)
                        _persona_phase("final_checks")
                        if (candidate and policy.get('eligibility_state') == 'available' and
                                policy.get('capture_allowed') is True and policy.get('fingerprint') == fresh.get('fingerprint') and
                                fresh.get('eligibility_state') == 'available' and fresh.get('capture_allowed') is True and
                                await admission_barrier.is_live(self._owner, admission) and
                                await self._resolve_standing_guidance_turn(identity) == before and
                                self._turns.get(resolved['rootSessionID']) is root and not root.privacy_capture_veto and
                                self._managed_provider_generation == resolved.get('holderID') and
                                auth.account_id(self._owner) == owner_account and auth.username_for_account_id(owner_account) == self._owner and
                                bool((auth.get_privileges(self._owner) or {}).get('can_manage_memory', True)) == memory_privilege):
                            factory_persona = candidate
                            _persona_outcome='ready'
            except TimeoutError:
                _persona_outcome='timeout'
                factory_persona = None  # Optional provenance never changes ordinary admission.
            except Exception:
                _persona_outcome='error'
                factory_persona = None
        if _persona_timings is not None and factory_persona is None:
            try:
                _persona_timings[_persona_stage]=round((time.monotonic()-_persona_started)*1000,3)
                logger.debug('factory persona offer outcome=%s phase=%s timings_ms=%s',_persona_outcome,_persona_stage,_persona_timings)
            except Exception: pass
        media_offer=None
        if (root_state is not None and identity['actorID']=='main' and identity['sessionID']==resolved['rootSessionID']
                and root_state.root_operation_id==identity['rootOperationID'] and root_state.owner_account_id==owner_account
                and root_state.media_source_offer and root_state.media_source_offer.get('holder')==self._managed_provider_generation
                and callable(original_credential)):
            try:
                if original_credential() is True:
                    media_offer={'version':1,'rootOperationID':identity['rootOperationID'],'items':root_state.media_source_offer['items']}
                    async with self._managed_execution_lock:
                        if self._managed_actor_turns.get(key) is entry: entry['mediaSourcesV1']=media_offer
                        else: media_offer=None
            except Exception: media_offer=None
        media_derivatives = None
        if (media_offer is not None and root_state is not None and root_state.media_view_offer
                and entry.get('originalIngressRequired') is not True):
            async with self._managed_execution_lock:
                if self._managed_actor_turns.get(key) is entry:
                    entry['_mediaViewContext'] = root_state.media_view_offer
                    media_derivatives = _current_media_derivative_offer(self,entry,self._managed_provider_generation)
                    if media_derivatives is None: entry.pop('_mediaViewContext',None)
        offer=await self._session_environment_offer(identity,entry)
        offer_at_owner = offer is not None
        if offer is not None:
            async with self._managed_execution_lock:
                if self._managed_actor_turns.get(key) is entry:
                    entry['sessionEnvironment']=offer
                else:
                    offer=None
        browser_offer = None
        try:
            # Optional representation only; current policy, route, owner and the actual worker lease remain independent authorities.
            from src.agent_settings import AdmittedBrowserSnapshotPreference, agent_settings_revision, validate_agent_settings
            from src.openclank.mimo_supervisor import AgentWorkerLease
            if (identity['actorID'] == 'main' and identity['sessionID'] == resolved['rootSessionID']
                    and root_state is not None and root_state.root_operation_id == identity['rootOperationID']
                    and root_state.owner_account_id == owner_account and not root_state.provider_root_closing):
                retained = root_state.browser_snapshot_preference
                if type(retained) is tuple and len(retained) == 3:
                    pref, lease, holder = retained
                    def browser_settings_current():
                        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
                        with PREFERENCE_LOCK:
                            settings = validate_agent_settings((_load_for_user(self._owner) or {}).get('agent_settings', {}))
                            return settings.get('tokenomics', {}).get('browser_snapshot_repeat') is True and agent_settings_revision(settings) == pref.revision
                    if (type(pref) is AdmittedBrowserSnapshotPreference and pref.requested is True
                            and type(lease) is AgentWorkerLease and not lease._released and lease.owner == self._owner
                            and lease.worker._bridge is self and lease.worker.is_alive() and holder == self._managed_provider_generation
                            and holder == resolved.get('holderID') and await asyncio.to_thread(browser_settings_current)):
                        binding = self._managed_actor_provider_binding(identity, resolved)
                        bounded = ((owner_account,256),(holder,256),(binding['connection_id'],256),
                                   (binding['provider_id'],256),(binding['model_id'],512),(binding['model_route_id'],256))
                        if (binding['model_id'] == pref.model_id and pref.provider_id in (binding['provider_id'], binding['connection_id'])
                                and all(type(value) is str and value and len(value)<=limit for value,limit in bounded)):
                            candidate = {'version':1, **identity, 'hostAccountID':owner_account, 'holderID':holder,
                                'settingsRevision':pref.revision, 'connectionID':binding['connection_id'],
                                'providerID':binding['provider_id'], 'modelID':binding['model_id'], 'modelRouteID':binding['model_route_id']}
                            async with self._managed_execution_lock:
                                if (self._managed_actor_turns.get(key) is entry and entry.get('state') == 'active'
                                        and self._turns.get(resolved['rootSessionID']) is root_state
                                        and root_state.browser_snapshot_preference is retained and not lease._released
                                        and holder == self._managed_provider_generation):
                                    entry['browserSnapshotRepeatAdmission'] = candidate
                                    browser_offer = candidate
        except Exception:
            browser_offer = None
        # The optional offer is inert until both the registered protocol and
        # the existing settings owner declare the complete capability available.
        naming_offer = False
        try:
            from src.openclank.managed_protocol import HOST_CALLBACK_METHODS
            from src.agent_settings import agent_settings_schema
            from src.openclank.worker_naming import _settings
            field = next((item for item in agent_settings_schema()['fields']
                          if item.get('path') == 'worker_naming.enabled'), None)
            if ("_openclank/session/v1/worker-naming/request" in HOST_CALLBACK_METHODS
                    and field and field.get('available') is True and identity['actorID'] == 'main'
                    and identity['sessionID'] == resolved['rootSessionID'] and not resolved.get('authority')
                    and root_state is not None and root_state.root_operation_id == identity['rootOperationID']
                    and root_state.owner_account_id == owner_account and not root_state.provider_root_closing
                    and callable(original_credential) and original_credential() is True
                    and entry.get('ownerAdmissionAccount') == owner_account
                    and entry.get('ownerAdmissionBarrier') is admission_barrier
                    and await admission_barrier.is_live(self._owner, entry.get('ownerAdmission'))
                    and type(resolved.get('modelRequestOptions')) is dict):
                enabled, revision = await asyncio.to_thread(_settings, self._owner)
                if enabled:
                    async with self._managed_execution_lock:
                        if (self._managed_actor_turns.get(key) is entry and entry.get('state') == 'active'
                                and self._turns.get(resolved['rootSessionID']) is root_state
                                and not root_state.provider_root_closing and original_credential() is True
                                and resolved.get('holderID') == self._managed_provider_generation):
                            entry['workerNamingEnabled'] = True
                            entry['workerNamingPreferenceRevision'] = revision
                            naming_offer = True
        except Exception:
            naming_offer = False
        async with self._managed_execution_lock:
            native_policy = entry.get('nativeToolPolicy') if self._managed_actor_turns.get(key) is entry and entry.get('state') == 'active' else None
            witness = entry.get('nativePolicyFilesWitness')
            try:
                from core.models import get_session_manager_instance
                manager = get_session_manager_instance()
                lifecycle = getattr(manager, 'lifecycle_context', None) if manager else None
                auth = getattr(lifecycle, 'auth_manager', None)
                owner, account = entry.get('owner'), entry.get('ownerAccountID')
                if (not isinstance(witness, tuple) or len(witness) != 3 or not auth or not owner or not account
                        or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                        or getattr(lifecycle, 'files_policy_repository', None) is not witness[0]
                        or witness[0].generation() != witness[1] or entry.get('holderID') != self._managed_provider_generation
                        or getattr(entry.get('ownerAdmission'), 'released', True)
                        or entry.get('policyRevision') != witness[2] or entry.get('ownerAdmissionStopped')
                        or getattr(self, '_ownerAdmissionHolderStopped', False)
                        or not callable(entry.get('originalCredentialCurrent')) or not entry['originalCredentialCurrent']()):
                    native_policy = None
            except Exception:
                native_policy = None
            native_policy = dict(native_policy) if isinstance(native_policy, dict) else None
        result = validate_managed_method_result(method, {"accepted": True, "replayed": False, "originalIngressRequired": bool(entry.get("originalIngressRequired")), **identity,
            **({'nativeToolPolicy':native_policy} if native_policy is not None else {}),
            **({'workerNamingEnabled':True} if naming_offer else {}),
            **({'forwardPairV1':True} if entry.get('forwardPairV1') else {}),
            **({'browserSnapshotRepeatAdmission':browser_offer} if browser_offer is not None else {}),
            **({'sessionEnvironment':offer} if offer is not None else {}),
            **({'mediaSourcesV1':media_offer} if media_offer is not None else {}),
            **({'mediaDerivativesV1':media_derivatives} if media_derivatives is not None else {}),
            **({'factoryPersona':factory_persona} if factory_persona is not None else {}),
            **({'displayOwnerProof': {'version': 1, 'accountID': current_display_owner}} if current_display_owner else {}),
            **({'themeStatusOnly':True} if entry.get('themeStatusOnly') else {}),
            **({'codeActivityEnabled':True} if entry.get('codeActivityEnabled') else {})})
        try:
            reply_stage = ('missing_owner' if not offer_at_owner else 'dropped_entry' if offer is None
                else 'validated_present' if result.get('sessionEnvironment') is not None else 'validator_omitted')
            logger.debug('session_environment_reply outcome=%s', reply_stage)
        except Exception: pass
        return result

    @staticmethod
    async def _drain_actor_logging(callback):
        task = asyncio.create_task(asyncio.to_thread(callback))
        cancelled = False
        deadline = asyncio.get_running_loop().time() + 1.0
        def unknown():
            def consume_late(worker):
                if not worker.cancelled():
                    worker.exception()
            task.add_done_callback(consume_late)
            raise _ActorLoggingOutcomeUnknown('actor logging worker outcome unavailable')
        while True:
            try:
                remaining = max(0.0,deadline - asyncio.get_running_loop().time())
                done, _ = await asyncio.wait({task},timeout=remaining)
            except asyncio.CancelledError:
                cancelled = True
                if asyncio.get_running_loop().time() >= deadline:
                    unknown()
                continue
            if task in done:
                if task.cancelled():
                    raise _ActorLoggingOutcomeUnknown('actor logging worker outcome unavailable')
                try:
                    result = task.result()
                except Exception:
                    if cancelled:
                        raise asyncio.CancelledError
                    raise
                if cancelled:
                    raise asyncio.CancelledError
                return result
            if asyncio.get_running_loop().time() >= deadline:
                unknown()

    def _close_provider_root_admission(self, session_id: str, *, expected=None):
        """Fence an exact root before remote cancellation or async cleanup."""
        root = self._turns.get(session_id)
        if root is None or (expected is not None and root is not expected):
            return None
        if not root.provider_root_closing:
            root.provider_root_closing = True
            from src.openclank.browser_control_service import configured_browser_control_service

            browser_service = configured_browser_control_service()
            if browser_service is not None:
                context = self._session_context.get(session_id) or {}
                browser_service.retire_managed_root(
                    owner=self._owner, host_session_id=context.get("odysseus_session_id"),
                    root_operation_id=root.root_operation_id,
                )
        return root

    async def _retire_provider_root(self, session_id: str, *, expected=None):
        """Close the exact root and join its pre-C2 owned writer before removal."""
        root = self._close_provider_root_admission(session_id, expected=expected)
        if root is None:
            return

        async def retire():
            async with self._managed_execution_lock:
                if self._turns.get(session_id) is root:
                    self._turns.pop(session_id, None)
        work = asyncio.create_task(retire())
        cancelled = False
        while not work.done():
            try: await asyncio.shield(work)
            except asyncio.CancelledError: cancelled = True
        work.result()
        if cancelled: raise asyncio.CancelledError

    async def _install_provider_root(self, session_id: str, root):
        previous = self._turns.get(session_id)
        if previous is not None:
            await self._retire_provider_root(session_id, expected=previous)
        async with self._managed_execution_lock:
            if not self._provider_root_sync_guard.acquire(blocking=False):
                raise ValueError('synchronous provider root retirement is active')
            try:
                if self._turns.get(session_id) is not None:
                    raise ValueError('managed provider root changed during replacement')
                if root.foreground_attachment is not None:
                    root.provider_root_callback = getattr(self._managed_provider_context, '__self__', None)
                    root.provider_root_holder = self._managed_provider_generation
                self._turns[session_id] = root
            finally:
                self._provider_root_sync_guard.release()

    def _refuse_sync_provider_root_forget(self, session_id: str):
        root = self._turns.get(session_id)
        if root is not None and root.foreground_attachment is not None:
            raise RuntimeError('active attached provider root requires async retirement')

    def _update_provider_root_context(self, session_id: str, values: dict, *, replace=False):
        if not self._provider_root_sync_guard.acquire(blocking=False):
            raise RuntimeError('provider root lifecycle is busy')
        try:
            root = self._turns.get(session_id)
            if root is not None and root.foreground_attachment is not None:
                raise RuntimeError('provider root is active during context replacement')
            if replace:
                self._session_context[session_id] = values
            else:
                self._session_context.setdefault(session_id, {}).update(values)
        finally:
            self._provider_root_sync_guard.release()

    async def admit_managed_provider_root_transaction(self, session_id: str, root_id: str, transaction):
        """Hold the original host root/attachment while one synchronous worker settles."""
        from core.models import get_session_manager_instance
        if (type(session_id) is not str or not session_id or type(root_id) is not str or not root_id
                or not callable(transaction) or inspect.iscoroutinefunction(transaction)):
            raise ValueError('managed provider root identity unavailable')
        manager = get_session_manager_instance()
        lifecycle = getattr(manager, 'lifecycle_context', None) if manager else None
        auth = getattr(lifecycle, 'auth_manager', None)
        barrier = getattr(lifecycle, 'account_request_barrier', None)
        supervisor = getattr(lifecycle, 'mimo_supervisor', None)
        callbacks = getattr(self._managed_provider_context, '__self__', None)
        if auth is None or barrier is None or supervisor is None or callbacks is None:
            raise ValueError('managed provider root authority unavailable')
        async with self._managed_execution_lock:
            root = self._turns.get(session_id)
            attachment = root.foreground_attachment if root is not None else None
            if (root is None or root.provider_root_closing or root.root_operation_id != root_id
                    or not isinstance(attachment, dict)):
                raise ValueError('managed provider root unavailable')
            owner, account = self._owner, attachment.get('account')
            root_binding = attachment.get('root_binding')
            chat = root_binding[1] if isinstance(root_binding, tuple) and len(root_binding) == 4 else None
            holder = self._managed_provider_generation
            nonce = root.provider_root_nonce
            lease, worker, process = attachment.get('lease'), attachment.get('worker'), attachment.get('process')
            pool = supervisor.owner_lifecycle_pool()
            epoch, generation = attachment.get('owner_epoch'), attachment.get('generation')
            owner_state = pool._states.get(pool._key(owner))
            session_context = self._session_context.get(session_id)
            if (not isinstance(chat, str) or not chat or not isinstance(account, str) or not account
                    or root.owner_account_id != account or attachment.get('barrier') is not barrier
                    or attachment.get('private_authority') is not None
                    or not await barrier.is_live(owner, attachment.get('admission'))
                    or getattr(lease, '_pool', None) is not pool or pool.worker_for_owner(owner) is not worker
                    or owner_state is None or owner_state.active is not worker or owner_state.generation != generation
                    or owner_state.status != 'ready' or worker is None or process is None or worker._proc is not process
                    or worker._bridge is not self or not worker.is_alive() or worker.installed_generation != generation
                    or type(generation) is not int or generation < 0 or type(epoch) is not int or epoch < 0
                    or not isinstance(holder, str) or not holder or root.provider_root_holder != holder
                    or (nonce is not None and (not isinstance(nonce, str) or len(nonce) != 64))
                    or root.provider_root_callback is not callbacks or getattr(callbacks, 'holder_id', None) != holder
                    or not isinstance(session_context, dict) or session_context.get('root_operation_id') != root_id
                    or session_context.get('odysseus_session_id') != chat or session_context.get('owner') != owner
                    or not self._direct_foreground_current(attachment, owner=owner, host_session=chat,
                        native_session=session_id, root=root_id, holder=holder)):
                raise ValueError('managed provider root authority unavailable')
            try:
                options = json.dumps(root.main_option_candidate, sort_keys=True, separators=(',', ':'), allow_nan=False)
                if len(options.encode('utf-8')) > 16384: raise ValueError('options too large')
            except (TypeError, ValueError, UnicodeError, RecursionError):
                raise ValueError('managed provider root options unavailable') from None
            live = [True]
            def current():
                try:
                    pool._assert_owner_lifecycle(pool._key(owner), epoch)
                    return (live[0] and self._turns.get(session_id) is root and not root.provider_root_closing
                        and root.root_operation_id == root_id and root.foreground_attachment is attachment
                        and root.provider_root_callback is callbacks and root.provider_root_holder == holder
                        and root.provider_root_nonce == nonce
                        and root.owner_account_id == account and auth.account_id(owner) == account
                        and self._session_context.get(session_id) is session_context
                        and session_context.get('root_operation_id') == root_id
                        and session_context.get('odysseus_session_id') == chat and session_context.get('owner') == owner
                        and auth.username_for_account_id(account) == owner and attachment.get('barrier') is barrier
                        and not attachment['admission'].released and pool.worker_for_owner(owner) is worker
                        and pool._states.get(pool._key(owner)) is owner_state and owner_state.active is worker
                        and owner_state.generation == generation and owner_state.status == 'ready'
                        and worker._proc is process and worker._bridge is self and worker.is_alive()
                        and worker.installed_generation == generation and self._managed_provider_generation == holder
                        and getattr(self._managed_provider_context, '__self__', None) is callbacks
                        and getattr(callbacks, 'holder_id', None) == holder
                        and json.dumps(root.main_option_candidate, sort_keys=True, separators=(',', ':'), allow_nan=False) == options
                        and self._direct_foreground_current(attachment, owner=owner, host_session=chat,
                            native_session=session_id, root=root_id, holder=holder))
                except Exception:
                    return False
            snapshot = _ManagedProviderRootTransactionAdmission(owner, account, chat, session_id, root_id,
                holder, generation, epoch, options, root, callbacks, current, _PROVIDER_TRANSACTION_SEAL)
            def run():
                snapshot.assert_current()
                result = transaction(snapshot)
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result): result.close()
                    raise ValueError('managed provider root transaction must be synchronous')
                return result
            work = asyncio.create_task(asyncio.to_thread(run))
            cancelled = False
            while not work.done():
                try: await asyncio.shield(work)
                except asyncio.CancelledError: cancelled = True
                except Exception: break
            live[0] = False
            try: result = work.result()
            except BaseException as error:
                if cancelled: raise _ProviderTransactionCancelled(returned=False, error=error) from error
                raise
            if cancelled: raise _ProviderTransactionCancelled(returned=True, result=result)
            return result

    async def admit_managed_provider_transaction(self, identity: dict, transaction):
        """Run one host-private synchronous transaction under the exact MAIN turn.

        The callback must not await or perform native/network I/O. It receives
        a sealed snapshot and must recheck it under its own transaction locks.
        """
        from core.models import get_session_manager_instance
        fields = ('sessionID', 'rootOperationID', 'actorID', 'executionRevision', 'innerTurnID')
        if (type(identity) is not dict or set(identity) != set(fields) or identity.get('actorID') != 'main'
                or any(type(identity.get(k)) is not str or not identity[k] or len(identity[k]) > 256 for k in ('sessionID','rootOperationID','innerTurnID'))
                or type(identity.get('executionRevision')) is not int or identity['executionRevision'] < 0
                or not callable(transaction) or inspect.iscoroutinefunction(transaction)):
            raise ValueError('managed provider transaction identity unavailable')
        identity = dict(identity)
        key = self._managed_turn_key(identity)
        manager = get_session_manager_instance()
        state = getattr(manager, 'lifecycle_context', None) if manager else None
        supervisor = getattr(state, 'mimo_supervisor', None)
        auth = getattr(state, 'auth_manager', None)
        barrier = getattr(state, 'account_request_barrier', None)
        callbacks = getattr(self._managed_provider_context, '__self__', None)
        if supervisor is None or auth is None or barrier is None or callbacks is None:
            raise ValueError('managed provider transaction authority unavailable')
        async with self._managed_execution_lock:
            entry = self._managed_actor_turns.get(key)
            execution = self._managed_actor_executions.get(key[:4])
            pool = supervisor.owner_lifecycle_pool()
            worker = pool.worker_for_owner(self._owner) if pool is not None else None
            owner_state = pool._states.get(pool._key(self._owner)) if pool is not None else None
            holder = self._managed_provider_generation
            admission = entry.get('ownerAdmission') if isinstance(entry, dict) else None
            account = entry.get('ownerAdmissionAccount') if isinstance(entry, dict) else None
            process = getattr(worker, '_proc', None)
            generation = getattr(worker, 'installed_generation', None)
            epoch = pool._owner_lifecycle_epoch(self._owner) if pool is not None else None
            chat = entry.get('stableChatID') if isinstance(entry, dict) else None
            root = getattr(self, '_turns', {}).get(identity['sessionID'])
            attachment = root.foreground_attachment if root is not None else None
            root_nonce = getattr(root, 'provider_root_nonce', None) if root is not None else None
            root_admission = attachment.get('admission') if isinstance(attachment, dict) else None
            root_lease = attachment.get('lease') if isinstance(attachment, dict) else None
            def root_current():
                return bool(root is not None and isinstance(attachment, dict)
                    and self._turns.get(identity['sessionID']) is root and not root.provider_root_closing
                    and root.provider_root_nonce == root_nonce and root.foreground_attachment is attachment
                    and root.root_operation_id == identity['rootOperationID'] and root.owner_account_id == account
                    and root.provider_root_callback is callbacks and root.provider_root_holder == holder
                    and attachment.get('account') == account and attachment.get('barrier') is barrier
                    and attachment.get('worker') is worker and attachment.get('process') is process
                    and attachment.get('lease') is root_lease and attachment.get('admission') is root_admission
                    and attachment.get('generation') == generation and attachment.get('owner_epoch') == epoch
                    and getattr(root_lease, '_pool', None) is pool
                    and attachment.get('private_authority') is None and not getattr(attachment.get('admission'), 'released', True)
                    and self._direct_foreground_current(attachment, owner=self._owner, host_session=chat,
                        native_session=identity['sessionID'], root=identity['rootOperationID'], holder=holder))
            if (root is None or not isinstance(attachment, dict) or not isinstance(root_nonce, str)
                    or len(root_nonce) != 64 or not root_current()
                    or not await barrier.is_live(self._owner, attachment.get('admission'))
                    ):
                root, attachment, root_nonce = None, None, None
            model = entry.get('effectiveModel') if isinstance(entry, dict) else None
            if (type(model) is not dict or type(model.get('providerID')) is not str or not model['providerID']
                    or type(model.get('modelID')) is not str or not model['modelID']):
                raise ValueError('managed provider model unavailable')
            model = (model['providerID'], model['modelID'])
            try:
                option_context = json.dumps({'mainOptionCandidate':entry.get('mainOptionCandidate'),
                    'modelRequestOptions':entry.get('modelRequestOptions')}, sort_keys=True, separators=(',', ':'), allow_nan=False)
                if len(option_context.encode('utf-8')) > 16384: raise ValueError('option context too large')
            except (TypeError, ValueError, UnicodeError, RecursionError):
                raise ValueError('managed provider options unavailable') from None
            if (entry is None or execution is not entry or entry.get('state') != 'active'
                    or entry.get('rootSessionID') != identity['sessionID'] or entry.get('owner') != self._owner
                    or entry.get('ownerAdmissionBarrier') is not barrier or not admission or not account
                    or not isinstance(chat, str) or not chat
                    or entry.get('backgroundStartClosing') or not await barrier.is_live(self._owner, admission)
                    or not isinstance(holder, str) or not holder or entry.get('holderID') != holder
                    or getattr(callbacks, 'holder_id', None) != holder or worker is None or pool is None
                    or owner_state is None or owner_state.active is not worker
                    or owner_state.generation != generation or owner_state.status != 'ready'
                    or process is None or getattr(worker, '_bridge', None) is not self or not worker.is_alive()
                    or type(generation) is not int or generation < 0 or type(epoch) is not int or epoch < 0
                    or getattr(self, '_ownerAdmissionHolderStopped', False)):
                raise ValueError('managed provider transaction authority unavailable')
            live = [True]
            def current():
                try:
                    pool._assert_owner_lifecycle(pool._key(self._owner), epoch)
                    return (live[0] and self._managed_actor_turns.get(key) is entry and self._managed_actor_executions.get(key[:4]) is entry
                        and entry.get('state') == 'active' and not entry.get('backgroundStartClosing') and not admission.released
                        and entry.get('ownerAdmission') is admission and entry.get('ownerAdmissionBarrier') is barrier
                        and entry.get('ownerAdmissionAccount') == account and entry.get('holderID') == holder
                        and entry.get('stableChatID') == chat
                        and type(entry.get('effectiveModel')) is dict
                        and (entry['effectiveModel'].get('providerID'), entry['effectiveModel'].get('modelID')) == model
                        and json.dumps({'mainOptionCandidate':entry.get('mainOptionCandidate'),
                            'modelRequestOptions':entry.get('modelRequestOptions')}, sort_keys=True, separators=(',', ':'), allow_nan=False) == option_context
                        and auth.account_id(self._owner) == account and auth.username_for_account_id(account) == self._owner
                        and pool.worker_for_owner(self._owner) is worker and pool._states.get(pool._key(self._owner)) is owner_state
                        and owner_state.active is worker and owner_state.generation == generation and owner_state.status == 'ready'
                        and worker.is_alive()
                        and worker._bridge is self and worker._proc is process and worker.installed_generation == generation
                        and pool._owner_lifecycle_epoch(self._owner) == epoch and self._managed_provider_generation == holder
                        and getattr(self._managed_provider_context, '__self__', None) is callbacks
                        and getattr(callbacks, 'holder_id', None) == holder and not getattr(self, '_ownerAdmissionHolderStopped', False)
                        and (root is None or root_current()))
                except Exception:
                    return False
            snapshot = _ManagedProviderTransactionAdmission(self._owner, account, chat,
                identity['sessionID'], identity['rootOperationID'], identity['actorID'], identity['innerTurnID'],
                identity['executionRevision'], holder, generation, epoch, model[0], model[1], option_context,
                callbacks, current, _PROVIDER_TRANSACTION_SEAL, root, attachment)
            def run():
                snapshot.assert_current()
                result = transaction(snapshot)
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result): result.close()
                    raise ValueError('managed provider transaction must be synchronous')
                return result
            work = asyncio.create_task(asyncio.to_thread(run))
            cancelled = False
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
            live[0] = False
            try:
                result = work.result()
            except BaseException as error:
                if cancelled:
                    raise _ProviderTransactionCancelled(returned=False, error=error) from error
                raise
            if cancelled:
                raise _ProviderTransactionCancelled(returned=True, result=result)
            return result

    async def admit_managed_actor_logging(self, identity: dict, admit):
        """Serialize a host-validated capture admission with exact execution finish."""
        key = (identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
        async with self._managed_execution_lock:
            if key in self._managed_finished_executions:
                raise ValueError('managed actor execution already finished')
            if key in self._managed_logging_uncertain or len(self._managed_logging_uncertain) >= 4096:
                raise ValueError('managed actor logging outcome unavailable')
            entry = self._managed_actor_executions.get(key)
            account = entry.get("ownerAccountID") if isinstance(entry, dict) else None
            snapshot = None
            if (entry and entry.get("state") == "active" and entry.get("holderID") == self._managed_provider_generation
                    and isinstance(account, str) and account and entry.get("stableChatID")):
                try:
                    from core.models import get_session_manager_instance
                    manager = get_session_manager_instance()
                    auth = getattr(getattr(manager, "lifecycle_context", None), "auth_manager", None) if manager else None
                    if (auth is not None and auth.account_id(self._owner) == account
                            and auth.username_for_account_id(account) == self._owner
                            and self._managed_actor_executions.get(key) is entry):
                        snapshot = {"account": account, "chat": entry["stableChatID"],
                                    "holder": entry["holderID"], "identity": dict(identity)}
                except Exception:
                    snapshot = None
            try:
                return await self._drain_actor_logging(lambda: admit(snapshot))
            except _ActorLoggingOutcomeUnknown:
                self._managed_logging_uncertain.add(key)
                callbacks = getattr(self,'_logging_callbacks',None)
                if callbacks is not None:
                    try:
                        callbacks.fence_actor_logging(identity)
                    except Exception:
                        logger.warning('managed actor capture fence unavailable')
                raise

    async def _recover_managed_execution_finish(self, identity: dict, recovery: dict) -> dict:
        """Acknowledge only a native pending settlement backed by the signed host turn receipt."""
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import validate_managed_method_result
        root = recovery['rootSessionID']; context = self._session_context.get(root) or {}
        chat = context.get('odysseus_session_id')
        if not isinstance(chat, str) or not chat or context.get('private_authority'):
            raise ValueError('managed execution recovery root unavailable')
        binding = await self._handle_session_binding_read({'sessionID':root})
        if binding.get('owner') != self._owner or binding.get('stableChatID') != chat:
            raise ValueError('managed execution recovery binding changed')
        manager = get_session_manager_instance(); state = getattr(manager, 'lifecycle_context', None) if manager is not None else None
        auth = getattr(state, 'auth_manager', None); barrier = getattr(state, 'account_request_barrier', None)
        worker = getattr(self._managed_actor_execution_reader, '__self__', None)
        provider_context = getattr(self._managed_provider_context, '__self__', None)
        if (auth is None or barrier is None or worker is None or getattr(worker, '_bridge', None) is not self
                or not self._managed_provider_generation or getattr(provider_context, 'holder_id', None) != self._managed_provider_generation):
            raise ValueError('managed execution recovery authority unavailable')
        account = auth.account_id(self._owner)
        if not account or auth.username_for_account_id(account) != self._owner:
            raise ValueError('managed execution recovery account changed')
        turn = {**identity, 'innerTurnID':recovery['innerTurnID']}
        native_request = {'directory':binding['canonicalCwd'], 'rootSessionID':root, 'sessionID':identity['sessionID'],
            'actorID':identity['actorID'], 'executionRevision':identity['executionRevision']}
        expected = {'state':'pending', 'rootSessionID':root, 'rootOperationID':identity['rootOperationID'],
            'innerTurnID':recovery['innerTurnID'], 'outcome':recovery['turnOutcome'], 'executionOutcome':recovery['executionOutcome']}
        async def marker():
            value = await worker.managed_engine_call('_openclank/session/v1/actor/settlement-read', native_request)
            if (type(value) is not dict or set(value) != {'sessionID','actorID','executionRevision','unresolved'}
                    or any(value.get(k) != identity[k] for k in ('sessionID','actorID','executionRevision'))
                    or value.get('unresolved') != expected):
                raise ValueError('managed execution recovery marker changed')
            return value
        async with barrier.admitted(self._owner):
            await marker()
            from src.openclank.transcript_projection import read_managed_turn_receipt, record_managed_execution_completion
            receipt = await asyncio.to_thread(read_managed_turn_receipt, chat, self._owner, turn)
            if (not receipt or receipt.get('accountID') != account or receipt.get('rootSessionID') != root
                    or receipt.get('outcome') != recovery['turnOutcome'] or not receipt.get('holderID')):
                raise ValueError('managed execution recovery receipt unavailable')
            if await marker() is None: raise ValueError('managed execution recovery marker unavailable')
            task = asyncio.create_task(asyncio.to_thread(record_managed_execution_completion, chat, self._owner,
                account, turn, recovery['executionOutcome']))
            cancelled = False
            while True:
                try:
                    await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    if task.cancelled(): raise ValueError('managed execution completion outcome unavailable')
                    cancelled = True
            await marker()
            if (await self._handle_session_binding_read({'sessionID':root}) != binding
                    or auth.account_id(self._owner) != account or auth.username_for_account_id(account) != self._owner):
                raise ValueError('managed execution recovery authority changed')
            key = (identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
            async with self._managed_execution_lock:
                if (key in self._managed_actor_executions or any(turn_key[:4] == key for turn_key in self._managed_actor_turns)
                        or getattr(provider_context, 'holder_id', None) != self._managed_provider_generation):
                    raise ValueError('managed execution recovery changed')
            if cancelled: raise asyncio.CancelledError
        return validate_managed_method_result('_openclank/session/v1/execution/finish',
            {'accepted':True, 'replayed':bool(receipt.get('executionFinish')), 'captureState':'unavailable'})

    async def _handle_managed_execution_finish(self, params: dict) -> dict:
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = "_openclank/session/v1/execution/finish"
        identity_keys = {'sessionID','rootOperationID','actorID','executionRevision'}
        if type(params) is not dict or set(params) - (identity_keys | {'evidence','recovery','executionOutcome'}):
            raise ValueError('managed execution finish identity unavailable')
        identity = validate_managed_method_request(method,{key:params[key] for key in identity_keys if key in params})
        execution_outcome = params.get('executionOutcome')
        if execution_outcome is not None:
            validate_managed_method_request(method, {**identity, 'executionOutcome':execution_outcome})
        evidence = None
        capture_state = 'absent'
        if 'evidence' in params:
            capture_state = 'unavailable'
            try:
                evidence = validate_managed_method_request(method,params)['evidence']
            except Exception:
                capture_state = 'unavailable'
        key = (identity["sessionID"], identity["rootOperationID"], identity["actorID"], identity["executionRevision"])
        async with self._managed_execution_lock:
            absent = key not in self._managed_actor_executions and key not in self._managed_finished_executions
        if absent:
            if 'recovery' not in params:
                raise ValueError('managed execution finish identity unavailable')
            recovery = validate_managed_method_request(method, {**identity, 'recovery':params['recovery']})['recovery']
            return await self._recover_managed_execution_finish(identity, recovery)
        logging_cancelled = False
        checked_entry = self._managed_actor_executions.get(key)
        retained_barrier = checked_entry.get('ownerAdmissionBarrier') if checked_entry else None
        retained_admission = checked_entry.get('ownerAdmission') if checked_entry else None
        if retained_admission is not None and (retained_barrier is None
                or not await retained_barrier.is_live(self._owner, retained_admission)):
            raise ValueError('managed execution owner admission unavailable')
        async with self._managed_execution_lock:
            if key in self._managed_finished_executions:
                return validate_managed_method_result(method, {"accepted": True, "replayed": True, 'captureState':'unavailable'})
            if key not in self._managed_actor_executions:
                raise ValueError("managed execution finish does not match an admitted execution")
            if any(turn[:4] == key for turn in self._managed_actor_turns):
                raise ValueError("managed execution still has an active inner turn")
            entry = self._managed_actor_executions[key]
            if retained_admission is not None and entry is not checked_entry:
                raise ValueError('managed execution owner admission changed')
            last_turn = entry.get('lastFinishedTurn')
            if retained_admission is not None and (execution_outcome is None or type(last_turn) is not dict
                    or not entry.get('ownerAccountID')):
                raise ValueError('managed execution terminal proof unavailable')
            if retained_admission is not None and entry.get('authority'):
                # Private turns have no durable transcript receipt. Their exact
                # in-memory finish and Native finalizing owner must agree while
                # the same ghost, account, holder and admission remain live.
                manager = get_session_manager_instance()
                state = getattr(manager, 'lifecycle_context', None) if manager is not None else None
                auth = getattr(state, 'auth_manager', None)
                barrier = getattr(state, 'account_request_barrier', None)
                worker = getattr(self._managed_actor_execution_reader, '__self__', None)
                provider_context = getattr(self._managed_provider_context, '__self__', None)
                holder = self._managed_provider_generation
                root = entry['rootSessionID']
                account = entry['ownerAccountID']
                turn_outcome = entry.get('privateFinishedTurnOutcome')
                process = getattr(worker, '_proc', None)
                context = self._session_context.get(root)
                if (auth is None or barrier is not retained_barrier or worker is None or process is None
                        or not getattr(worker, '_private_runtime', False) or getattr(worker, '_bridge', None) is not self
                        or not worker.is_alive() or not holder or entry.get('holderID') != holder
                        or getattr(provider_context, 'holder_id', None) != holder
                        or getattr(self, '_ownerAdmissionHolderStopped', False) or entry.get('ownerAdmissionStopped')
                        or entry.get('ownerAdmissionAccount') != account or retained_admission.released
                        or not isinstance(context, dict) or context.get('root_operation_id') != identity['rootOperationID']
                        or auth.account_id(entry['owner']) != account or auth.username_for_account_id(account) != entry['owner']
                        or turn_outcome not in ('completed', 'cancelled', 'error')
                        or last_turn.get('sessionID') != identity['sessionID']
                        or last_turn.get('rootOperationID') != identity['rootOperationID']
                        or last_turn.get('actorID') != identity['actorID']
                        or last_turn.get('executionRevision') != identity['executionRevision']):
                    raise ValueError('private managed execution finalizing authority unavailable')
                binding = await self._handle_session_binding_read({'sessionID':root})
                if (binding.get('owner') != entry['owner'] or binding.get('stableChatID') != entry['stableChatID']
                        or binding.get('authority') != entry['authority'] or binding.get('engineSessionID') != root):
                    raise ValueError('private managed execution binding unavailable')
                native_request = {'directory':binding['canonicalCwd'], 'rootSessionID':root, 'sessionID':identity['sessionID'],
                    'actorID':identity['actorID'], 'executionRevision':identity['executionRevision'], 'profile':'finalizing',
                    'rootOperationID':identity['rootOperationID'], 'innerTurnID':last_turn['innerTurnID'],
                    'turnOutcome':turn_outcome, 'executionOutcome':execution_outcome}
                expected = {'state':'pending', 'rootSessionID':root, 'rootOperationID':identity['rootOperationID'],
                    'innerTurnID':last_turn['innerTurnID'], 'outcome':turn_outcome, 'executionOutcome':execution_outcome}
                async def private_finalizing_marker():
                    value = await worker.managed_engine_call('_openclank/session/v1/actor/settlement-read', native_request)
                    if (type(value) is not dict or set(value) != {'sessionID','actorID','executionRevision','unresolved'}
                            or any(value.get(k) != identity[k] for k in ('sessionID','actorID','executionRevision'))
                            or value.get('unresolved') != expected):
                        raise ValueError('private managed execution finalizing marker unavailable')
                await private_finalizing_marker()
                if (await self._handle_session_binding_read({'sessionID':root}) != binding
                        or self._session_context.get(root) is not context or getattr(worker, '_proc', None) is not process
                        or not worker.is_alive() or self._managed_provider_generation != holder
                        or getattr(provider_context, 'holder_id', None) != holder
                        or getattr(self, '_ownerAdmissionHolderStopped', False) or entry.get('ownerAdmissionStopped')
                        or not await barrier.is_live(self._owner, retained_admission)
                        or auth.account_id(entry['owner']) != account or auth.username_for_account_id(account) != entry['owner']):
                    raise ValueError('private managed execution finalizing authority changed')
                await private_finalizing_marker()
                if (await self._handle_session_binding_read({'sessionID':root}) != binding
                        or self._session_context.get(root) is not context or getattr(worker, '_bridge', None) is not self
                        or getattr(worker, '_proc', None) is not process or not worker.is_alive()
                        or self._managed_provider_generation != holder or getattr(provider_context, 'holder_id', None) != holder
                        or getattr(self, '_ownerAdmissionHolderStopped', False) or entry.get('ownerAdmissionStopped')
                        or not await barrier.is_live(self._owner, retained_admission)
                        or auth.account_id(entry['owner']) != account or auth.username_for_account_id(account) != entry['owner']):
                    raise ValueError('private managed execution finalizing authority changed')
            if execution_outcome is not None and type(last_turn) is dict and not entry.get('authority') and entry.get('ownerAccountID'):
                manager = get_session_manager_instance()
                state = getattr(manager, 'lifecycle_context', None) if manager is not None else None
                auth = getattr(state, 'auth_manager', None); barrier = getattr(state, 'account_request_barrier', None)
                if auth is None or barrier is None: raise ValueError('managed execution receipt authority unavailable')
                from contextlib import nullcontext
                if retained_admission is not None and (barrier is not retained_barrier
                        or entry.get('ownerAdmissionAccount') != entry['ownerAccountID']):
                    raise ValueError('managed execution owner admission changed')
                async with (nullcontext() if retained_admission is not None else barrier.admitted(entry['owner'])):
                    account = entry['ownerAccountID']
                    if auth.account_id(entry['owner']) != account or auth.username_for_account_id(account) != entry['owner']:
                        raise ValueError('managed execution receipt account changed')
                    # Public actor status intentionally remains running until the outer
                    # fiber exits.  The private finalizing profile instead proves the
                    # settled row and that same current owner without claiming quiescence.
                    worker = getattr(self._managed_actor_execution_reader, '__self__', None)
                    provider_context = getattr(self._managed_provider_context, '__self__', None)
                    holder = self._managed_provider_generation
                    root = entry['rootSessionID']
                    if (worker is None or getattr(worker, '_bridge', None) is not self or not holder
                            or entry.get('holderID') != holder or getattr(provider_context, 'holder_id', None) != holder
                            or getattr(self, '_ownerAdmissionHolderStopped', False) or entry.get('ownerAdmissionStopped')
                            or (retained_admission is not None and retained_admission.released)):
                        raise ValueError('managed execution finalizing authority unavailable')
                    binding = await self._handle_session_binding_read({'sessionID':root})
                    if binding.get('owner') != self._owner or binding.get('stableChatID') != entry['stableChatID']:
                        raise ValueError('managed execution finalizing binding unavailable')
                    from src.openclank.transcript_projection import read_managed_turn_receipt, record_managed_execution_completion
                    receipt = await asyncio.to_thread(read_managed_turn_receipt, entry['stableChatID'], entry['owner'], last_turn)
                    if (type(receipt) is not dict or receipt.get('accountID') != account or receipt.get('rootSessionID') != root
                            or receipt.get('holderID') != holder or receipt.get('executionFinish') is not None
                            or receipt.get('outcome') not in ('completed','cancelled','error')):
                        raise ValueError('managed execution finished-turn receipt unavailable')
                    native_request = {'directory':binding['canonicalCwd'], 'rootSessionID':root, 'sessionID':identity['sessionID'],
                        'actorID':identity['actorID'], 'executionRevision':identity['executionRevision'], 'profile':'finalizing',
                        'rootOperationID':identity['rootOperationID'], 'innerTurnID':last_turn['innerTurnID'],
                        'turnOutcome':receipt['outcome'], 'executionOutcome':execution_outcome}
                    expected = {'state':'pending', 'rootSessionID':root, 'rootOperationID':identity['rootOperationID'],
                        'innerTurnID':last_turn['innerTurnID'], 'outcome':receipt['outcome'], 'executionOutcome':execution_outcome}
                    async def finalizing_marker():
                        value = await worker.managed_engine_call('_openclank/session/v1/actor/settlement-read', native_request)
                        if (type(value) is not dict or set(value) != {'sessionID','actorID','executionRevision','unresolved'}
                                or any(value.get(k) != identity[k] for k in ('sessionID','actorID','executionRevision'))
                                or value.get('unresolved') != expected):
                            raise ValueError('managed execution finalizing marker unavailable')
                    await finalizing_marker()
                    if (await asyncio.to_thread(read_managed_turn_receipt, entry['stableChatID'], entry['owner'], last_turn) != receipt
                            or await self._handle_session_binding_read({'sessionID':root}) != binding
                            or getattr(worker, '_bridge', None) is not self or self._managed_provider_generation != holder
                            or getattr(provider_context, 'holder_id', None) != holder or getattr(self, '_ownerAdmissionHolderStopped', False)
                            or entry.get('ownerAdmissionStopped') or (retained_admission is not None and not await barrier.is_live(self._owner, retained_admission))
                            or auth.account_id(entry['owner']) != account or auth.username_for_account_id(account) != entry['owner']):
                        raise ValueError('managed execution finalizing authority changed')
                    await finalizing_marker()
                    task = asyncio.create_task(asyncio.to_thread(record_managed_execution_completion,
                        entry['stableChatID'], entry['owner'], account, last_turn, execution_outcome))
                    while True:
                        try:
                            await asyncio.shield(task)
                            break
                        except asyncio.CancelledError:
                            if task.cancelled(): raise ValueError('managed execution receipt outcome unavailable')
                            logging_cancelled = True
                    if auth.account_id(entry['owner']) != account or auth.username_for_account_id(account) != entry['owner']:
                        raise ValueError('managed execution receipt account changed')
            logging_callbacks = getattr(self,'_logging_callbacks',None)
            if logging_callbacks is not None:
                if key in self._managed_logging_uncertain:
                    capture_state = 'unavailable'
                    try:
                        logging_callbacks.fence_actor_logging(identity)
                    except Exception:
                        logger.warning('managed actor capture fence unavailable')
                else:
                    try:
                        observed = await self._drain_actor_logging(lambda:logging_callbacks.finish_actor_logging(identity,evidence))
                        if evidence is not None:
                            capture_state = observed
                    except _ActorLoggingOutcomeUnknown:
                        capture_state = 'unavailable'
                        self._managed_logging_uncertain.add(key)
                        try:
                            logging_callbacks.fence_actor_logging(identity)
                        except Exception:
                            logger.warning('managed actor capture fence unavailable')
                    except asyncio.CancelledError:
                        # The worker was drained; retire the actor before
                        # propagating the caller's cancellation.
                        logging_cancelled = True
                        capture_state = 'unavailable'
                    except Exception:
                        capture_state = 'unavailable'
                        self._managed_logging_uncertain.add(key)
                        try:
                            logging_callbacks.fence_actor_logging(identity)
                        except Exception:
                            logger.warning('managed actor capture fence unavailable')
                        logger.warning('managed actor logging settlement unavailable')
            runtime = entry.get('foregroundRuntime')
            if runtime is not None:
                runtime.leave(entry['foregroundHandle'])
            self._managed_actor_executions.pop(key)
            if not entry.get('authority'):
                from src.openclank.browser_control_service import configured_browser_control_service

                browser_service = configured_browser_control_service()
                if browser_service is not None:
                    browser_service.retire_managed_execution(
                        owner=entry.get('owner'), account_id=entry.get('ownerAccountID'),
                        host_session_id=entry.get('stableChatID'), execution_key=key,
                    )
            direct_handoff=entry.get('directMainPolicyHandoff')
            if direct_handoff is not None:
                reservation=self._prepared_managed_handoffs.get(direct_handoff)
                if reservation is not None and reservation.get('directMainPolicyIntent') is not None:
                    self._prepared_managed_handoffs.pop(direct_handoff,None)
            self._managed_finished_executions.add(key)
            if len(self._managed_finished_executions) > 4096:
                self._managed_finished_executions.pop()
        if retained_admission is not None:
            await self._release_owner_admission(retained_barrier, retained_admission)
        if runtime is None and not entry.get("authority"):
            from src.openclank.memory_job_admission import observe_shadow
            shadow_id = hashlib.sha256(json.dumps([self._owner, entry["holderID"], *key], separators=(",", ":")).encode()).hexdigest()
            observe_shadow(self._memory_provider, operation_id="actor:" + shadow_id, owner=self._owner,
                           connection_id="unknown", generation=entry["holderID"], event="finished", account_id=entry.get("ownerAdmissionAccount"))
        if logging_cancelled:
            raise asyncio.CancelledError
        return validate_managed_method_result(method, {"accepted": True, "replayed": False, 'captureState':capture_state})

    async def _handle_managed_turn_status(self, params: dict) -> dict:
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = "_openclank/session/v1/turn/status"
        request = validate_managed_method_request(method, params)
        fields = ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')
        identity = {field:request[field] for field in fields}
        key = self._managed_turn_key(identity)
        async with self._managed_execution_lock:
            if key in self._managed_actor_turns:
                result = {**identity, "state": "active"}
            elif key in self._managed_finished_turns:
                result = {**identity, "state": "finished", "outcome": self._managed_finished_turns[key]}
            else:
                result = {**identity, "state": "unknown", "reason": "identity_not_retained"}
        if result['state'] == 'unknown' and request.get('rootSessionID'):
            # The hint only selects a currently mapped root. Native independently
            # proves the exact unresolved marker on both sides of the host read.
            try:
                root = request['rootSessionID']
                context = self._session_context.get(root) or {}
                chat_id = context.get('odysseus_session_id')
                if not isinstance(chat_id, str) or not chat_id or context.get('private_authority'):
                    raise ValueError('recovery root unavailable')
                binding = await self._handle_session_binding_read({'sessionID':root})
                if binding.get('owner') != self._owner or binding.get('stableChatID') != chat_id:
                    raise ValueError('recovery binding changed')
                manager = get_session_manager_instance()
                state = getattr(manager, 'lifecycle_context', None) if manager is not None else None
                auth = getattr(state, 'auth_manager', None); barrier = getattr(state, 'account_request_barrier', None)
                worker = getattr(self._managed_actor_execution_reader, '__self__', None)
                provider_context = getattr(self._managed_provider_context, '__self__', None)
                if (auth is None or barrier is None or worker is None or getattr(worker, '_bridge', None) is not self
                        or not self._managed_provider_generation or getattr(provider_context, 'holder_id', None) != self._managed_provider_generation):
                    raise ValueError('recovery authority unavailable')
                account = auth.account_id(self._owner)
                if not account or auth.username_for_account_id(account) != self._owner:
                    raise ValueError('recovery account changed')
                native_request = {'directory':binding['canonicalCwd'], 'rootSessionID':root,
                    'sessionID':identity['sessionID'], 'actorID':identity['actorID'],
                    'executionRevision':identity['executionRevision']}
                async def read_marker():
                    value = await worker.managed_engine_call('_openclank/session/v1/actor/settlement-read', native_request)
                    if (type(value) is not dict or set(value) != {'sessionID','actorID','executionRevision','unresolved'}
                            or any(value.get(k) != identity[k] for k in ('sessionID','actorID','executionRevision'))):
                        raise ValueError('recovery marker unavailable')
                    marker = value['unresolved']
                    if (type(marker) is not dict or set(marker) != {'state','rootSessionID','rootOperationID','innerTurnID','outcome'}
                            or marker['state'] != 'uncertain' or marker['rootSessionID'] != root or marker['rootOperationID'] != identity['rootOperationID']
                            or marker['innerTurnID'] != identity['innerTurnID'] or marker['outcome'] not in ('completed','cancelled','error')):
                        raise ValueError('recovery marker changed')
                    return marker
                async with barrier.admitted(self._owner):
                    marker = await read_marker()
                    from src.openclank.transcript_projection import read_managed_turn_receipt
                    receipt = await asyncio.to_thread(read_managed_turn_receipt, chat_id, self._owner, identity)
                    if (not receipt or receipt.get('accountID') != account or receipt.get('rootSessionID') != root
                            or receipt.get('outcome') != marker['outcome'] or receipt.get('executionFinish') is not None
                            or not receipt.get('holderID')):
                        raise ValueError('recovery receipt unavailable')
                    if (await read_marker() != marker or await self._handle_session_binding_read({'sessionID':root}) != binding
                            or auth.account_id(self._owner) != account or auth.username_for_account_id(account) != self._owner):
                        raise ValueError('recovery authority changed')
                    async with self._managed_execution_lock:
                        if (key in self._managed_actor_turns or getattr(provider_context, 'holder_id', None) != self._managed_provider_generation):
                            raise ValueError('recovery execution changed')
                    result = {**identity, 'state':'finished', 'outcome':marker['outcome']}
            except Exception:
                pass
        return validate_managed_method_result(method, result)

    async def _settle_failed_directory_lease(self, *, lease, state, owner, host_session, native_session, root):
        """Prove this original failed execution has physically left its runners.

        Existing finished sets only select the signed receipt; a null recovery
        marker is never treated as an outcome or a standalone quiescence proof.
        """
        from core.models import get_session_manager_instance
        from src.openclank.mimo_supervisor import AgentWorkerLease
        from src.openclank.transcript_projection import read_managed_turn_receipt
        if type(lease) is not AgentWorkerLease or not owner or not host_session or not native_session or not root: return False
        attachment = state.foreground_attachment
        worker = lease.worker; pool = lease._pool; process = getattr(worker, '_proc', None)
        holder = self._managed_provider_generation
        barrier = attachment.get('barrier') if isinstance(attachment, dict) else None
        admission = attachment.get('admission') if isinstance(attachment, dict) else None
        account = state.owner_account_id
        def current():
            try:
                auth = getattr(getattr(get_session_manager_instance(), 'lifecycle_context', None), 'auth_manager', None)
                return bool(self._turns.get(native_session) is state and state.root_operation_id == root
                    and state.foreground_attachment is attachment and state.original_credential_current
                    and state.original_credential_current() is True and account and auth
                    and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                    and isinstance(attachment, dict) and attachment.get('account') == account and attachment.get('private_authority') is None
                    and attachment.get('lease') is lease and attachment.get('worker') is worker
                    and attachment.get('process') is process and attachment.get('barrier') is barrier
                    and attachment.get('admission') is admission and admission and not admission.released
                    and barrier and not barrier._fenced(admission.owner) and not getattr(self, '_ownerAdmissionHolderStopped', False)
                    and not lease._released and lease.owner == owner and lease._directory_issued
                    and not lease._directory_settled and lease._directory_token is not None
                    and lease._directory_process is process and process is not None and process.returncode is None
                    and pool.worker_for_owner(owner) is worker and getattr(worker, '_bridge', None) is self
                    and getattr(worker, '_proc', None) is process and worker.is_alive()
                    and getattr(worker, 'installed_generation', None) == lease.generation
                    and self._direct_foreground_current(attachment, owner=owner, host_session=host_session,
                        native_session=native_session, root=root, holder=holder))
            except Exception: return False
        try:
            async with asyncio.timeout(2.0):
                if not current() or not await barrier.is_live(owner, admission):
                    logger.debug('failed-directory-settlement: initial-current'); return False
                async with self._managed_execution_lock:
                    executions = [key for key in self._managed_finished_executions if key[:3] == (native_session,root,'main')]
                    if len(executions) != 1:
                        logger.debug('failed-directory-settlement: execution-count'); return False
                    execution = executions[0]
                    turns = [(key, outcome) for key,outcome in self._managed_finished_turns.items() if key[:4] == execution]
                    if not turns or len(turns) > 16 or any(key[:4] == execution for key in self._managed_actor_turns):
                        logger.debug('failed-directory-settlement: turns'); return False
                binding = await self._handle_session_binding_read({'sessionID':native_session})
                if binding.get('owner') != owner or binding.get('stableChatID') != host_session:
                    logger.debug('failed-directory-settlement: binding'); return False
                receipts = []
                for key, outcome in turns:
                    identity = {'sessionID':key[0], 'rootOperationID':key[1], 'actorID':key[2], 'executionRevision':key[3], 'innerTurnID':key[4]}
                    receipt = await asyncio.to_thread(read_managed_turn_receipt, host_session, owner, identity)
                    if (type(receipt) is dict and receipt.get('accountID') == account and receipt.get('rootSessionID') == native_session
                            and receipt.get('holderID') == holder and receipt.get('outcome') == outcome
                            and receipt.get('executionFinish') == {'outcome':'failure'}): receipts.append((identity, receipt))
                if len(receipts) != 1:
                    logger.debug('failed-directory-settlement: receipt-count'); return False
                identity, receipt = receipts[0]
                request = {'directory':binding['canonicalCwd'], 'rootSessionID':native_session, 'sessionID':native_session,
                           'actorID':'main', 'executionRevision':identity['executionRevision']}
                logger.debug('failed-directory-settlement: quiescence-start')
                async def proof():
                    result = await worker.managed_engine_call('_openclank/session/v1/actor/settlement-read', request)
                    if (type(result) is not dict or set(result) != {'sessionID','actorID','executionRevision','unresolved'}
                            or result.get('sessionID') != native_session or result.get('actorID') != 'main'
                            or result.get('executionRevision') != identity['executionRevision'] or result.get('unresolved') is not None): return False
                    tree = await worker._read_managed_root_actors(native_session)
                    actor = self._find_actor_in_tree(tree, native_session, 'main')
                    return bool(type(actor) is dict and actor.get('executionRevision') == identity['executionRevision']
                        and actor.get('status') == 'idle' and actor.get('executionActive') is False and actor.get('lastOutcome') == 'failure')
                # A short external wait permits normal outer-fiber teardown.
                while current():
                    try:
                        if await proof(): break
                    except Exception: pass
                    await asyncio.sleep(.05)
                else: return False
                if (not await barrier.is_live(owner, admission) or not current()
                        or await asyncio.to_thread(read_managed_turn_receipt, host_session, owner, identity) != receipt
                        or await self._handle_session_binding_read({'sessionID':native_session}) != binding
                        or not await proof()): return False
                async with self._managed_execution_lock:
                    if (execution not in self._managed_finished_executions
                            or any(key[:4] == execution for key in self._managed_actor_turns)
                            or [(key,outcome) for key,outcome in self._managed_finished_turns.items() if key[:4] == execution] != turns): return False
                    with self._stdin_issue_lock:
                        if not current(): return False
                        lease.mark_directory_settled()
                logger.debug('failed-directory-settlement: marked')
                return True
        except Exception:
            logger.debug('failed-directory-settlement: unavailable')
            return False

    async def _terminal_error_current(self, identity, entry, root, output):
        """Verify one persisted Native marker while the original Host turn is held."""
        try:
            from core.models import get_session_manager_instance
            manager=get_session_manager_instance(); lifecycle=getattr(manager,'lifecycle_context',None) if manager else None
            auth=getattr(lifecycle,'auth_manager',None); barrier=getattr(lifecycle,'account_request_barrier',None)
            supervisor=getattr(lifecycle,'mimo_supervisor',None); pool=supervisor.owner_lifecycle_pool() if supervisor else None
            worker=pool.worker_for_owner(self._owner) if pool else None; owner_state=pool._states.get(pool._key(self._owner)) if pool else None
            admission=entry.get('ownerAdmission'); account=entry.get('ownerAdmissionAccount'); process=getattr(worker,'_proc',None)
            epoch=pool._owner_lifecycle_epoch(pool._key(self._owner)) if pool else None; generation=getattr(owner_state,'generation',None)
            holder=self._managed_provider_generation; chat=entry.get('stableChatID'); key=self._managed_turn_key(identity)
            snapshot={k:copy.deepcopy(entry.get(k)) for k in ('stableChatID','authorityWorkspaceID','workspaceRevision','mappingRevision','mapRevision','holderID','rootSessionID')}
            def current():
                try:
                    return bool(identity['actorID']=='main' and auth and barrier and pool and worker and process and owner_state and admission and not admission.released
                        and not getattr(self,'_ownerAdmissionHolderStopped',False) and not entry.get('ownerAdmissionStopped') and entry.get('ownerAdmissionBarrier') is barrier
                        and entry.get('ownerAdmission') is admission and entry.get('state')=='active' and not entry.get('authority')
                        and self._managed_actor_turns.get(key) is entry and self._managed_actor_executions.get(key[:4]) is entry
                        and all(entry.get(k)==v for k,v in snapshot.items()) and root is self._turns.get(entry.get('rootSessionID'))
                        and root is not None and root.root_operation_id==identity['rootOperationID'] and root.owner_account_id==account
                        and root.forward_pair_eligible and not root.privacy_capture_veto and callable(root.original_credential_current) and root.original_credential_current()
                        and self._session_map.get(chat)==identity['sessionID'] and (self._session_context.get(identity['sessionID']) or {}).get('root_operation_id')==identity['rootOperationID']
                        and account and auth.account_id(self._owner)==account and auth.username_for_account_id(account)==self._owner
                        and holder and self._managed_provider_generation==holder and entry.get('holderID')==holder
                        and pool._states.get(pool._key(self._owner)) is owner_state and owner_state.active is worker and owner_state.generation==generation and owner_state.status=='ready'
                        and getattr(worker,'_bridge',None) is self and getattr(worker,'_proc',None) is process and worker.is_alive() and pool._owner_lifecycle_epoch(pool._key(self._owner))==epoch)
                except Exception: return False
            if not current() or not await barrier.is_live(self._owner,admission): return None
            value=await pool.session_terminal_diagnostic_source(chat,identity['sessionID'],output['messageID'],owner=self._owner,timeout=2.0)
            names={'ProviderAuthError','APIError','MessageOutputLengthError','StructuredOutputError','InvalidOutputError','TextToolCallError','ContentFilterError','ModelError','UnknownError'}
            if (type(value) is not dict or set(value)!={'version','sessionID','messageID','identity','diagnostic','messageUpdated'} or type(value['version']) is not int or value['version']!=1
                    or value['sessionID']!=identity['sessionID'] or value['messageID']!=output['messageID'] or value['identity']!=identity
                    or type(value['messageUpdated']) is not int or not 0<=value['messageUpdated']<=9007199254740991): return None
            marker=value['diagnostic']
            if type(marker) is not dict or set(marker)!={'version','errorName','stage'} or type(marker['version']) is not int or marker['version']!=1 or marker['errorName'] not in names or marker['stage'] not in ('processor','prompt_classification'): return None
            callbacks=getattr(self,'_logging_callbacks',None); capture=getattr(callbacks,'_capture',None)
            policy_reader=getattr(capture,'actor_diagnostic_policy',None)
            actor_identity={k:identity[k] for k in ('sessionID','rootOperationID','actorID','executionRevision')}
            if (not callable(policy_reader) or getattr(callbacks,'owner',None)!=self._owner
                    or getattr(callbacks,'holder_id',None)!=holder): return None
            logging_policy=await asyncio.to_thread(policy_reader,self._owner,holder,actor_identity,account,current)
            if (type(logging_policy) is not dict or getattr(self,'_logging_callbacks',None) is not callbacks
                    or not current() or not await barrier.is_live(self._owner,admission)): return None
            return {'identity':copy.deepcopy(identity),'messageID':output['messageID'],'messageUpdated':value['messageUpdated'],
                    'diagnostic':copy.deepcopy(marker),'loggingPolicy':logging_policy},current
        except Exception:
            return None

    async def _source_closure_current(self, identity, entry, root, closure, output):
        """Optional, bounded original-turn observation; never a capture grant."""
        _closure_trace = logger.isEnabledFor(logging.DEBUG)
        _closure_phase = 'setup'
        _closure_started = time.monotonic() if _closure_trace else 0.0
        _closure_timings = {} if _closure_trace else None
        _closure_outcome = 'refused'
        def _closure_mark(name):
            nonlocal _closure_phase, _closure_started
            if _closure_timings is not None:
                now = time.monotonic(); _closure_timings[_closure_phase] = round((now - _closure_started) * 1000, 3); _closure_started = now
            _closure_phase = name
        try:
            _closure_mark('validation')
            from core.models import get_session_manager_instance
            from src.openclank.chat_source_bridge import _source_closure_matches, _issue_host_constructor_manifest
            from src.openclank.memory_policy import STORE
            if not isinstance(entry, dict) or root is None or entry.get('state') != 'active': return False
            source=entry.get('forwardPairInput')
            if not isinstance(source, dict): return False
            wire=closure.get('wire') if type(closure) is dict else None
            source_scope=None
            ref=None
            constructor_manifest=None
            try:
                env=wire['prefix']['environment']
                if env.get('version') == 2:
                    from src.memery_external_sources import SourceRef, _source_ref_error
                    payload=json.loads(source['attestation'])['payload']
                    ref=SourceRef(**payload['source'])
                    if (_source_ref_error(ref) or ref.account_id != entry.get('ownerAccountID')
                            or ref.chat_id != entry.get('stableChatID') or ref.message_id != identity['rootOperationID']): return False
                    source_scope=(ref.version,ref.workspace_id)
            except (KeyError,TypeError,ValueError): return False
            if type(wire) is dict and wire.get('version') == 2:
                boundary=entry.get('maintenancePromptBoundary')
                try:
                    layout=source['layout']; position=source['sourcePosition']
                    if (type(boundary) is not dict or boundary.get('coverage') != 'candidate'
                            or layout != list(boundary.get('parts', ())) or position != boundary.get('sourcePosition')
                            or source['layoutSHA256'] != _maintenance_layout_digest(layout,position)
                            or wire.get('layoutSHA256') != source['layoutSHA256'] or wire.get('sourcePosition') != position): return False
                except (KeyError,TypeError,ValueError): return False
                if wire.get('generated'):
                    if ref is None:return False
                    if ('host-session-personal-env.v1' in [item.get('constructor') for item in wire['generated'] if type(item) is dict]
                            and (source_scope != (2,None) or entry.get('authorityWorkspaceID') != '')): return False
                    constructor_manifest=_issue_host_constructor_manifest(identity=identity,account=entry.get('ownerAccountID'),
                        chat=entry.get('stableChatID'),source_hash=ref.content_hash,holder=entry.get('holderID'),
                        session_environment=entry.get('sessionEnvironment'),
                        constructor_ids=[item.get('constructor') for item in wire['generated'] if type(item) is dict])
                    if constructor_manifest is None:return False
                if not _source_closure_matches(closure,identity=identity,source=source,output=output,layout=layout,source_position=position,
                        source_scope=source_scope,owner_account=entry.get('ownerAccountID'),chat_id=entry.get('stableChatID'),
                        constructor_manifest=constructor_manifest.value() if constructor_manifest else None,
                        source_hash=ref.content_hash if ref else None): return False
            elif not _source_closure_matches(closure,identity=identity,source=source,output=output,source_scope=source_scope): return False
            manager=get_session_manager_instance(); lifecycle=getattr(manager,'lifecycle_context',None) if manager else None
            auth=getattr(lifecycle,'auth_manager',None); barrier=getattr(lifecycle,'account_request_barrier',None)
            supervisor=getattr(lifecycle,'mimo_supervisor',None); pool=supervisor.owner_lifecycle_pool() if supervisor else None
            callbacks=getattr(self._managed_provider_context,'__self__',None)
            worker=pool.worker_for_owner(self._owner) if pool else None
            owner_state=pool._states.get(pool._key(self._owner)) if pool else None
            account=entry.get('ownerAdmissionAccount'); admission=entry.get('ownerAdmission')
            process=getattr(worker,'_proc',None); generation=getattr(worker,'installed_generation',None)
            epoch=pool._owner_lifecycle_epoch(pool._key(self._owner)) if pool else None
            holder=self._managed_provider_generation; scope=closure['wire']['prefix']['environment']['scope']
            fields=('stableChatID','authorityWorkspaceID','workspaceRevision','mappingRevision','mapRevision','effectiveModel','modelRequestOptions')
            original_fields={key:copy.deepcopy(entry.get(key)) for key in fields}
            privileges=auth.get_privileges(self._owner) or {} if auth is not None else {}
            allowed=bool(privileges.get('can_manage_memory',True))
            def local_current():
                try:
                    return bool(auth is not None and barrier is not None and pool is not None and worker is not None and process is not None
                        and not getattr(self,'_ownerAdmissionHolderStopped',False) and not entry.get('ownerAdmissionStopped')
                        and entry.get('ownerAdmissionBarrier') is barrier and entry.get('ownerAdmission') is admission and admission is not None and not admission.released
                        and self._managed_actor_turns.get(self._managed_turn_key(identity)) is entry
                        and self._managed_actor_executions.get(self._managed_turn_key(identity)[:4]) is entry
                        and entry.get('state')=='active' and entry.get('forwardPairV1') and not entry.get('authority')
                        and all(entry.get(key)==original_fields[key] for key in fields)
                        and bool((auth.get_privileges(self._owner) or {}).get('can_manage_memory',True))==allowed
                        and self._turns.get(entry.get('rootSessionID')) is root and root.root_operation_id==identity['rootOperationID']
                        and self._session_map.get(entry.get('stableChatID'))==identity['sessionID']
                        and (self._session_context.get(identity['sessionID']) or {}).get('root_operation_id')==identity['rootOperationID']
                        and hashlib.sha256(str((self._session_context.get(identity['sessionID']) or {}).get('physical_cwd') or '').encode('utf-8')).hexdigest()==scope['cwdSHA256']
                        and root.forward_pair_eligible and not root.privacy_capture_veto and root.owner_account_id==account
                        and account and auth.account_id(self._owner)==account and auth.username_for_account_id(account)==self._owner
                        and holder and self._managed_provider_generation==holder and entry.get('holderID')==holder
                        and getattr(callbacks,'holder_id',None)==holder and owner_state is pool._states.get(pool._key(self._owner))
                        and owner_state.active is worker and owner_state.generation==generation and owner_state.status=='ready'
                        and getattr(worker,'_bridge',None) is self and getattr(worker,'_proc',None) is process and worker.is_alive()
                        and pool._owner_lifecycle_epoch(pool._key(self._owner))==epoch)
                except Exception: return False
            if not local_current() or not await barrier.is_live(self._owner,admission): return False
            _closure_mark('initial_resolve')
            before=await self._resolve_standing_guidance_turn(identity)
            binding=await self._handle_session_binding_read({'sessionID':identity['sessionID']})
            model=entry.get('effectiveModel') or {}
            if (scope['ownerAccountID']!=account or scope['chatID']!=entry.get('stableChatID')
                or scope['authorityWorkspaceID']!=entry.get('authorityWorkspaceID')
                or any(scope[k]!=entry.get(k) for k in ('workspaceRevision','mappingRevision','mapRevision'))
                or binding.get('canonicalCwd') is None or hashlib.sha256(binding['canonicalCwd'].encode('utf-8')).hexdigest()!=scope['cwdSHA256']
                or scope['providerID']!=model.get('providerID') or scope['modelID']!=model.get('modelID')): return False
            _closure_mark('policy')
            policy=await asyncio.to_thread(STORE.capture_policy_snapshot,self._owner,read_privilege=allowed,write_privilege=allowed,incognito=False,no_memory=False,compare_mode=False)
            _closure_mark('final_resolve')
            after=await self._resolve_standing_guidance_turn(identity)
            again=await self._handle_session_binding_read({'sessionID':identity['sessionID']})
            live_execution=await self.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
            _closure_mark('fresh_policy')
            fresh=await asyncio.to_thread(STORE.capture_policy_snapshot,self._owner,read_privilege=allowed,write_privilege=allowed,incognito=False,no_memory=False,compare_mode=False)
            _closure_mark('final_checks')
            valid=bool(before==after and binding==again and live_execution.get('holderID')==holder
                and live_execution.get('effectiveModel')==entry.get('effectiveModel') and live_execution.get('modelRequestOptions')==entry.get('modelRequestOptions')
                and policy.get('eligibility_state')=='available' and policy.get('capture_allowed') is True
                and fresh.get('eligibility_state')=='available' and fresh.get('capture_allowed') is True and fresh.get('fingerprint')==policy.get('fingerprint')
                and bool((auth.get_privileges(self._owner) or {}).get('can_manage_memory',True))==allowed
                and await barrier.is_live(self._owner,admission) and local_current())
            _closure_outcome = 'ready' if valid else 'refused'
            if valid and constructor_manifest is not None:
                entry['sourceConstructorManifest']=constructor_manifest
            return local_current if valid else None
        except asyncio.CancelledError:
            _closure_outcome = 'cancelled'
            raise
        except Exception:
            _closure_outcome = 'error'
            raise
        finally:
            if _closure_timings is not None:
                try:
                    _closure_mark('done')
                    logger.debug('source closure outcome=%s phases_ms=%s', _closure_outcome, _closure_timings)
                except Exception:
                    pass

    async def _handle_managed_turn_finish(self, params: dict) -> dict:
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import (
            validate_managed_method_request,
            validate_managed_method_result,
        )

        method = "_openclank/session/v1/turn/finish"
        request = validate_managed_method_request(method, params)
        key = self._managed_turn_key(request)
        cancelled = False
        queued_completion = None
        # Close optional queued staging under the short entry lock, then drain
        # it without holding the C2 lock or any chat guard. A failed stage never
        # changes the ordinary turn result.
        async with self._managed_execution_lock:
            closing_entry = self._managed_actor_turns.get(key)
            kernel_records = []
            if isinstance(closing_entry,dict):
                closing_entry['queuedSourceClosing'] = True
                closing_entry['pythonKernelClosing'] = True
                with self._stdin_issue_lock: closing_entry['backgroundStartClosing'] = True
                closing_entry['taskCreateClosing'] = True
                frame_tasks=[]
                for frame in closing_entry.get('actorFrameCalls',{}).values():
                    frame['invalidated'].set()
                    if isinstance(frame.get('task'),asyncio.Task):frame_tasks.append(frame['task'])
                background_tasks = [record.get('task') for record in closing_entry.get('backgroundStartAdmitted',{}).values()
                    if isinstance(record,dict) and isinstance(record.get('task'),asyncio.Task)]
                background_tasks.extend(frame_tasks)
                shell_approvals = [record['shellApproval'] for record in closing_entry.get('backgroundStartAdmitted',{}).values()
                    if isinstance(record,dict) and isinstance(record.get('shellApproval'),tuple)]
                file_approvals = [record['fileApproval'] for record in closing_entry.get('backgroundStartAdmitted',{}).values()
                    if isinstance(record,dict) and isinstance(record.get('fileApproval'),tuple)]
                files_prepared = [record.get('ordinaryPrepared') for record in closing_entry.get('backgroundStartAdmitted',{}).values()
                    if isinstance(record,dict) and record.get('ordinaryPrepared') is not None]
                kernel_scopes = {(record['manager'], record['reservation'].scope)
                    for record in closing_entry.get('pythonKernelCalls', {}).values() if record.get('reservation') is not None}
                for record in closing_entry.get('pythonKernelCalls', {}).values():
                    if record.get('reservation') is not None and not record.get('completed'):
                        record['cancelled'] = True
                        record['manager'].revoke_call(record['reservation'])
                        kernel_records.append(record)
            else:
                kernel_scopes = set()
                background_tasks = []
                shell_approvals = []
                file_approvals = []
                files_prepared = []
            queued_stage = closing_entry.get('queuedSourceStageTask') if isinstance(closing_entry,dict) else None
        from src.agent_tools.filesystem_tools import resolve_file_approval
        for request_id,owner,chat,account,pending in file_approvals:
            resolve_file_approval(request_id,'reject',owner=owner,session_id=chat,account_id=account,expected_pending=pending)
        from src.shell_policy import resolve_shell_approval
        for request_id,owner,chat,account in shell_approvals:
            resolve_shell_approval(request_id,'reject',owner=owner,session_id=chat,account_id=account)
        for task in background_tasks:
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: cancelled = True
                except Exception: break
            try: task.result()
            except (Exception,asyncio.CancelledError): pass
        async with self._managed_execution_lock:
            if isinstance(closing_entry,dict):
                files_prepared.extend(record.get('ordinaryPrepared') for record in closing_entry.get('backgroundStartAdmitted',{}).values()
                    if isinstance(record,dict) and record.get('ordinaryPrepared') is not None)
        cancelled = await _retire_unissued_files_preparations(files_prepared) or cancelled
        for record in kernel_records:
            drain = asyncio.create_task(asyncio.to_thread(record['manager'].drain_call,record['reservation']))
            while not drain.done():
                try: await asyncio.shield(drain)
                except asyncio.CancelledError: cancelled = True
            drain.result()
            task = record.get('task')
            if task is not None:
                while not task.done():
                    try: await asyncio.shield(task)
                    except asyncio.CancelledError: cancelled = True
                    except Exception: break
                try: task.result()
                except (Exception,asyncio.CancelledError): pass
        turn_key = json.dumps(key,separators=(',',':'),ensure_ascii=True)
        for kernels, scope in kernel_scopes:
            kernels.retire_turn(scope,turn_key=turn_key)
        if isinstance(queued_stage,asyncio.Task):
            while not queued_stage.done():
                try: await asyncio.shield(queued_stage)
                except asyncio.CancelledError: cancelled = True
                except Exception: break
            try: queued_stage.result()
            except (Exception,asyncio.CancelledError): pass
        closure_accepted = False
        closure_root = None
        async with self._managed_execution_lock:
            if key in self._managed_finished_turns:
                if self._managed_finished_turns[key] != request["outcome"]:
                    raise ValueError("managed turn finish replay changed its outcome")
                if request.get('terminalErrorOutput') is not None:
                    prior=self._turns.get(request['sessionID']); witness=getattr(prior,'terminal_error_finish',None)
                    identity={field:request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
                    if not isinstance(witness,dict) or witness.get('identity')!=identity or witness.get('messageID')!=request['terminalErrorOutput']['messageID']: raise ValueError('terminal witness unavailable')
                return validate_managed_method_result(method, {"accepted": True, "replayed": True})
            if key not in self._managed_actor_turns:
                raise ValueError("managed turn finish does not match an active identity")
            entry = self._managed_actor_turns[key]
            if closing_entry is not None and entry is not closing_entry:
                raise ValueError('managed turn changed while queued staging drained')
            if entry.get('sourceClosureFinishPending'):
                raise ValueError('managed turn finish is already settling')
            if not entry.get('authority') and entry.get('ownerAccountID'):
                # An account request lease fences rename/reset while the exact
                # host receipt is committed; no native IPC occurs in its DB transaction.
                manager = get_session_manager_instance()
                state = getattr(manager, 'lifecycle_context', None) if manager is not None else None
                auth = getattr(state, 'auth_manager', None)
                barrier = getattr(state, 'account_request_barrier', None)
                if auth is None or barrier is None:
                    raise ValueError('managed turn receipt authority unavailable')
                async with barrier.admitted(entry['owner']):
                    account = entry['ownerAccountID']
                    if auth.account_id(entry['owner']) != account or auth.username_for_account_id(account) != entry['owner']:
                        raise ValueError('managed turn receipt account changed')
                    receipt = {'version':1, 'owner':entry['owner'], 'accountID':account,
                        'chatID':entry['stableChatID'], 'holderID':entry['holderID'],
                        'rootSessionID':entry['rootSessionID'], 'sessionID':request['sessionID'],
                        'rootOperationID':request['rootOperationID'], 'actorID':request['actorID'],
                        'executionRevision':request['executionRevision'], 'innerTurnID':request['innerTurnID'],
                        'outcome':request['outcome'], 'workspaceRevision':entry['workspaceRevision'],
                        'mappingRevision':entry['mappingRevision'], 'authorityWorkspaceID':entry['authorityWorkspaceID']}
                    from src.openclank.transcript_projection import record_managed_turn_receipt
                    task = asyncio.create_task(asyncio.to_thread(record_managed_turn_receipt, receipt))
                    while True:
                        try:
                            await asyncio.shield(task)
                            break
                        except asyncio.CancelledError:
                            if task.cancelled(): raise ValueError('managed turn receipt outcome unavailable')
                            cancelled = True
                    if auth.account_id(entry['owner']) != account or auth.username_for_account_id(account) != entry['owner']:
                        raise ValueError('managed turn receipt account changed')
                    entry['lastFinishedTurn'] = {field:request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
                    if request['outcome'] == 'completed' and isinstance(entry.get('queuedSourceStaged'),dict):
                        callback=getattr(state,'queued_source_completion_callback',None)
                        if callable(callback):
                            queued_completion=(callback,copy.deepcopy(entry['lastFinishedTurn']),copy.deepcopy(entry['queuedSourceStaged']))
            if request.get('terminalErrorOutput') is not None:
                terminal_root=self._turns.get(entry.get('rootSessionID')); finish_token=object(); entry['sourceClosureFinishPending']=finish_token
                terminal_verified=None
                self._managed_execution_lock.release()
                try:
                    try:
                        async with asyncio.timeout(2.0):
                            identity={field:request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
                            terminal_verified=await self._terminal_error_current(identity,entry,terminal_root,request['terminalErrorOutput'])
                    except asyncio.CancelledError: cancelled=True
                    except Exception: pass
                finally:
                    while True:
                        try: await self._managed_execution_lock.acquire(); break
                        except asyncio.CancelledError: cancelled=True
                    if entry.get('sourceClosureFinishPending') is finish_token: entry.pop('sourceClosureFinishPending',None)
                    else: terminal_verified=None
                if self._managed_actor_turns.get(key) is not entry: raise ValueError('managed turn changed while terminal proof settled')
                if terminal_root is not None and terminal_root is self._turns.get(entry.get('rootSessionID')):
                    terminal_root.terminal_error_finish={'identity':identity,'messageID':request['terminalErrorOutput']['messageID']}
                if terminal_verified and terminal_verified[1]() and terminal_root is self._turns.get(entry.get('rootSessionID')):
                    terminal_root.terminal_error_witness=terminal_verified[0]
            # Receipt is settled. Re-read optional authority outside the C2 lock;
            # the token prevents another finish from claiming this entry meanwhile.
            if request.get('outcome')=='completed' and request.get('sourceClosure') is not None and request.get('forwardPairOutput') is not None:
                closure_root=self._turns.get(entry.get('rootSessionID'))
                finish_token=object(); entry['sourceClosureFinishPending']=finish_token
                self._managed_execution_lock.release()
                try:
                    try:
                        async with asyncio.timeout(0.2):
                            identity={field:request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
                            closure_accepted=await self._source_closure_current(identity,entry,closure_root,request['sourceClosure'],request['forwardPairOutput'])
                    except asyncio.CancelledError:
                        cancelled=True
                    except Exception:
                        pass
                finally:
                    while True:
                        try:
                            await self._managed_execution_lock.acquire()
                            break
                        except asyncio.CancelledError:
                            cancelled=True
                    if entry.get('sourceClosureFinishPending') is finish_token:
                        entry.pop('sourceClosureFinishPending',None)
                    else:
                        closure_accepted=False
                if self._managed_actor_turns.get(key) is not entry:
                    raise ValueError('managed turn changed while optional proof settled')
            from src.openclank.browser_control_service import configured_browser_control_service
            service = configured_browser_control_service()
            if service is not None and not entry.get("authority"):
                identity = {field: request[field] for field in ("sessionID", "rootOperationID", "actorID", "executionRevision", "innerTurnID")}
                service.clear_managed_turn_policy(owner=entry["owner"], host_session_id=entry["stableChatID"],
                    workspace_id=entry["authorityWorkspaceID"], binding_revision=entry["workspaceRevision"], identity=identity)
            root = self._turns.get(entry.get('rootSessionID'))
            if (request.get('outcome') == 'completed' and request.get('forwardPairOutput') and entry.get('forwardPairV1')
                    and isinstance(entry.get('forwardPairInput'), dict) and root is not None
                    and root.forward_pair_eligible and root.root_operation_id == request['rootOperationID']
                    and root.owner_account_id == entry.get('ownerAccountID') and not root.privacy_capture_veto):
                root.forward_pair_witness = {'identity': {field: request[field] for field in
                    ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')},
                    'input': copy.deepcopy(entry['forwardPairInput']), 'output': copy.deepcopy(request['forwardPairOutput'])}
                if (callable(closure_accepted) and closure_accepted() and entry is closing_entry and root is closure_root and not entry.get('ownerAdmissionStopped')
                    and self._managed_provider_generation == entry.get('holderID') and not root.privacy_capture_veto):
                    root.forward_pair_witness['sourceClosure'] = copy.deepcopy(request['sourceClosure'])
                    if entry.get('sourceConstructorManifest') is not None:
                        root.forward_pair_witness['constructorManifest'] = entry['sourceConstructorManifest']
                    try:
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug('source closure attachment outcome=attached')
                    except Exception:
                        pass
                elif request.get('sourceClosure') is not None:
                    try:
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug('source closure attachment outcome=refused')
                    except Exception:
                        pass
            # A settled turn cannot donate its consumed input credential to a
            # later turn. Fence late registration of the same receipt too.
            origin_state=entry.get('inputOriginSet') or {}
            registry=self._receipt_origin_registry.get(entry.get('rootSessionID'))
            if registry and registry.get('context') is self._session_context.get(entry.get('rootSessionID')):
                now=time.monotonic(); slots=registry['slots']; retired=registry['retired']
                for old in [k for k,v in retired.items() if v <= now]: retired.pop(old,None)
                for item in origin_state.get('receipts', []):
                    receipt_key=(item['owner'],entry.get('ownerAccountID'),item['stableChatID'],item['sessionID'],item['actorID'],item['requestID'])
                    slots.pop(receipt_key,None)
                    receipt_identity=(item['owner'],item['stableChatID'],item['sessionID'],item['actorID'],item['requestID'])
                    if len(retired)<512 or receipt_identity in retired: retired[receipt_identity]=now+3600
                    else:
                        registry['fullUntil']=now+3600
                        slots.clear()
                        break
            if entry.get('authority'):
                execution = self._managed_actor_executions.get(key[:4])
                if execution is not entry:
                    raise ValueError('private managed execution changed before turn finish')
                execution['lastFinishedTurn'] = {field:request[field] for field in
                    ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
                execution['privateFinishedTurnOutcome'] = request['outcome']
            self._managed_actor_turns.pop(key)
            self._managed_finished_turns[key] = request["outcome"]
            while len(self._managed_finished_turns) > 1024:
                self._managed_finished_turns.pop(next(iter(self._managed_finished_turns)))
        if queued_completion is not None:
            callback,identity,staged=queued_completion
            task=asyncio.create_task(callback(self,identity,staged))
            def consume(done):
                try: done.exception()
                except (Exception,asyncio.CancelledError): pass
            task.add_done_callback(consume)
        if cancelled:
            raise asyncio.CancelledError
        return validate_managed_method_result(method, {"accepted": True, "replayed": False})

    async def _handle_worker_naming(self, params: dict) -> dict:
        from src.openclank.worker_naming import request
        return await request(self, params)

    async def _handle_managed_tool_admit(self, params: dict) -> dict:
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = "_openclank/session/v1/tool/admit"
        request = validate_managed_method_request(method, params)
        operation = request.get('toolOperation')
        if request.get('shellRewriteAdmission') is not None and (request['toolName'] != 'bash' or request['kind'] != 'tool'):
            raise ValueError('shell rewrite admission unavailable')
        if (operation is not None and (request['toolName'] != 'theme_emoji_assets' or request['kind'] != 'tool')):
            raise ValueError('tool operation unavailable')
        identity = {field: request[field] for field in ("sessionID", "rootOperationID", "actorID", "executionRevision", "innerTurnID")}
        key = self._managed_turn_key(identity)
        async with self._managed_execution_lock:
            entry = self._managed_actor_turns.get(key)
            policy_revision = entry.get("policyRevision") if entry is not None else None
        resolver = getattr(self, "_managed_tool_policy_resolver", None)
        if entry is None or resolver is None:
            return validate_managed_method_result(method, {"allowed": False, "reason": "authority_unavailable"})
        invocation_id = None
        rewrite_candidate = None
        recall_trace = logger.isEnabledFor(logging.DEBUG) and request['kind'] == 'memory_read' and request['toolName'] == 'recall_memory'
        timings = {} if recall_trace else None
        diagnostic_outcome = 'refused'
        diagnostic_started = time.monotonic() if recall_trace else None
        loop = asyncio.get_running_loop()
        phase = ['actor', loop.time()]
        def mark(name):
            if timings is not None:
                now = loop.time(); timings[phase[0]] = round((now - phase[1]) * 1000, 3); phase[:] = [name, now]
        try:
            current = await self.resolve_managed_actor_execution(identity["sessionID"], identity["rootOperationID"],
                identity["actorID"], identity["executionRevision"], **({"_recall_timings": timings} if recall_trace else {}))
            mark('binding')
            binding = await self._handle_session_binding_read({"sessionID": current["rootSessionID"]})
            mark('account')
            account = entry.get('ownerAccountID')
            if entry.get('recoveryOrigin') and not account:
                raise ValueError('managed recovery account unavailable')
            if account:
                from core.models import get_session_manager_instance
                manager = get_session_manager_instance()
                state = getattr(manager, 'lifecycle_context', None) if manager is not None else None
                auth = getattr(state, 'auth_manager', None)
                if (auth is None or auth.account_id(entry['owner']) != account
                        or auth.username_for_account_id(account) != entry['owner']):
                    raise ValueError('managed owner account changed')
            if current.get("authority") and request["kind"] in {"memory_read", "skill_use"}:
                raise ValueError("private memory authority is unavailable")
            mark('policy')
            allowed = await resolver(identity, entry, request["toolName"], request["kind"], {**current, "toolOperation":operation})
            mark('final')
            selector = request.get('shellRewriteAdmission')
            if (allowed and selector is not None and request['kind'] == 'tool' and request['toolName'] == 'bash'
                    and isinstance(request.get('toolCallID'), str) and request['toolCallID']):
                try:
                    root = self._turns.get(current['rootSessionID'])
                    retained = getattr(root, 'shell_rewrite_preference', None) if root is not None else None
                    from src.agent_settings import AdmittedGcfPreference, agent_settings_revision, validate_agent_settings
                    from src.openclank.mimo_supervisor import AgentWorkerLease
                    if type(retained) is tuple and len(retained) == 3:
                        preference, lease, holder = retained
                        if (type(preference) is AdmittedGcfPreference and preference.git_diff_no_color_requested is True
                                and selector == {'version':1,'profile':'git-diff-no-color-v1','settingsRevision':preference.revision}
                                and type(lease) is AgentWorkerLease and not lease._released and lease.owner == entry.get('owner')
                                and lease.worker is lease._pool.worker_for_owner(lease.owner) and lease.worker._bridge is self
                                and lease.worker.is_alive() and getattr(lease.worker,'_proc',None) is not None
                                and getattr(lease.worker,'_client',None) is self._client
                                and lease.generation == getattr(lease.worker,'installed_generation',None)
                                and lease._pool._owner_lifecycle_epoch(lease.owner) == lease._owner_epoch
                                and getattr(getattr(lease.worker,'_managed_callbacks',None),'holder_id',None) == holder
                                and holder == entry.get('holderID') == self._managed_provider_generation
                                and isinstance(account, str) and account and root.root_operation_id == identity['rootOperationID']
                                and root.owner_account_id == account and not root.provider_root_closing):
                            def read_rewrite_preference():
                                from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
                                with PREFERENCE_LOCK:
                                    fresh = validate_agent_settings((_load_for_user(lease.owner) or {}).get('agent_settings', {}))
                                    return (fresh.get('tokenomics', {}).get('git_diff_no_color') is True
                                            and agent_settings_revision(fresh) == preference.revision)
                            if await asyncio.to_thread(read_rewrite_preference):
                                rewrite_candidate = (root, retained, dict(selector))
                except Exception:
                    rewrite_candidate = None
            if account and (getattr(get_session_manager_instance(), 'lifecycle_context', None) is not state
                    or getattr(state,'auth_manager',None) is not auth or auth.account_id(entry['owner'])!=account
                    or auth.username_for_account_id(account)!=entry['owner']):
                allowed=False
            async with self._managed_execution_lock:
                if self._managed_actor_turns.get(key) is not entry or entry.get("policyRevision") != policy_revision:
                    allowed = False
                if (any(binding.get(field) != entry.get(field) for field in ("stableChatID", "owner"))
                    or binding.get("authority") != entry.get("authority")
                    or (not binding.get("authority") and (any(binding.get(field) != entry.get(field) for field in
                        ("mappingRevision", "workspaceRevision")) or entry.get("mapRevision", -1) > binding.get("mapRevision", -1)))):
                    allowed = False
                if rewrite_candidate is not None:
                    root, retained, _ = rewrite_candidate
                    preference, lease, holder = retained
                    if (not allowed or self._turns.get(current['rootSessionID']) is not root
                            or root.shell_rewrite_preference is not retained or root.provider_root_closing
                            or root.root_operation_id != identity['rootOperationID'] or root.owner_account_id != account
                            or lease._released or lease.worker is not lease._pool.worker_for_owner(lease.owner)
                            or lease.worker._bridge is not self or not lease.worker.is_alive()
                            or getattr(lease.worker,'_proc',None) is None or getattr(lease.worker,'_client',None) is not self._client
                            or lease.generation != getattr(lease.worker,'installed_generation',None)
                            or lease._pool._owner_lifecycle_epoch(lease.owner) != lease._owner_epoch
                            or getattr(getattr(lease.worker,'_managed_callbacks',None),'holder_id',None) != holder
                            or holder != entry.get('holderID') or holder != self._managed_provider_generation
                            or entry.get('state') != 'active'):
                        rewrite_candidate = None
                call_id = request.get('toolCallID')
                if (allowed and request['kind']=='tool' and request['toolName'] in {'manage_bg_jobs','manage_files','write_file','edit_file'}
                    and isinstance(call_id,str) and call_id):
                    admitted=entry.setdefault('backgroundStartAdmitted',{})
                    if entry.get('backgroundStartClosing') or (len(admitted)>=128 and call_id not in admitted):
                        allowed=False
                    else:
                        admitted.setdefault(call_id,{'state':'admitted'})
                if (allowed and entry.get('codeActivityEnabled') is True and request['kind'] == 'tool'
                    and request['toolName'] == 'search_code' and isinstance(call_id, str) and call_id):
                    admitted = entry.setdefault('codeActivityAdmitted', {})
                    if len(admitted) < 128 or call_id in admitted: admitted[call_id] = True
                if (allowed and (request['kind'] == 'project_policy_read' or (request['kind'] == 'tool' and request['toolName'] == 'evolve_hexes') or (request['kind'] == 'skill_use' and request['toolName'] == 'skill')) and isinstance(call_id,str) and call_id
                    and isinstance(request['toolName'],str) and request['toolName']):
                    calls = entry.setdefault('policyInvocations', {})
                    if isinstance(calls,dict) and not current.get('authority') and not binding.get('authority'):
                        policy_call = {'toolName':request['toolName'],'toolCallID':call_id,
                            'canonicalCwd':binding.get('canonicalCwd'),'authorityWorkspaceID':binding.get('authorityWorkspaceID'),
                            'holderID':current.get('holderID')}
                        if (isinstance(policy_call['canonicalCwd'],str) and policy_call['canonicalCwd']
                            and policy_call['authorityWorkspaceID']==entry.get('authorityWorkspaceID')
                            and policy_call['holderID'] and policy_call['holderID']==entry.get('holderID')):
                            invocation_id = next((token for token,call in calls.items() if call==policy_call),None)
                            if invocation_id is None and len(calls)<128:
                                invocation_id=secrets.token_urlsafe(24)
                                calls[invocation_id]=policy_call
                if (allowed and request['kind'] == 'memory_read' and request['toolName'] == 'recall_memory'
                    and identity['actorID'] == 'main' and isinstance(call_id, str) and call_id):
                    calls = entry.setdefault('feedbackInvocations', {})
                    if isinstance(calls, dict):
                        invocation_id = next((token for token, call in calls.items()
                            if isinstance(call, dict) and call.get('toolCallID') == call_id), None)
                        if invocation_id is None and len(calls) < 128:
                            invocation_id = secrets.token_urlsafe(24)
                            calls[invocation_id] = {'toolName': 'recall_memory', 'toolCallID': call_id}
            diagnostic_outcome = 'allowed' if allowed else 'refused'
        except asyncio.CancelledError:
            diagnostic_outcome = 'cancelled'
            raise
        except Exception as exc:
            diagnostic_outcome = 'timeout' if isinstance(exc, TimeoutError) else 'valueerror' if isinstance(exc, ValueError) else 'other'
            logger.warning("managed per-call tool authority unavailable", exc_info=True)
            allowed = False
        finally:
            if timings is not None:
                failed_phase=phase[0]
                mark('done')
                logger.debug('S07_RECALL_RESOLVER outcome=%s phase=%s total_ms=%s phases_ms=%s', diagnostic_outcome, failed_phase, round((time.monotonic()-diagnostic_started)*1000,3), timings)
        return validate_managed_method_result(method, {"allowed": bool(allowed), **({} if allowed else {"reason": "policy_denied"}),
            **({'invocationID': invocation_id} if allowed and invocation_id else {}),
            **({'shellRewriteProof': rewrite_candidate[2]} if allowed and rewrite_candidate is not None else {})})

    async def resolve_managed_policy_invocation(self, identity: dict, invocation_id: str, tool_name: str, tool_call_id: str) -> dict:
        """Resolve a policy-only token against the exact still-live original turn."""
        fields = ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')
        if (type(identity) is not dict or set(identity) != set(fields) or
            any(type(v) is not str or not v or len(v)>256 for v in (invocation_id,tool_name,tool_call_id))):
            raise ValueError('policy invocation unavailable')
        key = self._managed_turn_key(identity)
        async with self._managed_execution_lock:
            entry = self._managed_actor_turns.get(key)
            calls = entry.get('policyInvocations') if isinstance(entry,dict) else None
            call=calls.get(invocation_id) if isinstance(calls,dict) else None
            if (not isinstance(call,dict) or call.get('toolName')!=tool_name or call.get('toolCallID')!=tool_call_id
                or entry.get('state')!='active'): raise ValueError('policy invocation unavailable')
            revision = entry.get('policyRevision')
        current = await self.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
        binding = await self._handle_session_binding_read({'sessionID':current['rootSessionID']})
        async with self._managed_execution_lock:
            if (self._managed_actor_turns.get(key) is not entry or entry.get('state')!='active' or entry.get('policyRevision')!=revision
                or entry.get('policyInvocations',{}).get(invocation_id)!=call
                or binding.get('canonicalCwd')!=call.get('canonicalCwd') or binding.get('authorityWorkspaceID')!=call.get('authorityWorkspaceID')
                or binding.get('authorityWorkspaceID')!=entry.get('authorityWorkspaceID') or current.get('holderID')!=call.get('holderID')
                or current.get('holderID')!=entry.get('holderID') or current.get('authority') or binding.get('authority')
                or any(binding.get(field)!=entry.get(field) for field in ('stableChatID','owner','mappingRevision','workspaceRevision'))
                or entry.get('mapRevision',-1)>binding.get('mapRevision',-1)):
                raise ValueError('policy invocation changed')
            return {**identity,'owner':entry.get('owner'),'ownerAccountID':entry.get('ownerAccountID'),
                'stableChatID':entry.get('stableChatID'),'authorityWorkspaceID':entry.get('authorityWorkspaceID'),
                'mappingRevision':entry.get('mappingRevision'),'workspaceRevision':entry.get('workspaceRevision'),
                'mapRevision':entry.get('mapRevision'),'policyRevision':revision,'canonicalCwd':binding.get('canonicalCwd'),
                'toolName':tool_name,'toolCallID':tool_call_id,'invocationID':invocation_id}

    async def resolve_managed_control_turn(self, identity: dict, tool_name: str = 'control_desktop') -> dict:
        """Prove the control tool call belongs to a live, admitted managed turn; returns its owner."""
        fields = ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')
        if type(identity) is not dict or set(identity) != set(fields) or type(identity['sessionID']) is not str:
            raise ValueError('control turn unavailable')
        key = self._managed_turn_key(identity)
        async with self._managed_execution_lock:
            entry = self._managed_actor_turns.get(key)
            if not isinstance(entry, dict) or entry.get('state') != 'active': raise ValueError('control turn unavailable')
        decision = await self._handle_managed_tool_admit({**identity, 'toolName':tool_name, 'kind':'tool'})
        async with self._managed_execution_lock:
            if decision.get('allowed') is not True or self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active':
                raise ValueError('control turn unavailable')
            return {'owner':entry.get('owner'), 'ownerAccountID':entry.get('ownerAccountID')}

    async def resolve_managed_recall_turn(self, identity: dict) -> dict:
        """Resolve an independently authorized live actor recall, without feedback authority."""
        trace=logger.isEnabledFor(logging.DEBUG)
        started=time.monotonic() if trace else None
        phase=['validation',started]; timings={}; outcome='refused'
        def mark(name):
            if trace:
                now=time.monotonic(); timings[phase[0]]=round((now-phase[1])*1000,3); phase[:]=[name,now]
        try:
            fields = ("sessionID", "rootOperationID", "actorID", "executionRevision", "innerTurnID")
            if type(identity) is not dict or set(identity) != set(fields):
                raise ValueError('recall turn unavailable')
            mark('entry')
            key = self._managed_turn_key(identity)
            async with self._managed_execution_lock:
                initial = self._managed_actor_turns.get(key)
                if not isinstance(initial, dict) or initial.get('state') != 'active':
                    raise ValueError('recall turn unavailable')
                revision = initial.get('policyRevision')
            mark('tool_policy')
            decision = await self._handle_managed_tool_admit({**identity, 'toolName':'recall_memory', 'kind':'memory_read'})
            if decision.get('allowed') is not True:
                raise ValueError('recall turn unavailable')
            mark('final')
            async with self._managed_execution_lock:
                entry = self._managed_actor_turns.get(key)
                if entry is not initial or entry.get('state') != 'active' or entry.get('policyRevision') != revision:
                    raise ValueError('recall turn unavailable')
                outcome='allowed'
                return {**identity, 'owner':entry.get('owner'), 'stableChatID':entry.get('stableChatID'),
                    'authorityWorkspaceID':entry.get('authorityWorkspaceID'), 'mappingRevision':entry.get('mappingRevision'),
                    'workspaceRevision':entry.get('workspaceRevision'), 'mapRevision':entry.get('mapRevision'),
                    'policyRevision':entry.get('policyRevision'), 'ownerAccountID':entry.get('ownerAccountID')}

        except asyncio.CancelledError:
            outcome='cancelled'
            raise
        except Exception as exc:
            outcome='timeout' if isinstance(exc,TimeoutError) else 'valueerror' if isinstance(exc,ValueError) else 'other'
            raise
        finally:
            if trace and outcome != 'allowed':
                failed_phase=phase[0]; mark('done')
                logger.debug('S07_RECALL_TURN outcome=%s phase=%s total_ms=%s phases_ms=%s',outcome,failed_phase,round((time.monotonic()-started)*1000,3),timings)

    async def _resolve_standing_guidance_turn(self, identity: dict) -> dict:
        """Private C2/binding snapshot; callers separately prove current memory read policy."""
        fields = ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')
        if type(identity) is not dict or set(identity) != set(fields) or identity['actorID'] != 'main':
            raise ValueError('guidance turn unavailable')
        key = self._managed_turn_key(identity)
        async with self._managed_execution_lock:
            entry = self._managed_actor_turns.get(key)
            if not isinstance(entry, dict) or entry.get('state') != 'active': raise ValueError('guidance turn unavailable')
            revision = entry.get('policyRevision')
        current = await self.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
        binding = await self._handle_session_binding_read({'sessionID':current['rootSessionID']})
        async with self._managed_execution_lock:
            if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                or entry.get('policyRevision') != revision or current.get('authority') or binding.get('authority')
                or any(binding.get(field) != entry.get(field) for field in ('stableChatID','owner','mappingRevision','workspaceRevision'))
                or entry.get('mapRevision',-1) > binding.get('mapRevision',-1)):
                raise ValueError('guidance turn changed')
            return {**identity, 'owner':entry.get('owner'), 'stableChatID':entry.get('stableChatID'),
                'authorityWorkspaceID':entry.get('authorityWorkspaceID'), 'mappingRevision':entry.get('mappingRevision'),
                'workspaceRevision':entry.get('workspaceRevision'), 'mapRevision':entry.get('mapRevision'),
                'policyRevision':entry.get('policyRevision'), 'ownerAccountID':entry.get('ownerAccountID')}

    async def resolve_managed_feedback_invocation(self, identity: dict, invocation_id: str) -> dict:
        """Resolve one host-minted recall admission against its still-live turn."""
        fields = ("sessionID", "rootOperationID", "actorID", "executionRevision", "innerTurnID")
        if (type(identity) is not dict or set(identity) != set(fields)
            or type(invocation_id) is not str or not invocation_id or len(invocation_id) > 256):
            raise ValueError('feedback invocation unavailable')
        key = self._managed_turn_key(identity)
        async with self._managed_execution_lock:
            entry = self._managed_actor_turns.get(key)
            calls = entry.get('feedbackInvocations') if entry else None
            call = calls.get(invocation_id) if isinstance(calls, dict) else None
            if (not isinstance(call, dict) or call.get('toolName') != 'recall_memory'
                or entry.get('state') != 'active' or entry.get('actorID') != 'main'):
                raise ValueError('feedback invocation unavailable')
            return {**{field: identity[field] for field in fields},
                'owner': entry.get('owner'), 'stableChatID': entry.get('stableChatID'),
                'authorityWorkspaceID': entry.get('authorityWorkspaceID'),
                'mappingRevision': entry.get('mappingRevision'), 'workspaceRevision': entry.get('workspaceRevision'),
                'mapRevision': entry.get('mapRevision'), 'policyRevision': entry.get('policyRevision'),
                'ownerAccountID': entry.get('ownerAccountID'),
                'toolCallID': call.get('toolCallID'), 'invocationID': invocation_id}

    async def issue_managed_recall_attestation(self, identity: dict, invocation_id: str,
                                                result_text: str, observations: tuple):
        """Bind host-formatted result bytes to already-written exact found events."""
        import hashlib
        import secrets
        from src.memery_external_sources import SourceRef
        from src.memory_provider import FoundObservation, RecallInvocation, MemoryRevisionRef
        resolved = await self.resolve_managed_feedback_invocation(identity, invocation_id)
        if (type(result_text) is not str or len(result_text.encode('utf-8')) > 1048576
                or type(observations) is not tuple or not 1 <= len(observations) <= 100):
            raise ValueError('recall attestation unavailable')
        for item in observations:
            if (type(item) is not FoundObservation or type(item.source) is not SourceRef
                or type(item.invocation) is not RecallInvocation or type(item.memory) is not MemoryRevisionRef
                or item.version != 1 or item.owner != item.memory.owner
                or item.source.chat_id != resolved['stableChatID']
                or item.source.message_id != identity['rootOperationID']
                or item.invocation.session_id != identity['sessionID']
                or item.invocation.root_operation_id != identity['rootOperationID']
                or item.invocation.actor_id != identity['actorID']
                or item.invocation.execution_revision != identity['executionRevision']
                or item.invocation.inner_turn_id != identity['innerTurnID']
                or item.invocation.recall_attempt_id != invocation_id):
                raise ValueError('recall attestation identity changed')
        digest = hashlib.sha256(result_text.encode('utf-8')).hexdigest()
        key = self._managed_turn_key(identity)
        async with self._managed_execution_lock:
            entry = self._managed_actor_turns.get(key)
            calls = entry.get('feedbackInvocations') if entry else None
            call = calls.get(invocation_id) if isinstance(calls, dict) else None
            if (not isinstance(call, dict) or entry.get('state') != 'active'
                    or call.get('toolCallID') != resolved['toolCallID']):
                raise ValueError('recall attestation unavailable')
            prior = call.get('admissionAttestation')
            if prior is not None:
                if prior['resultSHA256'] != digest or prior['observations'] != observations:
                    raise ValueError('recall attestation conflict')
            else:
                prior = {'attestationID': secrets.token_urlsafe(24), 'resultSHA256': digest,
                         'observations': observations}
                call['admissionAttestation'] = prior
            return {'attestationID': prior['attestationID'], 'resultSHA256': digest}

    async def _handle_managed_maintenance_stage(self, params: dict) -> dict:
        """Offer an exact content-free pending reference; never publish here."""
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import validate_managed_method_request
        try:
            request = validate_managed_method_request('_openclank/session/v1/maintenance/stage',params)
            fields = ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')
            identity = {field:request[field] for field in fields}
            manager = get_session_manager_instance()
            state = getattr(manager,'lifecycle_context',None) if manager is not None else None
            callback = getattr(state,'maintenance_stage_callback',None) if state is not None else None
            if not callable(callback): return {'accepted':False}
            proof, summary = await self.resolve_managed_maintenance_summary(identity,request['summaryMessageID'],tuple(request['summaryPartIDs']))
            # Only a source-classified built-in Responses instruction can
            # contribute to complete source coverage.
            if proof.get('wireProfile') != 'openai-chat':
                if proof.get('wireProfile') != 'codex-responses' or proof.get('instructionsClass') != 'builtin-compaction-v1':
                    return {'accepted':False}
                root = Path(__file__).resolve().parents[2]
                instruction_path = root / 'packages/mimo-code/packages/cli/src/agent/prompt/compaction.txt'
                if not instruction_path.is_file():
                    # A portable build ships the exact prompt without the engine build tree.
                    instruction_path = root / 'resources/engine/compaction.txt'
                try:
                    instruction_bytes = instruction_path.read_bytes()
                    if len(instruction_bytes) > 65536 or hashlib.sha256(instruction_bytes).hexdigest() != proof.get('instructionsSHA256'):
                        return {'accepted':False}
                except OSError:
                    return {'accepted':False}
            descriptor = {'sessionID':summary['sessionID'],'summaryMessageID':summary['summaryMessageID'],
                'providerID':summary['providerID'],'modelID':summary['modelID'],
                'parts':[{'partID':item['partID'],'sha256':item['sha256']} for item in summary['parts']]}
            return {'accepted': (await callback(self,identity,proof,descriptor)) is True}
        except asyncio.CancelledError:
            raise
        except Exception:
            return {'accepted':False}

    async def _handle_managed_maintenance_ack(self, params: dict) -> dict:
        """Retain only the first verified native ACK in this active turn."""
        from src import secret_storage
        from src.memery_external_sources import SourceRef, _source_ref_error
        from src.openclank.managed_protocol import validate_managed_method_request
        method = '_openclank/session/v1/maintenance/ack'
        try:
            request = validate_managed_method_request(method, params)
            fields = ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')
            identity = {field:request[field] for field in fields}
            original = await self.resolve_managed_recall_turn(identity)
            envelope = json.loads(request['attestation'])
            if (type(envelope) is not dict or set(envelope) != {'payload','mac'} or type(envelope['payload']) is not dict
                    or type(envelope['mac']) is not str or not secret_storage._KEY_PATH.is_file()):
                raise ValueError('maintenance attestation unavailable')
            payload = envelope['payload']
            if set(payload) == {'version','source','identity','messageID','partID','textSHA256','position'} and type(payload['version']) is int and payload['version'] == 1 and type(payload['position']) is int and payload['position'] == 0:
                domain = 'openclank.c2.maintenance-source.v1'
            elif (set(payload) == {'version','source','identity','messageID','partID','textSHA256','sourcePosition','layoutSHA256'}
                    and type(payload['version']) is int and payload['version'] == 2
                    and type(payload['sourcePosition']) is int and 0 <= payload['sourcePosition'] < 128
                    and type(payload['layoutSHA256']) is str and re.fullmatch(r'[0-9a-f]{64}',payload['layoutSHA256'])):
                domain = 'openclank.c2.maintenance-source.v2'
            else:
                raise ValueError('maintenance attestation malformed')
            encoded = json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            expected = secret_storage.keyed_digest(encoded,context=domain)
            source = SourceRef(**payload['source'])
            old_identity = payload['identity']
            if (not hmac.compare_digest(envelope['mac'],expected) or _source_ref_error(source)
                    or source.account_id != original.get('ownerAccountID') or source.chat_id != original.get('stableChatID')
                    or type(old_identity) is not dict or old_identity.get('actorID') != 'main'
                    or old_identity.get('rootOperationID') != source.message_id
                    or payload['messageID'] != request['sourceMessageID'] or payload['partID'] != request['sourcePartID']
                    or payload['textSHA256'] != request['sourceTextSHA256']
                    or re.fullmatch(r'[0-9a-f]{64}',payload['textSHA256']) is None):
                raise ValueError('maintenance attestation changed')
            proof = {'summaryMessageID':request['summaryMessageID'], 'source':payload['source'],
                     'sourceAttestationSHA256':hashlib.sha256(request['attestation'].encode()).hexdigest(),
                     'sourceMessageID':payload['messageID'], 'sourcePartID':payload['partID'],
                     'sourceTextSHA256':payload['textSHA256'], 'dispatchID':request['dispatchID'],
                     'bodySHA256':request['bodySHA256'], 'promptSHA256':request['promptSHA256'],
                     'wireProfile':request['wireProfile'], **({'instructionsSHA256':request['instructionsSHA256']} if 'instructionsSHA256' in request else {}),
                     **({'instructionsClass':request['instructionsClass']} if 'instructionsClass' in request else {})}
            key = self._managed_turn_key(identity)
            async with self._managed_execution_lock:
                entry = self._managed_actor_turns.get(key)
                if not isinstance(entry,dict) or entry.get('state') != 'active' or entry.get('ownerAccountID') != source.account_id:
                    raise ValueError('maintenance turn changed')
                retained = entry.setdefault('maintenanceAcks',{})
                if type(retained) is not dict or (request['summaryMessageID'] not in retained and len(retained) >= 16):
                    raise ValueError('maintenance ACK capacity unavailable')
                previous = retained.get(request['summaryMessageID'])
                if previous is not None and previous != proof: raise ValueError('maintenance ACK conflict')
                retained[request['summaryMessageID']] = proof
            return {'accepted':True}
        except asyncio.CancelledError:
            raise
        except Exception:
            return {'accepted':False}

    async def resolve_managed_maintenance_summary(self, identity: dict, summary_id: str, part_ids: tuple):
        """Read only the exact summary named by this active turn's retained ACK."""
        from src.openclank.managed_protocol import validate_managed_method_result
        if (type(identity) is not dict or identity.get('actorID') != 'main' or type(summary_id) is not str
                or type(part_ids) is not tuple or not 1 <= len(part_ids) <= 16
                or any(type(value) is not str or not value or len(value)>256 for value in part_ids)
                or len(set(part_ids)) != len(part_ids)):
            raise ValueError('maintenance summary unavailable')
        original = await self.resolve_managed_recall_turn(identity)
        key = self._managed_turn_key(identity)
        async with self._managed_execution_lock:
            entry = self._managed_actor_turns.get(key)
            acks = entry.get('maintenanceAcks') if isinstance(entry,dict) else None
            proof = copy.deepcopy(acks.get(summary_id)) if isinstance(acks,dict) else None
            if not proof: raise ValueError('maintenance ACK unavailable')
        current = await self.resolve_managed_actor_execution(identity['sessionID'], identity['rootOperationID'],
            identity['actorID'], identity['executionRevision'])
        root = current.get('rootSessionID')
        if root != identity['sessionID']: raise ValueError('maintenance root unavailable')
        binding = await self._handle_session_binding_read({'sessionID':root})
        worker = getattr(self._managed_actor_execution_reader,'__self__',None)
        provider_context = getattr(self._managed_provider_context,'__self__',None)
        if (worker is None or getattr(worker,'_bridge',None) is not self or not self._managed_provider_generation
                or getattr(provider_context,'holder_id',None) != self._managed_provider_generation):
            raise ValueError('maintenance worker unavailable')
        request = {'directory':binding['canonicalCwd'],'rootSessionID':root,'sessionID':identity['sessionID'],'summaryMessageID':summary_id}
        value = await worker.managed_engine_call('_openclank/session/v1/maintenance/summary-read',request)
        value = validate_managed_method_result('_openclank/session/v1/maintenance/summary-read',value)
        if (value.get('state') != 'ready' or value.get('sessionID') != identity['sessionID']
                or value.get('summaryMessageID') != summary_id or tuple(p['partID'] for p in value['parts']) != part_ids
                or sum(len(p['text'].encode('utf-8')) for p in value['parts']) > 65536
                or any(hashlib.sha256(p['text'].encode('utf-8')).hexdigest() != p['sha256'] for p in value['parts'])
                or await self.resolve_managed_recall_turn(identity) != original
                or await self._handle_session_binding_read({'sessionID':root}) != binding):
            raise ValueError('maintenance summary changed')
        async with self._managed_execution_lock:
            if self._managed_actor_turns.get(key) is not entry or entry.get('maintenanceAcks',{}).get(summary_id) != proof:
                raise ValueError('maintenance ACK changed')
        return proof, value

    async def resolve_ready_maintenance_summary(self, ready):
        """Resolve only the exact host-authorized, origin-ready native artifact.

        The caller retains the source guard and rechecks account, source and write
        authority. No current turn is inferred from this historical reference.
        """
        from src.openclank.conversation_archive import TrustedMaintenanceReady
        from src.openclank.managed_protocol import validate_managed_method_result
        if type(ready) is not TrustedMaintenanceReady:
            raise ValueError('maintenance reference unavailable')
        payload = ready.payload()
        if set(payload) != {'version','identity','proof','descriptor','binding'} or payload['version'] != 'maintenance.pending.v1':
            raise ValueError('maintenance reference unavailable')
        identity, proof, descriptor, frozen = (payload[k] for k in ('identity','proof','descriptor','binding'))
        keys = ('engineSessionID','stableChatID','owner','canonicalCwd','authorityWorkspaceID',
                'memoryWorkspaceID','copalWorkspace','mappingRevision')
        if (type(identity) is not dict or type(proof) is not dict or type(descriptor) is not dict or
                type(frozen) is not dict or set(frozen) != set(keys) or identity.get('actorID') != 'main' or
                descriptor.get('sessionID') != identity.get('sessionID') or
                frozen.get('engineSessionID') != identity.get('sessionID') or
                descriptor.get('summaryMessageID') != proof.get('summaryMessageID') or
                type(descriptor.get('parts')) is not list or not 1 <= len(descriptor['parts']) <= 16):
            raise ValueError('maintenance reference unavailable')
        parts = descriptor['parts']
        if (any(type(p) is not dict or set(p) != {'partID','sha256'} or type(p['partID']) is not str or
                not p['partID'] or len(p['partID']) > 256 or type(p['sha256']) is not str or
                re.fullmatch(r'[0-9a-f]{64}',p['sha256']) is None for p in parts) or
                len({p['partID'] for p in parts}) != len(parts)):
            raise ValueError('maintenance reference unavailable')
        root = identity['sessionID']
        binding = await self._handle_session_binding_read({'sessionID':root})
        worker = getattr(self._managed_actor_execution_reader,'__self__',None)
        provider_context = getattr(self._managed_provider_context,'__self__',None)
        holder = self._managed_provider_generation
        if (any(binding.get(k) != frozen[k] for k in keys) or worker is None or
                getattr(worker,'_bridge',None) is not self or not holder or
                getattr(provider_context,'holder_id',None) != holder):
            raise ValueError('maintenance binding unavailable')
        request = {'directory':binding['canonicalCwd'],'rootSessionID':root,'sessionID':root,
                   'summaryMessageID':descriptor['summaryMessageID']}
        value = await worker.managed_engine_call('_openclank/session/v1/maintenance/summary-read',request)
        value = validate_managed_method_result('_openclank/session/v1/maintenance/summary-read',value)
        current = await self._handle_session_binding_read({'sessionID':root})
        if (ready.payload() != payload or self._managed_provider_generation != holder or
                getattr(provider_context,'holder_id',None) != holder or
                any(current.get(k) != frozen[k] for k in keys) or value.get('state') != 'ready' or
                value.get('sessionID') != root or value.get('summaryMessageID') != descriptor['summaryMessageID'] or
                value.get('providerID') != descriptor.get('providerID') or value.get('modelID') != descriptor.get('modelID') or
                [{'partID':p['partID'],'sha256':p['sha256']} for p in value['parts']] != parts or
                sum(len(p['text'].encode('utf-8')) for p in value['parts']) > 65536 or
                any(hashlib.sha256(p['text'].encode('utf-8')).hexdigest() != p['sha256'] for p in value['parts'])):
            raise ValueError('maintenance summary changed')
        return proof, value

    async def _handle_queued_source_forward(self, params: dict) -> dict:
        """Retain exact consume-transaction IDs as a non-authorizing candidate."""
        from src.openclank.managed_protocol import validate_managed_method_request
        from core.models import get_session_manager_instance
        method='_openclank/session/v1/queued-source/forward'
        try:
            request=validate_managed_method_request(method,params)
            identity={k:request[k] for k in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
            key=self._managed_turn_key(identity)
            manager=get_session_manager_instance();state=getattr(manager,'lifecycle_context',None) if manager else None
            supervisor=getattr(state,'mimo_supervisor',None);owner=self._owner
            pool=supervisor.owner_lifecycle_pool() if supervisor is not None else None
            worker=pool.worker_for_owner(owner) if pool is not None and owner else None
            holder=self._managed_provider_generation;generation=getattr(worker,'installed_generation',None)
            if (pool is None or worker is None or not worker.is_alive() or getattr(worker,'_bridge',None) is not self
                or not isinstance(holder,str) or not holder or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder
                or type(generation) is not int or generation<0):return {'accepted':False}
            epoch=pool._owner_lifecycle_epoch(owner)
            if type(epoch) is not int or epoch<0:return {'accepted':False}
            producer={'version':1,'holderSHA256':hashlib.sha256(holder.encode('utf-8')).hexdigest(),'generation':generation,'ownerEpoch':epoch}
            def producer_current():
                return (supervisor.owner_lifecycle_pool() is pool and pool.worker_for_owner(owner) is worker and worker.is_alive() and getattr(worker,'_bridge',None) is self
                    and self._managed_provider_generation==holder and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)==holder
                    and getattr(worker,'installed_generation',None)==generation and pool._owner_lifecycle_epoch(owner)==epoch)
            async with self._managed_execution_lock:
                entry=self._managed_actor_turns.get(key)
                if not isinstance(entry,dict) or entry.get('state') != 'active' or entry.get('owner')!=owner or not producer_current(): return {'accepted':False}
                ingress=copy.deepcopy(entry.get('queuedSourceIngress') or {})
                account=entry.get('ownerAccountID');policy=entry.get('policyRevision')
            current=await self.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
            auth=getattr(state,'auth_manager',None)
            if (not account or auth is None or current['owner']!=owner or auth.account_id(current['owner']) != account or
                auth.username_for_account_id(account) != current['owner'] or current.get('rootSessionID') != identity['sessionID']):
                return {'accepted':False}
            references={}
            for item in request['sources']:
                candidate=ingress.get(item['requestID'])
                if (not isinstance(candidate,dict) or item['requestID'] in references or
                    candidate.get('payloadHash') != item['payloadHash'] or candidate.get('proofSHA256') != item['proofSHA256'] or
                    candidate.get('proof',{}).get('bodySHA256') != item['bodySHA256']): return {'accepted':False}
                references[item['requestID']]={**candidate,'messageID':item['messageID'],'partID':item['partID']}
            async with self._managed_execution_lock:
                if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active' or
                    entry.get('policyRevision') != policy or entry.get('ownerAccountID') != account or
                    entry.get('queuedSourceIngress') != ingress or auth.account_id(current['owner']) != account or
                    auth.username_for_account_id(account) != current['owner'] or not producer_current()): return {'accepted':False}
                previous=entry.get('queuedSourceForwarded')
                if previous is not None and previous != references: return {'accepted':False}
                if entry.get('queuedSourceProducer') not in (None,producer):return {'accepted':False}
                if entry.get('queuedSourceClosing'): return {'accepted':False}
                entry['queuedSourceForwarded']=references
                entry['queuedSourceProducer']=producer
                callback=getattr(state,'queued_source_stage_callback',None) if state is not None else None
                if callable(callback) and entry.get('queuedSourceStageTask') is None:
                    task=asyncio.create_task(callback(self,dict(identity),entry,copy.deepcopy(references),dict(producer)))
                    entry['queuedSourceStageTask']=task
                    def consume(done):
                        try: done.exception()
                        except (Exception,asyncio.CancelledError): pass
                    task.add_done_callback(consume)
            return {'accepted':True}
        except Exception:
            return {'accepted':False}

    async def _handle_generated_attachment_source(self, params: dict) -> dict:
        """Recheck one host-frozen generated attachment against the live turn."""
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method='_openclank/session/v1/attachment/source'
        try:
            request=validate_managed_method_request(method,params)
            identity={field:request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
            key=self._managed_turn_key(identity)
            async with self._managed_execution_lock:
                entry=self._managed_actor_turns.get(key)
                if not isinstance(entry,dict) or entry.get('state')!='active' or identity['actorID']!='main': return {'state':'unavailable'}
                matches=[item for item in (entry.get('generatedSummaryBoundary') or ()) if item.get('uploadID')==request['uploadID']
                    and item.get('originSHA256')==request['originSHA256'] and item.get('textSHA256')==request['textSHA256']]
                if len(matches)!=1: return {'state':'unavailable'}
                frozen=copy.deepcopy(matches[0]); owner=entry.get('owner'); account=entry.get('ownerAccountID'); policy=entry.get('policyRevision')
            resolved=await self.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
            binding=await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']})
            manager=get_session_manager_instance(); state=getattr(manager,'lifecycle_context',None) if manager is not None else None
            auth=getattr(state,'auth_manager',None); supervisor=getattr(state,'mimo_supervisor',None)
            pool = supervisor.owner_lifecycle_pool() if supervisor is not None else None
            worker=pool.worker_for_owner(owner) if pool is not None else None; holder=self._managed_provider_generation
            if (not account or auth is None or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
                or worker is None or not worker.is_alive() or getattr(worker,'_bridge',None) is not self or not holder
                or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder
                or binding.get('owner')!=owner or binding.get('stableChatID')!=entry.get('stableChatID')
                or any(binding.get(field)!=entry.get(field) for field in ('authorityWorkspaceID','mappingRevision','workspaceRevision','mapRevision'))): return {'state':'unavailable'}
            from src.tool_utils import get_upload_handler
            from src.openclank.generated_summary_attachment import verify_upload, _encoded
            handler=get_upload_handler()
            if handler is None: return {'state':'unavailable'}
            def inspect():
                info=handler.resolve_upload(request['uploadID'],owner=owner,allow_admin=False)
                if not isinstance(info,dict) or _encoded(info.get('generated_summary_origin')).decode()!=frozen['envelope']: return None
                origin=verify_upload(info,handler=handler,owner=owner,account_id=account,chat_id=entry.get('stableChatID'))
                if origin is None: return None
                with open(info['path'],'rb') as source: value=source.read(262145)
                if (len(value)>262144 or hashlib.sha256(value).hexdigest()!=origin['output_sha256']
                    or hashlib.sha256(b'[Model-generated interpretation; untrusted]\n'+value).hexdigest()!=request['textSHA256']): return None
                return origin
            async with asyncio.timeout(0.2): origin=await asyncio.to_thread(inspect)
            if not origin: return {'state':'unavailable'}
            fresh=await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']})
            if fresh!=binding or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner: return {'state':'unavailable'}
            async with self._managed_execution_lock:
                if (self._managed_actor_turns.get(key) is not entry or entry.get('state')!='active' or entry.get('policyRevision')!=policy
                    or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker
                    or self._managed_provider_generation!=holder or getattr(worker,'_bridge',None) is not self or not worker.is_alive()
                    or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder
                    or frozen not in (entry.get('generatedSummaryBoundary') or ())): return {'state':'unavailable'}
            result={'state':'ready','messageID':request['messageID'],'partID':request['partID'],'textSHA256':request['textSHA256'],'uploadID':request['uploadID'],
                    'artifactID':origin['artifact_id'],'artifactSHA256':origin['artifact_sha256'],
                    'outputSHA256':origin['output_sha256'],'previewHash':origin['preview_hash'],
                    'sourceReceiptSHA256':hashlib.sha256(_encoded(origin['receipt'])).hexdigest()}
            return validate_managed_method_result(method,result)
        except asyncio.CancelledError: raise
        except Exception: return {'state':'unavailable'}

    async def _handle_managed_maintenance_source(self, params: dict) -> dict:
        """Bind one post-plugin native part to a freshly verified host source."""
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/maintenance/source'
        finalizer = None
        trace=logger.isEnabledFor(logging.DEBUG)
        started=time.monotonic() if trace else None
        phase=['validation',started]; timings={}; outcome='refused'
        def mark(name):
            if trace:
                now=time.monotonic(); timings[phase[0]]=round((now-phase[1])*1000,3); phase[:]=[name,now]
        try:
            request = validate_managed_method_request(method, params)
            fields = ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')
            identity = {field:request[field] for field in fields}
            mark('entry_recall')
            original = await self.resolve_managed_recall_turn(identity)
            mark('boundary')
            key = self._managed_turn_key(identity)
            async with self._managed_execution_lock:
                entry = self._managed_actor_turns.get(key)
                boundary = entry.get('maintenancePromptBoundary') if isinstance(entry, dict) else None
                if not isinstance(boundary, dict) or boundary.get('coverage') != 'candidate' or not entry.get('ownerAccountID'):
                    raise ValueError('maintenance source boundary unavailable')
                version = request.get('version', 1)
                parts = list(boundary.get('parts', ()))
                if version == 1:
                    if len(parts) != 1 or parts[0].get('sha256') != request['textSHA256']:
                        raise ValueError('maintenance source boundary unavailable')
                elif type(version) is int and version == 2:
                    position = request.get('sourcePosition')
                    layout = request.get('layout')
                    _maintenance_layout_digest(layout, position)
                    if (layout != parts or position != boundary.get('sourcePosition')
                            or layout[position]['sha256'] != request['textSHA256']):
                        raise ValueError('maintenance source boundary unavailable')
                else:
                    raise ValueError('maintenance source boundary unavailable')
                frozen = copy.deepcopy(boundary)
            manager = get_session_manager_instance()
            state = getattr(manager, 'lifecycle_context', None) if manager is not None else None
            callback = getattr(state, 'maintenance_source_callback', None) if state is not None else None
            if not callable(callback): raise ValueError('maintenance source unavailable')
            mark('issuer')
            async with asyncio.timeout(0.2) as deadline:
                finalizer = _MaintenanceFinalizer(self, identity, original, frozen, request, deadline.when())
                token = _maintenance_finalizer_context.set(finalizer)
                try:
                    attestation = await callback(self, identity, original, frozen, request)
                finally:
                    _maintenance_finalizer_context.reset(token)
                mark('final_recall')
                if type(attestation) is not str or not attestation or len(attestation) > 8192 or not finalizer.current():
                    raise ValueError('maintenance source changed')
                if finalizer.claimed:
                    if not finalizer.checked:
                        raise ValueError('maintenance finalizer unavailable')
                elif await self.resolve_managed_recall_turn(identity) != original or not finalizer.current():
                    raise ValueError('maintenance source changed')
                mark('commit')
                async with self._managed_execution_lock:
                    if (not finalizer.current() or self._managed_actor_turns.get(key) is not entry
                            or entry.get('maintenancePromptBoundary') != frozen):
                        raise ValueError('maintenance turn changed')
                    # Retain the exact original issuer result, not a later
                    # reconstruction from native part metadata or text.
                    candidate = {'identity': dict(identity), 'attestation': attestation,
                                 'messageID': request['messageID'], 'partID': request['partID'],
                                 'textSHA256': request['textSHA256']}
                    if version == 2:
                        candidate.update({'layout':copy.deepcopy(layout),'sourcePosition':position,
                                          'layoutSHA256':_maintenance_layout_digest(layout,position)})
                    prior = entry.get('forwardPairInput')
                    if prior is None:
                        entry['forwardPairInput'] = candidate
                    elif prior != candidate:
                        entry['forwardPairInput'] = False
                result=validate_managed_method_result(method, {'state':'ready','attestation':attestation})
                outcome='allowed'
                return result
        except asyncio.CancelledError:
            outcome='cancelled'
            raise
        except Exception as exc:
            outcome='timeout' if isinstance(exc,TimeoutError) else 'valueerror' if isinstance(exc,ValueError) else 'other'
            return {'state':'unavailable'}
        finally:
            if finalizer is not None:
                finalizer.active = False
            if trace and outcome != 'allowed':
                failed_phase=phase[0]; mark('done')
                logger.debug('S14_MAINTENANCE_SOURCE outcome=%s phase=%s total_ms=%s phases_ms=%s',outcome,failed_phase,round((time.monotonic()-started)*1000,3),timings)

    async def _retire_python_kernels(self, session_id=None, chat_id=None):
        from core.models import get_session_manager_instance
        manager = get_session_manager_instance()
        state = getattr(manager, 'lifecycle_context', None) if manager is not None else None
        kernels = getattr(state, 'python_kernel_manager', None)
        async with self._managed_execution_lock:
            if session_id is None: self._kernel_retired = True
            else: self._kernel_retired_sessions.add(session_id)
            if chat_id: self._kernel_retired_chats.add(chat_id)
            scopes = [scope for scope in self._kernel_scopes if session_id is None or scope.engine_session_id == session_id or (chat_id and scope.chat_id == chat_id)]
            self._kernel_scopes.difference_update(scopes)
        if kernels is None and scopes: raise ValueError('kernel retirement unavailable')
        for scope in scopes:
            task = asyncio.create_task(asyncio.to_thread(kernels.retire_scope, scope))
            try: await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    @staticmethod
    async def _drain_python_kernel_call(kernels, reservation):
        task = asyncio.create_task(asyncio.to_thread(kernels.drain_call, reservation))
        cancelled = False
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: cancelled = True
        task.result()
        if cancelled: raise asyncio.CancelledError

    def _python_kernel_available(self):
        try:
            from core.models import get_session_manager_instance
            from src.openclank.python_kernel import KernelManager
            owner_manager = get_session_manager_instance()
            state = getattr(owner_manager, 'lifecycle_context', None) if owner_manager is not None else None
            kernels = getattr(state, 'python_kernel_manager', None)
            supervisor = getattr(state, 'mimo_supervisor', None)
            pool = supervisor.owner_lifecycle_pool() if supervisor is not None else None
            owner_worker = pool.worker_for_owner(self._owner) if pool is not None else None
            worker = Path(__file__).with_name('python_kernel_worker.py')
            client = getattr(self, '_client', None)
            return bool(sys.platform.startswith('linux') and os.name == 'posix' and not getattr(sys, 'frozen', False)
                and sys.implementation.name == 'cpython' and re.fullmatch(r'python(?:3(?:\.\d+)?)?',Path(sys.executable).name)
                and Path(sys.executable).is_file() and os.access(sys.executable, os.X_OK) and worker.is_file()
                and isinstance(kernels, KernelManager) and not kernels._closed and client is not None
                and not client.is_closed and self._managed_provider_generation and not self._kernel_retired
                and owner_worker is not None and owner_worker.is_alive() and getattr(owner_worker, '_bridge', None) is self
                and getattr(getattr(owner_worker, '_managed_callbacks', None), 'holder_id', None) == self._managed_provider_generation
                and client._callbacks.get('_openclank/session/v1/python-kernel/call') == self._handle_managed_python_kernel_call)
        except Exception:
            return False

    async def _handle_code_activity_observe(self, params):
        """Project one opt-in private code-read return, without source text."""
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/code-activity/observe'
        request = validate_managed_method_request(method, params)
        identity = {field:request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
        key = self._managed_turn_key(identity); unavailable = {'state':'unavailable'}
        try:
            async with self._managed_execution_lock:
                entry = self._managed_actor_turns.get(key)
                if (not isinstance(entry,dict) or entry.get('state')!='active' or entry.get('codeActivityEnabled') is not True
                    or request['toolCallID'] not in entry.get('codeActivityAdmitted',{})): return unavailable
                owner=entry.get('owner'); account=entry.get('ownerAccountID'); policy=entry.get('policyRevision')
            resolved=await self.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
            binding=await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']})
            manager=get_session_manager_instance(); state=getattr(manager,'lifecycle_context',None) if manager is not None else None
            auth=getattr(state,'auth_manager',None); supervisor=getattr(state,'mimo_supervisor',None)
            pool = supervisor.owner_lifecycle_pool() if supervisor is not None else None
            worker=pool.worker_for_owner(owner) if supervisor is not None else None
            holder=self._managed_provider_generation
            if (not account or auth is None or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
                or worker is None or getattr(worker,'_bridge',None) is not self or not worker.is_alive() or not holder
                or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder
                or binding.get('owner')!=owner or binding.get('stableChatID')!=entry.get('stableChatID')
                or binding.get('authorityWorkspaceID')!=entry.get('authorityWorkspaceID')
                or (request['resource'] is not None and request['resource']['receipt']['workspace_id']!=binding['authorityWorkspaceID'])):
                return unavailable
            decision=await self._handle_managed_tool_admit({**identity,'toolName':'search_code','kind':'tool','toolCallID':request['toolCallID']})
            if decision.get('allowed') is not True: return unavailable
            fresh=await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']})
            if fresh!=binding or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner:
                return unavailable
            async with self._managed_execution_lock:
                if (self._managed_actor_turns.get(key) is not entry or entry.get('state')!='active'
                    or entry.get('policyRevision')!=policy or entry.get('codeActivityEnabled') is not True
                    or request['toolCallID'] not in entry.get('codeActivityAdmitted',{})
                    or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker
                    or self._managed_provider_generation!=holder or getattr(worker,'_bridge',None) is not self
                    or not worker.is_alive() or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder
                    or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner): return unavailable
                events=entry.setdefault('codeActivityEvents',{})
                previous=events.get(request['toolCallID'])
                if previous is not None:
                    return previous if previous.get('resultSHA256')==request['resultSHA256'] and previous.get('resource')==request['resource'] else unavailable
                if len(events)>=128: return unavailable
                event={'version':1,'accountID':account,'workspaceID':binding['authorityWorkspaceID'],'hostChatID':binding['stableChatID'],
                    'engineSessionID':identity['sessionID'],'actorID':identity['actorID'],'rootOperationID':identity['rootOperationID'],
                    'executionRevision':identity['executionRevision'],'turnID':identity['innerTurnID'],'toolCallID':request['toolCallID'],
                    'resultSHA256':request['resultSHA256'],'observedAt':int(time.time()*1000),'operation':'read',
                    'outcome':'observed','resource':request['resource']}
                event=validate_managed_method_result(method,event); events[request['toolCallID']]=event
                return event
        except Exception:
            return unavailable

    async def _handle_queued_source_fence(self, params):
        """Issue only the limited fence from the current authenticated holder."""
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/queued-source/fence'
        request = validate_managed_method_request(method, params)
        unavailable = {'state':'unavailable'}
        try:
            owner_manager = get_session_manager_instance()
            state = getattr(owner_manager, 'lifecycle_context', None) if owner_manager is not None else None
            auth = getattr(state, 'auth_manager', None); supervisor = getattr(state, 'mimo_supervisor', None)
            pool = supervisor.owner_lifecycle_pool() if supervisor is not None else None
            callback = getattr(state, 'queued_source_fence_callback', None)
            owner = self._owner; holder = self._managed_provider_generation
            reset_lease=None
            if supervisor is not None and request['mutation']=='holder_retire':
                from src.openclank.queued_source import QueuedSourceRef
                try:
                    reset_lease=pool.queued_reset_lease_for_source(owner,self,QueuedSourceRef(**request['source']))
                except Exception: return unavailable
            if not getattr(state, 'queued_source_mutation_ready', False) and reset_lease is None: return unavailable
            def authorized_worker():
                if supervisor is None or supervisor.owner_lifecycle_pool() is not pool: return None
                if reset_lease is not None:
                    return reset_lease.worker if pool.queued_reset_lease_for_source(owner,self,QueuedSourceRef(**request['source'])) is reset_lease else None
                if request['mutation'] == 'holder_retire':
                    return pool.queued_retirement_worker(owner, self)
                active = pool.worker_for_owner(owner)
                if active is not None and getattr(active, '_bridge', None) is self: return active
                return None
            worker = authorized_worker()
            def current():
                return bool(auth is not None and callable(callback) and owner and holder and worker is not None
                    and authorized_worker() is worker and getattr(worker, '_bridge', None) is self and worker.is_alive()
                    and getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) == holder
                    and self._managed_provider_generation == holder
                    and (reset_lease is not None or (auth.account_id(owner) == request['source']['account_id']
                    and auth.username_for_account_id(request['source']['account_id']) == owner)))
            if not current(): return unavailable
            if reset_lease is not None:
                await pool.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim)
            result = await callback(self, request['source'], request['preimage'], request['mutation'])
            if reset_lease is not None:
                await pool.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim)
            if not current() or result is None: return unavailable
            if result.get('source') != request['source'] or result.get('preimage') != request['preimage']:
                return unavailable
            return validate_managed_method_result(method, result)
        except Exception:
            return unavailable

    async def _handle_memory_inferred_proposal(self, params):
        from src.openclank.m1_inferred_proposal import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method='_openclank/session/v1/memory/inferred-proposal'
        request=validate_managed_method_request(method,params)
        return validate_managed_method_result(method,await dispatch(self,request,status=False))

    async def _handle_memory_inferred_status(self, params):
        from src.openclank.m1_inferred_proposal import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method='_openclank/session/v1/memory/inferred-proposal/status'
        request=validate_managed_method_request(method,params)
        return validate_managed_method_result(method,await dispatch(self,request,status=True))

    async def _handle_workflow_artifact(self, params):
        from src.openclank.workflow_artifacts import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/workflow/artifact'
        request = validate_managed_method_request(method, params)
        result = validate_managed_method_result(method, await dispatch(self, request))
        # Only the verified Host callback may associate this actual invocation
        # with a presentation selector. The selector is never a Files grant.
        if result.get('state') == 'complete':
            try:
                from src.openclank.transcript_projection import get_mimo_state
                identity={k:request[k] for k in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
                async with self._managed_execution_lock:
                    entry=self._managed_actor_turns.get(self._managed_turn_key(identity))
                    if isinstance(entry,dict) and entry.get('state') == 'active':
                        owner,account,chat=entry.get('owner'),entry.get('ownerAccountID'),entry.get('stableChatID')
                        plan=get_mimo_state(chat,owner=owner).get('plan_state')
                        association=plan.get('artifact_association') if isinstance(plan,dict) else None
                        role,name=request['role'],request['name']
                        # Task-progress names are private derived identities;
                        # they remain unavailable until that mapping is projected.
                        key=role if name is None else role+':'+name
                        member=(association.get('members') or {}).get(key) if isinstance(association,dict) else None
                        if (role != 'progress' and isinstance(member,dict) and member.get('pending_write') is None
                            and member.get('revision') == result.get('revision') and association.get('account_id') == account
                            and association.get('plan_revision') == plan.get('revision') and association.get('plan_digest') == plan.get('digest')):
                            selector={'version':1,'kind':'workflow-member','account':account,'chat':chat,**identity,
                                'toolCallID':request['toolCallID'],'operationID':request['operationID'],
                                'artifactSetID':association.get('artifact_set_id'),'planRevision':plan.get('revision'),
                                'planDigest':plan.get('digest'),'role':role,'name':name,'revision':result['revision']}
                            records=entry.setdefault('workflowResultEvents',{})
                            if isinstance(records,dict) and len(records)<128:
                                records[request['toolCallID']]={'selector':selector,'result':result}
            except (KeyError,TypeError,ValueError,RuntimeError):
                pass
        return result

    async def _handle_workflow_artifact_status(self, params):
        from src.openclank.workflow_artifacts import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/workflow/artifact/status'
        request = validate_managed_method_request(method, params)
        return validate_managed_method_result(method, await dispatch(self, request, status=True))

    async def _handle_workflow_artifact_read(self, params):
        from src.openclank.workflow_artifacts import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/workflow/artifact/read'
        request = validate_managed_method_request(method, params)
        return validate_managed_method_result(method, await dispatch(self, request, read=True))

    async def _handle_workflow_snapshot_read(self, params):
        from src.openclank.workflow_artifacts import dispatch_snapshot
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/workflow/snapshot/read'
        request = validate_managed_method_request(method, params)
        return validate_managed_method_result(method, await dispatch_snapshot(self, request))

    async def _handle_evolve_operate(self, params):
        from src.openclank.evolve_dispatch import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/evolve/operate'
        request = validate_managed_method_request(method, params)
        return validate_managed_method_result(method, await dispatch(self, request))

    async def _handle_git_index_recheck(self, params):
        from src.openclank.git_index_authority import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/git-index/recheck'
        request = validate_managed_method_request(method, params)
        return validate_managed_method_result(method, await dispatch(self, request, finish=False))

    async def _handle_git_index_finish(self, params):
        from src.openclank.git_index_authority import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/git-index/finish'
        request = validate_managed_method_request(method, params)
        return validate_managed_method_result(method, await dispatch(self, request, finish=True))

    async def _handle_git_commit_recheck(self, params):
        from src.openclank.git_index_authority import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/git-commit/recheck'
        request = validate_managed_method_request(method, params)
        return validate_managed_method_result(method, await dispatch(self, request, finish=False, profile='commit'))

    async def _handle_git_commit_finish(self, params):
        from src.openclank.git_index_authority import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/git-commit/finish'
        request = validate_managed_method_request(method, params)
        return validate_managed_method_result(method, await dispatch(self, request, finish=True, profile='commit'))

    async def _handle_git_worktree_stage_phase(self, params):
        from src.openclank.git_worktree_stage_authority import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/git-worktree/phase'
        request = validate_managed_method_request(method, params)
        return validate_managed_method_result(method, await dispatch(self, request))

    async def _handle_git_checkout_phase(self, params):
        from src.openclank.git_checkout_authority import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/git-checkout/phase'
        request = validate_managed_method_request(method, params)
        return validate_managed_method_result(method, await dispatch(self, request))

    async def _handle_git_operation_recheck(self, params):
        from src.openclank.git_index_authority import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/git-operation/recheck'
        request = validate_managed_method_request(method, params)
        return validate_managed_method_result(method, await dispatch(self, request, finish=False, profile='branch_create'))

    async def _handle_git_operation_finish(self, params):
        from src.openclank.git_index_authority import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/git-operation/finish'
        request = validate_managed_method_request(method, params)
        return validate_managed_method_result(method, await dispatch(self, request, finish=True, profile='branch_create'))

    async def _handle_git_fetch_recheck(self, params):
        from src.openclank.git_index_authority import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/git-fetch/recheck'
        request = validate_managed_method_request(method, params)
        return validate_managed_method_result(method, await dispatch(self, request, finish=False, profile='fetch'))

    async def _handle_git_fetch_finish(self, params):
        from src.openclank.git_index_authority import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/git-fetch/finish'
        request = validate_managed_method_request(method, params)
        return validate_managed_method_result(method, await dispatch(self, request, finish=True, profile='fetch'))

    async def _handle_git_push_recheck(self, params):
        from src.openclank.git_index_authority import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/git-push/recheck'
        request = validate_managed_method_request(method, params)
        return validate_managed_method_result(method, await dispatch(self, request, finish=False, profile='push'))

    async def _handle_git_push_finish(self, params):
        from src.openclank.git_index_authority import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/git-push/finish'
        request = validate_managed_method_request(method, params)
        return validate_managed_method_result(method, await dispatch(self, request, finish=True, profile='push'))

    async def _handle_git_cleanup_recheck(self, params):
        from src.openclank.git_index_authority import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/git-cleanup/recheck'
        request = validate_managed_method_request(method, params)
        return validate_managed_method_result(method, await dispatch(self, request, finish=False, profile='cleanup'))

    async def _handle_git_cleanup_finish(self, params):
        from src.openclank.git_index_authority import dispatch
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/git-cleanup/finish'
        request = validate_managed_method_request(method, params)
        return validate_managed_method_result(method, await dispatch(self, request, finish=True, profile='cleanup'))

    async def _handle_native_analysis_source(self, params):
        """Capture and reauthorize one source for an exact active native tool call."""
        import base64
        import secrets
        from types import SimpleNamespace
        from core.models import get_session_manager_instance
        from src.openclank.analysis_adapters.base import capture_source, source_current
        from src.openclank.files_facade import ProviderContext
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        from src.openclank.analysis_result_receipts import validate_result, seal, digest, legacy_producer_profile
        from src.openclank.analysis_input_digest import disassembly_input_sha, xref_input_sha, decompile_input_sha, instruction_xref_input_sha
        from src.openclank.analysis_projects import read_only_project
        from src.openclank.artifacts import account_artifact_owner
        method = '_openclank/session/v1/native-analysis/source'
        request = validate_managed_method_request(method, params)
        identity = {name:request[name] for name in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
        key = self._managed_turn_key(identity); unavailable = {'state':'unavailable'}
        try:
            async with self._managed_execution_lock:
                entry = self._managed_actor_turns.get(key)
                if not isinstance(entry,dict) or entry.get('state') != 'active': return unavailable
                owner, account, policy = entry.get('owner'), entry.get('ownerAccountID'), entry.get('policyRevision')
            decision = await self._handle_managed_tool_admit({**identity,'toolName':'bash','kind':'tool'})
            if decision.get('allowed') is not True or not isinstance(account,str) or not account: return unavailable
            resolved = await self.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
            binding = await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']})
            manager=get_session_manager_instance(); state=getattr(manager,'lifecycle_context',None) if manager else None
            auth=getattr(state,'auth_manager',None); barrier=getattr(state,'account_request_barrier',None)
            supervisor=getattr(state,'mimo_supervisor',None); repository=getattr(state,'files_policy_repository',None)
            factory=getattr(state,'files_facade_factory',None); holder=self._managed_provider_generation
            pool=supervisor.owner_lifecycle_pool() if supervisor else None
            owner_key=pool._key(owner) if pool else None
            runtime_state=pool._states.get(owner_key) if pool else None
            worker=pool.worker_for_owner(owner) if pool else None
            epoch=pool._owner_lifecycle_epoch(owner_key) if pool else None
            generation=getattr(runtime_state,'generation',None); process=getattr(worker,'_proc',None)
            files_generation=repository.generation() if repository else -1
            def current():
                try:
                    if not pool:return False
                    pool._assert_owner_lifecycle(owner_key,epoch)
                except Exception:return False
                return bool(auth and barrier and repository and callable(factory) and worker and holder and process
                    and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                    and pool.worker_for_owner(owner) is worker and pool._states.get(owner_key) is runtime_state
                    and runtime_state.active is worker and runtime_state.status=='ready' and runtime_state.generation==generation
                    and worker.installed_generation==generation and worker._proc is process
                    and getattr(worker,'_bridge',None) is self and worker.is_alive()
                    and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)==holder
                    and self._managed_provider_generation==holder and repository.generation()==files_generation and binding.get('owner')==owner
                    and binding.get('stableChatID')==entry.get('stableChatID')
                    and binding.get('authorityWorkspaceID')==entry.get('authorityWorkspaceID'))
            if not current(): return unavailable
            if request['operation']=='eligibility':
                if await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']}) != binding: return unavailable
                async with self._managed_execution_lock:
                    if self._managed_actor_turns.get(key) is not entry or entry.get('state')!='active' or entry.get('policyRevision')!=policy or not current(): return unavailable
                    eligible=identity['sessionID']==resolved['rootSessionID'] and identity['actorID']=='main'
                    return validate_managed_method_result(method,{'state':'eligible' if eligible else 'legacy'})
            context=ProviderContext(account,owner,files_generation,bool(auth.is_admin(owner)),binding['authorityWorkspaceID'])
            facade=factory(SimpleNamespace(app=SimpleNamespace(state=state)),context)
            async def function_witness(target, source_ref):
                project=read_only_project(facade=facade,context=context,repository=repository,auth_manager=auth,
                    owner=owner,account=account,workspace_id=binding['authorityWorkspaceID'],extra_current=current)
                if 'opcode' in target['selector']:
                    value=await project.inspect_provider_instruction(target['dossier_id'],target['expected_dossier_revision'],target['selector'],source_ref)
                    if value.get('sourceState')!='current':raise ValueError('source changed')
                    facts=value['resultIdentity']
                    return {'dossierID':target['dossier_id'],'dossierRevision':value['dossierRevision'],'instruction':value['instruction'],
                        'maxReferences':target['max_references'],'resultSHA256':facts['resultSHA256'],
                        '_sourceID':facts['sourceID'],'_sourceSHA':facts['sourceSHA256'],'_sourceRevision':facts['sourceRevision']}
                value=await project.inspect_provider_function(target['dossier_id'],target['expected_dossier_revision'],target['selector'],source_ref)
                if value.get('sourceState')!='current': raise ValueError('source changed')
                facts=value['resultIdentity']
                count={'maxReferences':target['max_references']} if 'max_references' in target else ({'maxInstructions':target['max_instructions']} if 'max_instructions' in target else {})
                return {'dossierID':target['dossier_id'],'dossierRevision':value['dossierRevision'],
                    'selector':value['function'],**count,'resultSHA256':facts['resultSHA256'],
                    '_sourceID':facts['sourceID'],'_sourceSHA':facts['sourceSHA256'],'_sourceRevision':facts['sourceRevision']}
            qualified = request.get('messageID') is not None
            if qualified and (identity['sessionID'] != resolved['rootSessionID'] or identity['actorID'] != 'main'): return unavailable
            async def proof(phase, record, **extra):
                host_chat=binding['stableChatID']; workspace=worker._bridge.mapped_session_workspace(host_chat)
                if not workspace: return False
                payload={'stableChatID':host_chat,'messageID':record['messageID'],'actorID':identity['actorID'],
                    'rootOperationID':identity['rootOperationID'],'executionRevision':identity['executionRevision'],
                    'innerTurnID':identity['innerTurnID'],'callID':request['callID'],'inputSHA256':record['inputSHA256'],
                    'phase':phase,**extra}
                response=await pool.session_native_analysis_proof(host_chat,identity['sessionID'],workspace,payload,owner=owner)
                return response.status_code==200 and response.json()=={'state':'current'}
            async with self._managed_execution_lock:
                if self._managed_actor_turns.get(key) is not entry or entry.get('state')!='active' or entry.get('policyRevision')!=policy or not current(): return unavailable
                records=entry.setdefault('nativeAnalysisSources',{})
                if request['operation']=='capture':
                    if request['callID'] in records or len(records)>=8: return unavailable
                    if qualified:
                        if request['analysisOperation']=='xrefs_from_instruction':
                            calculated=instruction_xref_input_sha(request['sourceRef'],request['expectedRevision'],request['instructionTarget'])
                        elif request['analysisOperation']=='decompile':
                            calculated=decompile_input_sha(request['sourceRef'],request['expectedRevision'],request['decompileTarget'],request['decompileProfile'])
                        elif request['analysisOperation'] in ('disassembly','xrefs_to_entry'):
                            is_xref=request['analysisOperation']=='xrefs_to_entry'
                            calculated=(xref_input_sha if is_xref else disassembly_input_sha)(request['sourceRef'],request['expectedRevision'],request['xrefTarget' if is_xref else 'functionTarget'])
                        else:
                            values=['openclank.native-analysis.v1',request['sourceRef'],request['expectedRevision']['kind'],request['expectedRevision']['value'],request['analysisOperation'],request['maxFunctions'],request['maxStrings']]
                            if any(not isinstance(value,str) or not value.isascii() for value in values[:5]): return unavailable
                            calculated=hashlib.sha256(json.dumps(values,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
                        if calculated!=request['inputSHA256']: return unavailable
                    record={'state':'capturing','qualified':qualified}
                    if qualified: record.update(messageID=request['messageID'],inputSHA256=request['inputSHA256'],analysisOperation=request['analysisOperation'])
                    if request.get('analysisOperation') in ('disassembly','xrefs_to_entry','decompile','xrefs_from_instruction'):
                        record['requestedTarget']=request['instructionTarget' if request['analysisOperation']=='xrefs_from_instruction' else 'decompileTarget' if request['analysisOperation']=='decompile' else 'xrefTarget' if request['analysisOperation']=='xrefs_to_entry' else 'functionTarget']
                    if request.get('analysisOperation')=='decompile':record['decompileProfile']=request['decompileProfile']
                    records[request['callID']]=record
                else:
                    record=records.get(request['callID'])
                    expected='finalized' if request['operation']=='stage' else 'captured'
                    if not isinstance(record,dict) or record.get('state')!=expected or record.get('token')!=request['captureToken'] or record.get('sha')!=request['sourceSHA256']: return unavailable
                    if record.get('qualified') and (request.get('messageID')!=record['messageID'] or request.get('inputSHA256')!=record['inputSHA256']): return unavailable
                    if request['operation']=='stage' and (not record.get('qualified') or request['analysisOperation']!=record['analysisOperation']): return unavailable
                    record['state']='staging' if request['operation']=='stage' else 'finalizing'
            if request['operation']=='stage':
                async def stage_owned():
                    async with barrier.admitted(owner):
                        if not current() or await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']}) != binding or not await proof('running',record): return unavailable
                        if not await source_current(facade,context,record['ref'],record['revision']): return unavailable
                        if record.get('requestedTarget') and await function_witness(record['requestedTarget'],record['ref'])!=record['functionWitness']: return unavailable
                        output=base64.b64decode(request['outputBase64'],validate=True)
                        if base64.b64encode(output).decode('ascii')!=request['outputBase64'] or digest(output)!=request['outputSHA256']: return unavailable
                        if legacy_producer_profile(request['providerVersion']) is None: return unavailable
                        target={k:v for k,v in record.get('functionWitness',{}).items() if not k.startswith('_')} or None
                        result=validate_result(output,operation=record['analysisOperation'],source_sha=record['sha'],source_revision=record['revision'],provider_sha=request['providerSHA256'],expected_target=target,expected_profile=record.get('decompileProfile'))
                        if result['provider']!='rizin' or result['providerVersion']!=request['providerVersion']: return unavailable
                        artifacts=getattr(getattr(state,'account_owner_lifecycle',None),'artifact_store',None)
                        if artifacts is None:return unavailable
                        partition=account_artifact_owner(account)
                        def retain():
                            row=artifacts.put(owner=partition,chunks=[output],media_type='application/json')
                            data=b''.join(artifacts.read_chunks(owner=partition,artifact_id=row.id))
                            if data!=output or row.content_sha256!=request['outputSHA256'] or row.size_bytes!=len(output): raise ValueError('artifact changed')
                            return row.id
                        artifact_id=await asyncio.to_thread(retain)
                        if not current() or not await source_current(facade,context,record['ref'],record['revision']) or not await proof('running',record): return unavailable
                        if record.get('requestedTarget') and await function_witness(record['requestedTarget'],record['ref'])!=record['functionWitness']: return unavailable
                        issued=int(time.time())
                        descriptor={'v':1,'kind':'provisional','account':account,'workspace':binding['authorityWorkspaceID'],
                            'chat':binding['stableChatID'],'nativeSession':identity['sessionID'],'message':record['messageID'],
                            'call':request['callID'],'actor':identity['actorID'],'root':identity['rootOperationID'],
                            'turn':identity['innerTurnID'],'executionRevision':identity['executionRevision'],'holder':holder,
                            'sourceID':record['sourceID'],'sourceSHA':record['sha'],'sourceRevision':record['revision'],
                            'operation':record['analysisOperation'],'providerSHA':request['providerSHA256'],
                            'providerVersion':request['providerVersion'],'providerEvidence':'native_producer_attested',
                            'versionEvidence':'operator_configured_unverified','artifactID':artifact_id,'outputSHA':request['outputSHA256'],
                            'size':len(output),'inputSHA':record['inputSHA256'],'issued':issued,'expires':issued+86400}
                        provisional=seal(descriptor,kind='provisional')
                        async with self._managed_execution_lock:
                            if self._managed_actor_turns.get(key) is not entry or entry.get('state')!='active' or records.get(request['callID']) is not record or record.get('state')!='staging' or not current():return unavailable
                            record['state']='staged'
                        return validate_managed_method_result(method,{'state':'staged','provisional':provisional})
                task=asyncio.create_task(stage_owned())
                try:return await asyncio.shield(task)
                except asyncio.CancelledError:
                    task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
                    raise
            async with barrier.admitted(owner):
                if not current() or await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']}) != binding: return unavailable
                if record.get('qualified') and not await proof('running',record): return unavailable
                if request['operation']=='capture':
                    captured=await capture_source(facade,context,request['sourceRef'],request['expectedRevision'])
                    _,source_ref=facade._provider_for_ref(context,request['sourceRef'],capability='download')
                    witness=await function_witness(record['requestedTarget'],request['sourceRef']) if record.get('requestedTarget') else None
                    if witness and (witness['_sourceID']!=source_ref.stable_id or witness['_sourceSHA']!=captured.sha256 or witness['_sourceRevision']!=captured.revision): return unavailable
                else:
                    valid=await source_current(facade,context,record['ref'],record['revision'])
                    if valid and record.get('requestedTarget'):valid=await function_witness(record['requestedTarget'],record['ref'])==record['functionWitness']
                if not current() or await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']}) != binding or (record.get('qualified') and not await proof('running',record)): return unavailable
                async with self._managed_execution_lock:
                    if self._managed_actor_turns.get(key) is not entry or entry.get('state')!='active' or entry.get('policyRevision')!=policy or records.get(request['callID']) is not record or not current(): return unavailable
                    if request['operation']=='capture':
                        token=secrets.token_hex(32)
                        record.update(state='captured',token=token,sha=captured.sha256,ref=captured.resource_ref,revision=captured.revision,sourceID=source_ref.stable_id)
                        result={'state':'captured','captureToken':token,'sourceSHA256':captured.sha256,'revision':captured.revision,'size':len(captured.data),'bytesBase64':base64.b64encode(captured.data).decode('ascii')}
                        if witness:
                            record['functionWitness']=witness
                            result['instructionTarget' if 'instruction' in witness else 'functionTarget']={k:v for k,v in witness.items() if not k.startswith('_')}
                    else:
                        record['state']='finalized' if valid and record.get('qualified') else 'finished'; result={'state':'current'} if valid else unavailable
                return validate_managed_method_result(method,result)
        except Exception:
            return unavailable

    async def _handle_managed_task_read(self, params):
        """Read only positively bound tasks in the current Files workspace."""
        from core.models import get_session_manager_instance
        from core.database import SessionLocal
        from routes.task.task_routes import _observation_policy_generation
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        from src.openclank.managed_task_read import read_tasks
        from src.openclank.workspace_policy_service import resolve_owned_workspace
        method = '_openclank/session/v1/task/read'
        unavailable = {'ok':False, 'result':None}
        try:
            request = validate_managed_method_request(method, params); args = request['args']
            if set(args) != {'action','task_id','limit','cursor'}: return unavailable
            action, task_id, limit, cursor = (args[k] for k in ('action','task_id','limit','cursor'))
            if (action not in ('list','inspect') or type(limit) is not int or not 1 <= limit <= 32
                    or (cursor is not None and (not isinstance(cursor,str) or not cursor or len(cursor) > 8192))
                    or (action == 'list' and task_id is not None)
                    or (action == 'inspect' and (not isinstance(task_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',task_id)
                        or cursor is not None or limit != 1))): return unavailable
            digest = hashlib.sha256(json.dumps(['openclank.task-read.v1',action,task_id,limit,cursor],
                ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
            if digest != request['argsSHA256']: return unavailable
            identity = {field:request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
            key = self._managed_turn_key(identity)
            async with self._managed_execution_lock:
                entry = self._managed_actor_turns.get(key)
                if not isinstance(entry,dict) or entry.get('state') != 'active' or entry.get('taskCreateClosing'): return unavailable
                owner = entry.get('owner'); account = entry.get('ownerAccountID') or entry.get('foregroundAccountID'); policy = entry.get('policyRevision')
            if not isinstance(account,str) or not account: return unavailable
            if (await self._handle_managed_tool_admit({**identity,'toolName':'manage_tasks','kind':'tool','toolCallID':request['toolCallID']})).get('allowed') is not True: return unavailable
            resolved = await self.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
            root_session = resolved.get('rootSessionID')
            if not isinstance(root_session, str) or not root_session: return unavailable
            binding = await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']})
            manager = get_session_manager_instance(); state = getattr(manager,'lifecycle_context',None) if manager else None
            auth = getattr(state,'auth_manager',None); barrier = getattr(state,'account_request_barrier',None); repository = getattr(state,'files_policy_repository',None)
            supervisor = getattr(state,'mimo_supervisor',None)
            pool = supervisor.owner_lifecycle_pool() if supervisor is not None else None
            worker = pool.worker_for_owner(owner) if pool else None
            holder = self._managed_provider_generation; generation = getattr(worker,'installed_generation',None)
            chat = binding.get('stableChatID'); workspace_id = binding.get('authorityWorkspaceID')
            workspace = worker._bridge.mapped_session_workspace(chat) if worker and chat else None
            session = manager.get_session(chat) if manager and chat else None
            if (not auth or not barrier or not repository or not worker or not holder or type(generation) is not int or not workspace or not workspace_id
                    or not session or session.owner != owner or getattr(session,'incognito',False) or getattr(session,'archived',False)
                    or binding.get('owner') != owner or chat != entry.get('stableChatID') or workspace_id != entry.get('authorityWorkspaceID')): return unavailable
            files_binding = resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True)
            if files_binding.workspace.id != workspace_id or files_binding.path != workspace: return unavailable
            epoch = _observation_policy_generation(repository)
            source = {field:request[field] for field in ('messageID','actorID','rootOperationID','executionRevision','innerTurnID','toolCallID','toolName','argsSHA256')}; source['stableChatID'] = chat
            if identity['sessionID'] != root_session: source['sourceSessionID'] = identity['sessionID']
            async with barrier.admitted(owner):
                if auth.account_id(owner) != account or auth.username_for_account_id(account) != owner: return unavailable
                response = await worker.session_task_read_source_request(chat,root_session,workspace,source,owner=owner)
                if response.status_code != 200 or response.json() != {'state':'current','argsSHA256':digest}: return unavailable
                async with self._durable_session_map.async_chat_admission_lock(chat,blocking=False,existing_only=True):
                    if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                            or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker or worker._bridge is not self or not worker.is_alive()
                            or self._managed_provider_generation != holder or worker.installed_generation != generation
                            or manager.get_session(chat) is not session or session.owner != owner
                            or getattr(session,'incognito',False) or getattr(session,'archived',False)
                            or await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']}) != binding): return unavailable
                    async with self._managed_execution_lock:
                        if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                                or entry.get('taskCreateClosing') or entry.get('policyRevision') != policy): return unavailable
                    if (resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True) != files_binding
                            or _observation_policy_generation(repository) != epoch): return unavailable
                    scope = hashlib.sha256(json.dumps([account,chat,holder,policy],separators=(',',':')).encode()).hexdigest()
                    with SessionLocal() as db:
                        result = read_tasks(db,owner=owner,account=account,workspace_id=workspace_id,policy_generation=epoch,
                            action=action,task_id=task_id,cursor=cursor,limit=limit,cursor_scope=scope)
                    if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                            or _observation_policy_generation(repository) != epoch
                            or resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True) != files_binding): return unavailable
                    for item in result['items']:
                        revision = item.pop('_revision', None)
                        item['expected_revision'] = None
                        if revision:
                            import secrets
                            token = secrets.token_urlsafe(32)
                            async with self._managed_execution_lock:
                                if self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active' or entry.get('taskCreateClosing') or entry.get('policyRevision') != policy: return unavailable
                                guards = entry.setdefault('taskReadGuards', {})
                                if len(guards) >= 128: return unavailable
                                guards[token] = {'taskID':item['taskID'], 'revision':revision, 'account':account, 'workspace':workspace_id, 'holder':holder, 'policy':policy}
                            item['expected_revision'] = token
                    return validate_managed_method_result(method,{'ok':True,'result':result})
        except Exception:
            return unavailable

    async def _handle_managed_task_control(self, params):
        """Apply once, or read the original same-call scheduling receipt."""
        from contextlib import nullcontext
        from core.models import get_session_manager_instance
        from core.database import SessionLocal
        from routes.task.task_routes import _observation_policy_generation
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        from src.openclank.task_schedule_control import apply_schedule, delete_settled_task, scoped_task
        from core.database import Session as DbSession
        from src.openclank.workspace_policy_service import resolve_owned_workspace
        method = '_openclank/session/v1/task/control'
        unavailable = {'ok':False, 'result':None}
        try:
            request = validate_managed_method_request(method, params); args = request['args']
            if set(args) != {'action','task_id','expected_revision'}: return unavailable
            action, task_id, revision = (args[k] for k in ('action','task_id','expected_revision'))
            mode = request['mode']
            if (action not in ('pause','resume','delete') or mode not in ('apply','status')
                    or not isinstance(task_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',task_id)
                    or not isinstance(revision,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,64}',revision)): return unavailable
            digest = hashlib.sha256(json.dumps(['openclank.task-control.v1',action,task_id,revision],
                ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
            if digest != request['argsSHA256']: return unavailable
            identity = {field:request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
            key = self._managed_turn_key(identity)
            async with self._managed_execution_lock:
                entry = self._managed_actor_turns.get(key)
                if not isinstance(entry,dict) or entry.get('state') != 'active' or entry.get('taskCreateClosing'): return unavailable
                owner = entry.get('owner'); account = entry.get('ownerAccountID') or entry.get('foregroundAccountID'); policy = entry.get('policyRevision')
            if not isinstance(account,str) or not account: return unavailable
            if (await self._handle_managed_tool_admit({**identity,'toolName':'manage_tasks','kind':'tool','toolCallID':request['toolCallID']})).get('allowed') is not True: return unavailable
            resolved = await self.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
            root_session = resolved.get('rootSessionID')
            if not isinstance(root_session, str) or not root_session: return unavailable
            binding = await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']})
            manager = get_session_manager_instance(); state = getattr(manager,'lifecycle_context',None) if manager else None
            auth = getattr(state,'auth_manager',None); barrier = getattr(state,'account_request_barrier',None); repository = getattr(state,'files_policy_repository',None)
            supervisor = getattr(state,'mimo_supervisor',None)
            pool = supervisor.owner_lifecycle_pool() if supervisor is not None else None
            worker = pool.worker_for_owner(owner) if pool else None
            holder = self._managed_provider_generation; generation = getattr(worker,'installed_generation',None)
            chat = binding.get('stableChatID'); workspace_id = binding.get('authorityWorkspaceID')
            workspace = worker._bridge.mapped_session_workspace(chat) if worker and chat else None
            session = manager.get_session(chat) if manager and chat else None
            if (not auth or not barrier or not repository or not worker or not holder or type(generation) is not int or not workspace or not workspace_id
                    or not session or session.owner != owner or getattr(session,'incognito',False) or getattr(session,'archived',False)
                    or binding.get('owner') != owner or chat != entry.get('stableChatID') or workspace_id != entry.get('authorityWorkspaceID')): return unavailable
            files_binding = resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True)
            if files_binding.workspace.id != workspace_id or files_binding.path != workspace: return unavailable
            epoch = _observation_policy_generation(repository)
            source = {field:request[field] for field in ('messageID','actorID','rootOperationID','executionRevision','innerTurnID','toolCallID','toolName','argsSHA256')}; source['stableChatID'] = chat
            if identity['sessionID'] != root_session: source['sourceSessionID'] = identity['sessionID']
            async with barrier.admitted(owner):
                if auth.account_id(owner) != account or auth.username_for_account_id(account) != owner: return unavailable
                response = await worker.session_task_control_source_request(chat,root_session,workspace,source,owner=owner)
                if response.status_code != 200 or response.json() != {'state':'current','argsSHA256':digest}: return unavailable
                async with self._durable_session_map.async_chat_admission_lock(chat,blocking=False,existing_only=True):
                    if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                            or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker or worker._bridge is not self or not worker.is_alive()
                            or self._managed_provider_generation != holder or worker.installed_generation != generation
                            or manager.get_session(chat) is not session or session.owner != owner
                            or getattr(session,'incognito',False) or getattr(session,'archived',False)
                            or await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']}) != binding): return unavailable
                    async with self._managed_execution_lock:
                        if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                                or entry.get('taskCreateClosing') or entry.get('policyRevision') != policy): return unavailable
                    if (resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True) != files_binding
                            or _observation_policy_generation(repository) != epoch): return unavailable
                    scheduler = getattr(state, 'task_scheduler', None)
                    if action == 'delete' and scheduler is None: return unavailable
                    async with (scheduler._executing_lock if action == 'delete' else nullcontext()):
                        async with self._managed_execution_lock:
                            if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                                    or entry.get('taskCreateClosing') or entry.get('policyRevision') != policy): return unavailable
                            if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                                    or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker
                                    or worker._bridge is not self or not worker.is_alive() or self._managed_provider_generation != holder
                                    or worker.installed_generation != generation or _observation_policy_generation(repository) != epoch
                                    or resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True) != files_binding): return unavailable
                            guard = entry.get('taskReadGuards', {}).get(revision)
                            expected = {'taskID':task_id, 'account':account, 'workspace':workspace_id, 'holder':holder, 'policy':policy}
                            if not isinstance(guard,dict) or any(guard.get(k) != v for k,v in expected.items()): return unavailable
                            calls = entry.setdefault('taskControlIssued', {})
                            call_id = request['toolCallID']
                            frozen = {'digest':digest, 'messageID':request['messageID'], 'toolName':request['toolName']}
                            record = calls.get(call_id)
                            if record is not None and record.get('identity') != frozen: return unavailable
                            fresh = record is None and mode == 'apply'
                            if fresh:
                                if len(calls) >= 128: return unavailable
                                record = {'identity':frozen, 'original':'unknown'}
                                calls[call_id] = record
                        # No awaits between issuance, the SQLite commit and recording its exact result.
                        original = record['original'] if record else 'unknown'
                        with SessionLocal() as db:
                            if db.get_bind().dialect.name != 'sqlite': return unavailable
                            db.connection().exec_driver_sql('BEGIN IMMEDIATE' if fresh else 'BEGIN')
                            source_row = db.query(DbSession).filter_by(id=chat, owner=owner).first()
                            if not source_row or source_row.archived or source_row.workspace_id != workspace_id: return unavailable
                            if fresh:
                                try:
                                    candidate = scoped_task(db, owner=owner, account=account, task_id=task_id, workspace_id=workspace_id)
                                    from src.task_action_policy import is_admin_only_task_action, owner_has_admin_task_privileges
                                    if (not candidate or (action == 'resume' and is_admin_only_task_action(candidate.task_type, candidate.action)
                                            and not owner_has_admin_task_privileges(owner))): raise ValueError('task unavailable')
                                    if action == 'delete':
                                        if (task_id in scheduler._executing or scheduler._execution_handles_for_task(task_id)
                                                or scheduler._owner_lifecycle_fenced(owner)): raise ValueError('task is active')
                                        delete_settled_task(db, owner=owner, account=account, task_id=task_id,
                                            workspace_id=workspace_id, expected_revision=guard['revision'])
                                    else:
                                        task = apply_schedule(db, owner=owner, account=account, task_id=task_id, action=action,
                                            workspace_id=workspace_id, expected_revision=guard['revision'], strict_schedule=True)
                                    db.commit()
                                    original = 'applied'
                                except ValueError:
                                    db.rollback()
                                    original = 'refused'
                                record['original'] = original
                            task = scoped_task(db, owner=owner, account=account, task_id=task_id, workspace_id=workspace_id)
                            current = task.status if task and isinstance(task.status,str) and len(task.status) <= 128 else None
                        return validate_managed_method_result(method, {'ok':True, 'result':{'original':original, 'action':action, 'currentStatus':current}})
        except Exception:
            return unavailable

    async def _handle_managed_task_edit(self, params):
        """Edit bounded LLM content once, or read the original receipt."""
        from core.models import get_session_manager_instance
        from core.database import SessionLocal
        from routes.task.task_routes import _observation_policy_generation
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        from src.openclank.task_schedule_control import apply_content_edit, scoped_task
        from core.database import Session as DbSession
        from src.openclank.workspace_policy_service import resolve_owned_workspace
        method = '_openclank/session/v1/task/edit'
        unavailable = {'ok':False, 'result':None}
        try:
            request = validate_managed_method_request(method, params); args = request['args']
            if (set(args) - {'action','task_id','expected_revision','name','prompt'}
                    or not {'action','task_id','expected_revision'} <= set(args)
                    or not ({'name','prompt'} & set(args))): return unavailable
            action, task_id, revision = (args[k] for k in ('action','task_id','expected_revision'))
            mode = request['mode']
            if (action != 'edit' or mode not in ('apply','status')
                    or not isinstance(task_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',task_id)
                    or not isinstance(revision,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,64}',revision)): return unavailable
            fields = {key:args[key] for key in ('name','prompt') if key in args}
            if any(not isinstance(value,str) or not value.strip() or len(value.encode('utf-8')) > (200 if key == 'name' else 16384)
                   for key,value in fields.items()): return unavailable
            digest = hashlib.sha256(json.dumps(['openclank.task-edit.v1',task_id,revision,args.get('name'),args.get('prompt')],
                ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
            if digest != request['argsSHA256']: return unavailable
            identity = {field:request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
            key = self._managed_turn_key(identity)
            async with self._managed_execution_lock:
                entry = self._managed_actor_turns.get(key)
                if not isinstance(entry,dict) or entry.get('state') != 'active' or entry.get('taskCreateClosing'): return unavailable
                owner = entry.get('owner'); account = entry.get('ownerAccountID') or entry.get('foregroundAccountID'); policy = entry.get('policyRevision')
            if not isinstance(account,str) or not account: return unavailable
            if (await self._handle_managed_tool_admit({**identity,'toolName':'manage_tasks','kind':'tool','toolCallID':request['toolCallID']})).get('allowed') is not True: return unavailable
            resolved = await self.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
            root_session = resolved.get('rootSessionID')
            if not isinstance(root_session, str) or not root_session: return unavailable
            binding = await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']})
            manager = get_session_manager_instance(); state = getattr(manager,'lifecycle_context',None) if manager else None
            auth = getattr(state,'auth_manager',None); barrier = getattr(state,'account_request_barrier',None); repository = getattr(state,'files_policy_repository',None)
            supervisor = getattr(state,'mimo_supervisor',None)
            pool = supervisor.owner_lifecycle_pool() if supervisor is not None else None
            worker = pool.worker_for_owner(owner) if pool else None
            holder = self._managed_provider_generation; generation = getattr(worker,'installed_generation',None)
            chat = binding.get('stableChatID'); workspace_id = binding.get('authorityWorkspaceID')
            workspace = worker._bridge.mapped_session_workspace(chat) if worker and chat else None
            session = manager.get_session(chat) if manager and chat else None
            if (not auth or not barrier or not repository or not worker or not holder or type(generation) is not int or not workspace or not workspace_id
                    or not session or session.owner != owner or getattr(session,'incognito',False) or getattr(session,'archived',False)
                    or binding.get('owner') != owner or chat != entry.get('stableChatID') or workspace_id != entry.get('authorityWorkspaceID')): return unavailable
            files_binding = resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True)
            if files_binding.workspace.id != workspace_id or files_binding.path != workspace: return unavailable
            epoch = _observation_policy_generation(repository)
            source = {field:request[field] for field in ('messageID','actorID','rootOperationID','executionRevision','innerTurnID','toolCallID','toolName','argsSHA256')}; source['stableChatID'] = chat
            if identity['sessionID'] != root_session: source['sourceSessionID'] = identity['sessionID']
            async with barrier.admitted(owner):
                if auth.account_id(owner) != account or auth.username_for_account_id(account) != owner: return unavailable
                response = await worker.session_task_edit_source_request(chat,root_session,workspace,source,owner=owner)
                if response.status_code != 200 or response.json() != {'state':'current','argsSHA256':digest}: return unavailable
                async with self._durable_session_map.async_chat_admission_lock(chat,blocking=False,existing_only=True):
                    if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                            or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker or worker._bridge is not self or not worker.is_alive()
                            or self._managed_provider_generation != holder or worker.installed_generation != generation
                            or manager.get_session(chat) is not session or session.owner != owner
                            or getattr(session,'incognito',False) or getattr(session,'archived',False)
                            or await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']}) != binding): return unavailable
                    async with self._managed_execution_lock:
                        if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                                or entry.get('taskCreateClosing') or entry.get('policyRevision') != policy): return unavailable
                    if (resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True) != files_binding
                            or _observation_policy_generation(repository) != epoch): return unavailable
                    async with self._managed_execution_lock:
                        if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                                or entry.get('taskCreateClosing') or entry.get('policyRevision') != policy): return unavailable
                        if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                                or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker or worker._bridge is not self or not worker.is_alive()
                                or self._managed_provider_generation != holder or worker.installed_generation != generation
                                or _observation_policy_generation(repository) != epoch
                                or resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True) != files_binding): return unavailable
                        guard = entry.get('taskReadGuards', {}).get(revision)
                        expected = {'taskID':task_id, 'account':account, 'workspace':workspace_id, 'holder':holder, 'policy':policy}
                        if not isinstance(guard,dict) or any(guard.get(k) != v for k,v in expected.items()): return unavailable
                        calls = entry.setdefault('taskEditIssued', {})
                        call_id = request['toolCallID']
                        frozen = {'digest':digest, 'messageID':request['messageID'], 'toolName':request['toolName']}
                        record = calls.get(call_id)
                        if record is not None and record.get('identity') != frozen: return unavailable
                        fresh = record is None and mode == 'apply'
                        if fresh:
                            if len(calls) >= 128: return unavailable
                            record = {'identity':frozen, 'original':'unknown'}
                            calls[call_id] = record
                    # No await between issuance, transaction and recording the exact receipt.
                    original = record['original'] if record else 'unknown'
                    changed = record.get('changed', []) if record else []
                    with SessionLocal() as db:
                        if db.get_bind().dialect.name != 'sqlite': return unavailable
                        db.connection().exec_driver_sql('BEGIN IMMEDIATE' if fresh else 'BEGIN')
                        source_row = db.query(DbSession).filter_by(id=chat, owner=owner).first()
                        if not source_row or source_row.archived or source_row.workspace_id != workspace_id: return unavailable
                        if not fresh and not scoped_task(db,owner=owner,account=account,task_id=task_id,workspace_id=workspace_id): return unavailable
                        if fresh:
                            try:
                                apply_content_edit(db, owner=owner, account=account, task_id=task_id,
                                    workspace_id=workspace_id, expected_revision=guard['revision'], fields=fields)
                                db.commit()
                                original = 'applied'; changed = sorted(fields)
                            except ValueError:
                                db.rollback()
                                original = 'refused'; changed = []
                            record['original'] = original; record['changed'] = changed
                    return validate_managed_method_result(method, {'ok':True, 'result':{'original':original, 'changed':changed}})
        except Exception:
            return unavailable

    async def _handle_managed_task_run(self, params):
        """Enqueue one selected occurrence or read the original same-call receipt."""
        from core.models import get_session_manager_instance
        from core.database import SessionLocal
        from routes.task.task_routes import _observation_policy_generation
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        from src.openclank.task_schedule_control import scoped_task, task_revision
        from src.openclank.task_execution_context import manual_task_sha256
        from core.database import TaskRun
        from core.database import Session as DbSession
        from src.openclank.workspace_policy_service import resolve_owned_workspace
        method = '_openclank/session/v1/task/run'
        unavailable = {'ok':False, 'result':None}
        try:
            request = validate_managed_method_request(method, params); args = request['args']
            if set(args) != {'action','task_id','expected_revision'}: return unavailable
            action, task_id, revision = (args[k] for k in ('action','task_id','expected_revision'))
            mode = request['mode']
            if (action != 'run' or mode not in ('apply','status')
                    or not isinstance(task_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',task_id)
                    or not isinstance(revision,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,64}',revision)): return unavailable
            digest = hashlib.sha256(json.dumps(['openclank.task-run.v1',task_id,revision],
                ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
            if digest != request['argsSHA256']: return unavailable
            identity = {field:request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
            key = self._managed_turn_key(identity)
            async with self._managed_execution_lock:
                entry = self._managed_actor_turns.get(key)
                if not isinstance(entry,dict) or entry.get('state') != 'active' or entry.get('taskCreateClosing'): return unavailable
                owner = entry.get('owner'); account = entry.get('ownerAccountID') or entry.get('foregroundAccountID'); policy = entry.get('policyRevision')
            if not isinstance(account,str) or not account: return unavailable
            if (await self._handle_managed_tool_admit({**identity,'toolName':'manage_tasks','kind':'tool','toolCallID':request['toolCallID']})).get('allowed') is not True: return unavailable
            resolved = await self.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
            root_session = resolved.get('rootSessionID')
            if not isinstance(root_session, str) or not root_session: return unavailable
            binding = await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']})
            manager = get_session_manager_instance(); state = getattr(manager,'lifecycle_context',None) if manager else None
            auth = getattr(state,'auth_manager',None); barrier = getattr(state,'account_request_barrier',None); repository = getattr(state,'files_policy_repository',None)
            supervisor = getattr(state,'mimo_supervisor',None)
            pool = supervisor.owner_lifecycle_pool() if supervisor is not None else None
            worker = pool.worker_for_owner(owner) if pool else None
            holder = self._managed_provider_generation; generation = getattr(worker,'installed_generation',None)
            chat = binding.get('stableChatID'); workspace_id = binding.get('authorityWorkspaceID')
            workspace = worker._bridge.mapped_session_workspace(chat) if worker and chat else None
            session = manager.get_session(chat) if manager and chat else None
            if (not auth or not barrier or not repository or not worker or not holder or type(generation) is not int or not workspace or not workspace_id
                    or not session or session.owner != owner or getattr(session,'incognito',False) or getattr(session,'archived',False)
                    or binding.get('owner') != owner or chat != entry.get('stableChatID') or workspace_id != entry.get('authorityWorkspaceID')): return unavailable
            files_binding = resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True)
            if files_binding.workspace.id != workspace_id or files_binding.path != workspace: return unavailable
            epoch = _observation_policy_generation(repository)
            source = {field:request[field] for field in ('messageID','actorID','rootOperationID','executionRevision','innerTurnID','toolCallID','toolName','argsSHA256')}; source['stableChatID'] = chat
            if identity['sessionID'] != root_session: source['sourceSessionID'] = identity['sessionID']
            async with barrier.admitted(owner):
                if auth.account_id(owner) != account or auth.username_for_account_id(account) != owner: return unavailable
                response = await worker.session_task_run_source_request(chat,root_session,workspace,source,owner=owner)
                if response.status_code != 200 or response.json() != {'state':'current','argsSHA256':digest}: return unavailable
                async with self._durable_session_map.async_chat_admission_lock(chat,blocking=False,existing_only=True):
                    if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                            or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker or worker._bridge is not self or not worker.is_alive()
                            or self._managed_provider_generation != holder or worker.installed_generation != generation
                            or manager.get_session(chat) is not session or session.owner != owner
                            or getattr(session,'incognito',False) or getattr(session,'archived',False)
                            or await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']}) != binding): return unavailable
                    async with self._managed_execution_lock:
                        if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                                or entry.get('taskCreateClosing') or entry.get('policyRevision') != policy): return unavailable
                    if (resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True) != files_binding
                            or _observation_policy_generation(repository) != epoch): return unavailable
                    scheduler = getattr(state, 'task_scheduler', None)
                    if scheduler is None: return unavailable
                    # Serialize reservation with all ordinary manual launches. No await occurs
                    # between C2 issuance, SQL commit and registration of the owned task.
                    async with scheduler._executing_lock:
                        async with self._managed_execution_lock:
                            if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                                    or entry.get('taskCreateClosing') or entry.get('policyRevision') != policy): return unavailable
                            if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                                    or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker or worker._bridge is not self or not worker.is_alive()
                                    or self._managed_provider_generation != holder or worker.installed_generation != generation
                                    or _observation_policy_generation(repository) != epoch
                                    or resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True) != files_binding): return unavailable
                            guard = entry.get('taskReadGuards', {}).get(revision)
                            expected_guard = {'taskID':task_id, 'account':account, 'workspace':workspace_id, 'holder':holder, 'policy':policy}
                            if not isinstance(guard,dict) or any(guard.get(k) != v for k,v in expected_guard.items()): return unavailable
                            calls = entry.setdefault('taskRunIssued', {})
                            call_id = request['toolCallID']
                            frozen = {'digest':digest, 'messageID':request['messageID'], 'toolName':request['toolName']}
                            record = calls.get(call_id)
                            if record is not None and record.get('identity') != frozen: return unavailable
                            fresh = record is None and mode == 'apply'
                            if fresh:
                                if len(calls) >= 128: return unavailable
                                material = json.dumps(['openclank.selected-task-run.v1',account,identity,request['messageID'],call_id,request['toolName'],digest], sort_keys=True,separators=(',',':'))
                                run_id = 'managed-run-' + hashlib.sha256(material.encode('utf-8')).hexdigest()
                                record = {'identity':frozen, 'runID':run_id, 'original':'unknown'}
                                calls[call_id] = record
                        original = record['original'] if record else 'unknown'
                        run_id = record['runID'] if record else None
                        current = None
                        with SessionLocal() as db:
                            if db.get_bind().dialect.name != 'sqlite': return unavailable
                            db.connection().exec_driver_sql('BEGIN IMMEDIATE' if fresh else 'BEGIN')
                            source_row = db.query(DbSession).filter_by(id=chat, owner=owner).first()
                            if not source_row or source_row.archived or source_row.workspace_id != workspace_id: return unavailable
                            task = scoped_task(db, owner=owner, account=account, task_id=task_id, workspace_id=workspace_id)
                            if not task: return unavailable
                            if fresh:
                                from src.task_action_policy import is_admin_only_task_action, owner_has_admin_task_privileges
                                if (task_revision(task) != guard['revision'] or task.status != 'active'
                                        or scheduler._task_owner_lifecycle_fenced(task_id) or task_id in scheduler._executing
                                        or scheduler._has_waiting_run(task_id)
                                        or (is_admin_only_task_action(task.task_type, task.action) and not owner_has_admin_task_privileges(owner))):
                                    original = record['original'] = 'refused'
                                elif db.query(TaskRun.id).filter_by(id=run_id).first():
                                    original = 'unknown'
                                else:
                                    expected = {'owner':owner,'account':account,'bound_account':account,'status':task.status,'spec':manual_task_sha256(task)}
                                    db.add(TaskRun(id=run_id,task_id=task_id,status='queued',result='Queued for admission'))
                                    db.commit()
                                    scheduler._executing.add(task_id)
                                    started = [False]
                                    async def selected_invocation():
                                        started[0] = True
                                        await scheduler._execute_owned_manual_task(task_id,expected,selected_run_id=run_id)
                                    invocation = selected_invocation()
                                    handle = None
                                    # Before first poll, cancellation positively proves no issue.
                                    # Touch only the exact retained handle and its reservation.
                                    def cleanup_selected(completed):
                                        if completed.cancelled() and not started[0]:
                                            scheduler._skip_selected_manual_run(task_id, run_id)
                                        owns_reservation = scheduler._task_handles.get(task_id) is completed
                                        scheduler._unregister_task_handle(task_id, completed)
                                        if owns_reservation:
                                            scheduler._executing.discard(task_id)
                                    try:
                                        handle = asyncio.create_task(invocation)
                                        handle.add_done_callback(cleanup_selected)
                                        scheduler._register_task_handle(task_id, handle)
                                    except Exception:
                                        if handle is not None: handle.cancel()
                                        else: invocation.close()
                                        scheduler._executing.discard(task_id)
                                    else:
                                        original = record['original'] = 'queued'
                            row = db.query(TaskRun).filter_by(id=run_id,task_id=task_id).first() if run_id else None
                            if row and isinstance(row.status,str) and len(row.status) <= 128: current = row.status
                            if original == 'queued' and row is None: original = 'unknown'
                        return validate_managed_method_result(method, {'ok':True,'result':{'original':original,'runID':run_id,'state':current}})
        except Exception:
            return unavailable

    async def _handle_managed_task_status(self, params):
        return await self._handle_managed_task_create(params, status_only=True)

    async def _handle_managed_task_create(self, params, *, status_only=False):
        """Create only a freshly sourced task under the current actor/account."""
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        from src.openclank.managed_task_create import create_or_status
        method = '_openclank/session/v1/task/status' if status_only else '_openclank/session/v1/task/create'
        unavailable = {'ok': False, 'result': None}
        try:
            request = validate_managed_method_request(method, params)
            args = request['args']
            schedule = args.get('schedule')
            daily = schedule == 'daily'
            calendar = schedule in ('weekly', 'monthly')
            once = schedule == 'once'
            expected = {'action','task_type','name','prompt','schedule'} | ({'scheduled_date'} if once else
                {'scheduled_time'} if daily else {'scheduled_time','scheduled_day'})
            day = args.get('scheduled_day')
            if (not (daily or calendar or once) or set(args) != expected
                    or args['action'] != 'create' or args['task_type'] != 'llm'
                    or (calendar and (type(day) is not int or day < (0 if schedule == 'weekly' else 1)
                                      or day > (6 if schedule == 'weekly' else 31)))
                    or not isinstance(args['name'], str) or not args['name'].strip() or len(args['name'].encode('utf-8')) > 200
                    or not isinstance(args['prompt'], str) or not args['prompt'].strip() or len(args['prompt'].encode('utf-8')) > 16384
                    or (not once and (not isinstance(args['scheduled_time'], str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', args['scheduled_time'])))
                    or (once and (not isinstance(args['scheduled_date'], str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', args['scheduled_date'])))):
                return unavailable
            if once:
                from datetime import datetime
                datetime.strptime(args['scheduled_date'], '%Y-%m-%dT%H:%M:%SZ')
            material = ['openclank.task-create.v3' if once else 'openclank.task-create.v1' if daily else 'openclank.task-create.v2', args['name'], args['prompt'],
                args['schedule'], args['scheduled_date'] if once else args['scheduled_time'], args['task_type']]
            if calendar: material.append(day)
            digest = hashlib.sha256(json.dumps(material, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
            if digest != request['argsSHA256']:
                return unavailable
            identity = {field: request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
            key = self._managed_turn_key(identity)
            async with self._managed_execution_lock:
                entry = self._managed_actor_turns.get(key)
                if not isinstance(entry, dict) or entry.get('state') != 'active' or entry.get('taskCreateClosing'): return unavailable
                owner = entry.get('owner'); account = entry.get('ownerAccountID') or entry.get('foregroundAccountID')
                policy = entry.get('policyRevision')
            if not isinstance(account, str) or not account: return unavailable
            decision = await self._handle_managed_tool_admit({**identity, 'toolName':'manage_tasks', 'kind':'tool', 'toolCallID':request['toolCallID']})
            if decision.get('allowed') is not True: return unavailable
            resolved = await self.resolve_managed_actor_execution(identity['sessionID'], identity['rootOperationID'], identity['actorID'], identity['executionRevision'])
            root_session = resolved.get('rootSessionID')
            if not isinstance(root_session, str) or not root_session: return unavailable
            binding = await self._handle_session_binding_read({'sessionID': resolved['rootSessionID']})
            manager = get_session_manager_instance(); state = getattr(manager, 'lifecycle_context', None) if manager else None
            auth = getattr(state, 'auth_manager', None); barrier = getattr(state, 'account_request_barrier', None)
            repository = getattr(state, 'files_policy_repository', None)
            supervisor = getattr(state, 'mimo_supervisor', None)
            pool = supervisor.owner_lifecycle_pool() if supervisor is not None else None
            worker = pool.worker_for_owner(owner) if pool else None
            holder = self._managed_provider_generation; generation = getattr(worker, 'installed_generation', None)
            chat = binding.get('stableChatID'); workspace_id = binding.get('authorityWorkspaceID')
            workspace = worker._bridge.mapped_session_workspace(chat) if worker and chat else None
            session = manager.get_session(chat) if manager and chat else None
            copal_workspace = binding.get('copalWorkspace')
            ceiling = entry.get('rawPolicyCeiling')
            allowed_tools = ceiling.get('allowedTools') if isinstance(ceiling, dict) else None
            route_id = getattr(session, 'provider_model_route_id', None) if session else None
            if (not auth or not barrier or not repository or not worker or not holder or type(generation) is not int or not workspace or not workspace_id
                    or not session or session.owner != owner or getattr(session, 'incognito', False) or getattr(session, 'archived', False)
                    or binding.get('owner') != owner or chat != entry.get('stableChatID')
                    or workspace_id != entry.get('authorityWorkspaceID') or not isinstance(route_id, str) or not route_id
                    or not isinstance(copal_workspace, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', copal_workspace)
                    or not isinstance(allowed_tools, list) or len(allowed_tools) > 256
                    or any(not isinstance(item, str) or not item for item in allowed_tools)): return unavailable
            provider_binding = self._managed_actor_provider_binding(identity, resolved)
            if provider_binding['model_route_id'] != route_id: return unavailable
            from src.tool_policy import known_tool_names
            allowed_tools = sorted(set(allowed_tools))
            if set(allowed_tools) - known_tool_names(): return unavailable
            from src.openclank.workspace_policy_service import resolve_owned_workspace
            files_binding = resolve_owned_workspace(repository, workspace_id=workspace_id, owner_username=owner,
                auth_manager=auth, purpose='agent_workspace', observation_only=True)
            if files_binding.workspace.id != workspace_id or files_binding.path != workspace: return unavailable
            source = {field: request[field] for field in ('messageID','actorID','rootOperationID','executionRevision','innerTurnID','toolCallID','toolName','argsSHA256')}
            source['stableChatID'] = chat
            if identity['sessionID'] != root_session: source['sourceSessionID'] = identity['sessionID']
            async with barrier.admitted(owner):
                if auth.account_id(owner) != account or auth.username_for_account_id(account) != owner: return unavailable
                response = await worker.session_task_create_source_request(chat,root_session,workspace,source,owner=owner)
                if response.status_code != 200 or response.json() != {'state':'current','argsSHA256':digest}: return unavailable
                async with self._durable_session_map.async_chat_admission_lock(chat, blocking=False, existing_only=True):
                    if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                        or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker or worker._bridge is not self
                        or not worker.is_alive() or self._managed_provider_generation != holder
                        or worker.installed_generation != generation or manager.get_session(chat) is not session
                        or session.owner != owner or getattr(session, 'incognito', False) or getattr(session, 'archived', False)
                        or getattr(session, 'provider_model_route_id', None) != route_id
                        or self._managed_actor_provider_binding(identity, resolved) != provider_binding
                        or await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']}) != binding): return unavailable
                    async with self._managed_execution_lock:
                        if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                                or entry.get('taskCreateClosing') or entry.get('policyRevision') != policy): return unavailable
                        if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                                or self._managed_actor_provider_binding(identity, resolved) != provider_binding
                                or resolve_owned_workspace(repository, workspace_id=workspace_id, owner_username=owner,
                                    auth_manager=auth, purpose='agent_workspace', observation_only=True) != files_binding): return unavailable
                        calls = entry.setdefault('taskCreateIssued', {})
                        call_id = request['toolCallID']
                        frozen = {'digest':digest, 'messageID':request['messageID'], 'toolName':request['toolName'],
                                  'copal_workspace':copal_workspace, 'allowed_tools':allowed_tools, 'route_id':route_id,
                                  'provider_binding':provider_binding}
                        prior = calls.get(call_id)
                        if prior is not None and prior != frozen: return unavailable
                        if status_only and prior is None: return unavailable
                        if prior is None and len(calls) >= 128: return unavailable
                        fresh = prior is None and not status_only
                        if fresh: calls[call_id] = frozen
                    # No await after issuance; missing after uncertainty stays unknown.
                    result = create_or_status(account=account, owner=owner, chat=chat, workspace_id=workspace_id,
                        copal_workspace=copal_workspace, allowed_tools=allowed_tools, route_id=route_id, provider_binding=provider_binding,
                        identity={**identity, 'messageID':request['messageID'], 'toolCallID':call_id, 'toolName':request['toolName']},
                        args=args, args_sha256=digest, allow_insert=fresh)
                return validate_managed_method_result(method, {'ok': True, 'result': result})
        except Exception:
            return unavailable

    async def _handle_managed_files_mutate(self, params):
        from src.openclank.managed_files_mutation import dispatch
        return await dispatch(self, params)

    async def _handle_managed_background_start(self, params):
        """Admit one exact native tool part into the existing shell-job owner."""
        from core.models import get_session_manager_instance
        from src import bg_jobs
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        from src.shell_policy import contained_argv, require_shell_approval, shell_command_argv
        from core.platform_compat import find_bash
        method='_openclank/session/v1/background-job/start'
        unavailable={'ok':False,'result':None}
        request=validate_managed_method_request(method,params)
        identity={field:request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
        key=self._managed_turn_key(identity); args=request['args']
        try:
            command=args['command']; runtime=args['max_runtime_s']
            if (set(args)!={'action','command','max_runtime_s'} or args['action']!='start'
                or type(command) is not str or not command or type(runtime) is not int or not 1<=runtime<=86400): return unavailable
            encoded=command.encode('utf-8')
            if len(encoded)>65536: return unavailable
            digest=hashlib.sha256(json.dumps(['openclank.bg-start.v1',command,runtime],ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
            if digest!=request['argsSHA256']: return unavailable
            async with self._managed_execution_lock:
                entry=self._managed_actor_turns.get(key)
                if not isinstance(entry,dict) or entry.get('state')!='active': return unavailable
                owner=entry.get('owner'); account=entry.get('ownerAccountID') or entry.get('foregroundAccountID'); policy=entry.get('policyRevision')
                admission=entry.get('backgroundStartAdmitted',{}).get(request['toolCallID'])
                if not isinstance(admission,dict): return unavailable
            if not isinstance(account,str) or not account: return unavailable
            decision=await self._handle_managed_tool_admit({**identity,'toolName':'manage_bg_jobs','kind':'tool','toolCallID':request['toolCallID']})
            if decision.get('allowed') is not True: return unavailable
            resolved=await self.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
            root_session = resolved.get('rootSessionID')
            if not isinstance(root_session, str) or not root_session: return unavailable
            binding=await self._handle_session_binding_read({'sessionID':root_session})
            manager=get_session_manager_instance(); state=getattr(manager,'lifecycle_context',None) if manager else None
            auth=getattr(state,'auth_manager',None); barrier=getattr(state,'account_request_barrier',None)
            supervisor=getattr(state,'mimo_supervisor',None)
            pool = supervisor.owner_lifecycle_pool() if supervisor is not None else None
            worker=pool.worker_for_owner(owner) if pool else None
            holder=self._managed_provider_generation; generation=getattr(worker,'installed_generation',None)
            chat=binding.get('stableChatID'); workspace_id=binding.get('authorityWorkspaceID')
            workspace=worker._bridge.mapped_session_workspace(chat) if worker and chat else None
            session=manager.get_session(chat) if manager and chat else None
            if (not auth or not barrier or not worker or not holder or type(generation) is not int or not workspace
                or not session or session.owner!=owner or getattr(session,'incognito',False) or getattr(session,'archived',False)
                or binding.get('owner')!=owner or binding.get('stableChatID')!=entry.get('stableChatID')
                or workspace_id!=entry.get('authorityWorkspaceID')): return unavailable
            frozen_binding=dict(binding)
            source={name:request[name] for name in ('messageID','actorID','rootOperationID','executionRevision','innerTurnID','toolCallID','toolName','argsSHA256')}
            source['stableChatID']=chat
            if identity['sessionID'] != root_session: source['sourceSessionID'] = identity['sessionID']
            async def current():
                if (auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
                    or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker or getattr(worker,'_bridge',None) is not self
                    or not worker.is_alive() or self._managed_provider_generation!=holder
                    or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder
                    or getattr(worker,'installed_generation',None)!=generation
                    or manager.get_session(chat) is not session or session.owner!=owner
                    or getattr(session,'incognito',False) or getattr(session,'archived',False)
                    or await self._handle_session_binding_read({'sessionID':root_session})!=frozen_binding): return False
                async with self._managed_execution_lock:
                    if self._managed_actor_turns.get(key) is not entry or entry.get('state')!='active' or entry.get('backgroundStartClosing') or entry.get('policyRevision')!=policy: return False
                response=await worker.session_background_start_source_request(chat,root_session,workspace,source,owner=owner)
                if (response.status_code!=200 or response.json()!={'state':'current','argsSHA256':digest}
                    or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
                    or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker or self._managed_provider_generation!=holder
                    or getattr(worker,'installed_generation',None)!=generation or not worker.is_alive()
                    or manager.get_session(chat) is not session or session.owner!=owner
                    or getattr(session,'incognito',False) or getattr(session,'archived',False)
                    or await self._handle_session_binding_read({'sessionID':root_session})!=frozen_binding): return False
                async with self._managed_execution_lock:
                    return (self._managed_actor_turns.get(key) is entry and entry.get('state')=='active'
                        and not entry.get('backgroundStartClosing') and entry.get('policyRevision')==policy)
            async def admitted_start():
                async with barrier.admitted(owner):
                    if not await current(): return unavailable
                    shell=find_bash() or (os.environ.get('ComSpec','cmd.exe') if os.name=='nt' else '/bin/bash')
                    network=os.getenv('OPEN_CLANK_SHELL_NETWORK','enabled')
                    _,containment=contained_argv(shell_command_argv(shell,command),workspace=workspace,cwd=workspace,network=network)
                    async def approval_progress(payload):
                        if (type(payload) is not dict or payload.get('type')!='permission_request'
                                or type(payload.get('data')) is not dict or not await current()):
                            raise ValueError('shell approval unavailable')
                        data=payload['data']; request_id=data.get('request_id')
                        if (type(request_id) is not str or not request_id.startswith('shell_perm_')
                                or data.get('session_id')!=chat or type(data.get('detail')) is not dict
                                or data.get('options')!=['once','chat','workspace','always','reject']):
                            raise ValueError('shell approval unavailable')
                        context=self._session_context.get(root_session) or {}
                        if context.get('interaction_policy')=='fail_on_interaction':
                            raise ValueError('shell approval interaction unavailable')
                        from src.openclank.transcript_projection import get_projection
                        projection=await asyncio.to_thread(get_projection,chat,owner=owner)
                        if (not projection or projection.get('mimo_session_id')!=root_session
                                or projection.get('active_turn_id')!=identity['rootOperationID'] or not await current()):
                            raise ValueError('shell approval projection changed')
                        async with self._managed_execution_lock:
                            if (self._managed_actor_turns.get(key) is not entry or entry.get('state')!='active'
                                    or entry.get('backgroundStartClosing') or entry.get('policyRevision')!=policy
                                    or entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is not admission):
                                raise ValueError('shell approval turn closed')
                            admission['shellApproval']=(request_id,owner,chat,account)
                        req=PermissionRequest(request_id=request_id,tool_call={},raw_input=data['detail'],
                            options=data['options'],title=str(data.get('permission_type') or ''),
                            session_id=root_session,odysseus_session_id=chat,owner=owner,
                            workspace=workspace,authority_workspace_id=workspace_id,
                            turn_id=identity['rootOperationID'],revision=int(projection.get('transcript_revision') or 0))
                        from src.shell_policy import current_shell_approval
                        from src.openclank.tui_interactions import ManagedShellApproval
                        pending = current_shell_approval(request_id, owner=owner, session_id=chat, account_id=account)
                        if pending is None: raise ValueError('shell approval unavailable')
                        source_identity = tuple((field, identity.get(field)) for field in
                            ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')) + tuple(
                            (field, request.get(field)) for field in ('messageID','toolCallID','toolName')) + (('jobID', None),)
                        evidence = ManagedShellApproval(owner, account, chat, req, pending, source_identity, current, policy, holder, generation)
                        async with self._managed_execution_lock:
                            if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                                    or entry.get('backgroundStartClosing') or entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is not admission
                                    or admission.get('shellApproval') != (request_id,owner,chat,account)):
                                raise ValueError('shell approval turn closed')
                            admission['shellApprovalEvidence'] = evidence
                        await self._surface_permission(req)
                        if not await current(): raise ValueError('shell approval authority changed')
                    try:
                        approval=await require_shell_approval(command,ctx={'owner':owner,'session_id':chat,'workspace':workspace,
                            'authority_workspace_id':workspace_id,'progress_cb':approval_progress,
                            'approval_account_id':account,'approval_current':current,'suppress_sudo_prompt':True},
                            cwd=workspace,containment=containment,network=network)
                    finally:
                        async with self._managed_execution_lock:
                            admission.pop('shellApproval',None)
                            admission.pop('shellApprovalEvidence',None)
                    if not await current(): return unavailable
                    bound={'account_id':account,'owner':owner,'session_id':chat,'engine_session_id':identity['sessionID'],'workspace':workspace,'workspace_id':workspace_id,
                        'root_operation_id':identity['rootOperationID'],'actor_id':identity['actorID'],'execution_revision':identity['executionRevision'],
                        'turn_id':identity['innerTurnID'],'message_id':request['messageID'],'tool_call_id':request['toolCallID'],'holder':holder,'generation':generation,'privacy':'normal'}
                    row=await asyncio.to_thread(bg_jobs.reserve_bound_shell,bound,command_sha256=hashlib.sha256(encoded).hexdigest(),runtime_s=runtime)
                    if row.get('retired_from_view') == 1:
                        return validate_managed_method_result(method,{'ok':True,'result':{'output':'Shell job was retired.','exit_code':0,'session':{'id':row['id'],'state':'retired'}}})
                    if row.get('status')!='reserved': return unavailable
                    if not await current(): return unavailable
                    row=await asyncio.to_thread(bg_jobs.launch,command,session_id=chat,owner=owner,workspace=workspace,cwd=workspace,
                        max_runtime_s=runtime,network=network,approval_binding=approval,tool_id='manage_bg_jobs',bound_reservation=row)
                    # A closing turn cannot revoke the factual result of an
                    # already admitted, durably recorded spawn. This return
                    # performs no further admission or process effect.
                    result={'ok':True,'result':{'output':f"Started shell session `{row['id']}`.",'exit_code':0,'session':{'id':row['id'],'state':row['status']}}}
                    return validate_managed_method_result(method,result)
            async with self._managed_execution_lock:
                if self._managed_actor_turns.get(key) is not entry or entry.get('state')!='active' or entry.get('backgroundStartClosing') or entry.get('policyRevision')!=policy:
                    return unavailable
                if entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is not admission: return unavailable
                material=(request['messageID'],request['toolName'],digest)
                if admission.get('state')=='admitted':
                    task=asyncio.create_task(admitted_start())
                    admission.update(state='started',material=material,task=task)
                    task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                elif admission.get('state')=='started' and admission.get('material')==material:
                    task=admission.get('task')
                    if not isinstance(task,asyncio.Task): return unavailable
                else: return unavailable
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            return unavailable

    async def _handle_managed_background_read(self, params):
        """Read exact v2 shell rows for a current, admitted managed tool part."""
        from core.models import get_session_manager_instance
        from src import bg_jobs
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/background-job/read'
        unavailable = {'ok': False, 'result': None}
        try:
            request = validate_managed_method_request(method, params)
            identity = {field: request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
            args = request['args']
            if type(args) is not dict or set(args) != {'action','job_id','cursor','limit','timeout_s'}:
                return unavailable
            action, job_id, cursor, limit, timeout = (args[k] for k in ('action','job_id','cursor','limit','timeout_s'))
            if (action not in ('list','poll','tail','wait') or type(cursor) is not int or cursor < 0
                    or type(limit) is not int or type(timeout) is not int or not 0 <= timeout <= 30):
                return unavailable
            if action == 'list':
                if job_id is not None or cursor != 0 or not 1 <= limit <= 32 or timeout != 0: return unavailable
            elif (type(job_id) is not str or not job_id or len(job_id) > 256):
                return unavailable
            elif action == 'tail':
                if not 1 <= limit <= 65536 or timeout != 0: return unavailable
            elif cursor != 0 or limit != 32 or (action == 'poll' and timeout != 0):
                return unavailable
            digest = hashlib.sha256(json.dumps(['openclank.bg-read.v1',action,job_id,cursor,limit,timeout],
                ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
            if digest != request['argsSHA256']: return unavailable
            key = self._managed_turn_key(identity)
            async with self._managed_execution_lock:
                entry = self._managed_actor_turns.get(key)
                if not isinstance(entry,dict) or entry.get('state') != 'active': return unavailable
                owner = entry.get('owner'); account = entry.get('ownerAccountID') or entry.get('foregroundAccountID')
                policy = entry.get('policyRevision')
                admission = entry.get('backgroundStartAdmitted',{}).get(request['toolCallID'])
                if not isinstance(admission,dict) or not isinstance(account,str) or not account: return unavailable
            decision = await self._handle_managed_tool_admit({**identity,'toolName':'manage_bg_jobs','kind':'tool','toolCallID':request['toolCallID']})
            if decision.get('allowed') is not True: return unavailable
            resolved = await self.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
            root_session = resolved.get('rootSessionID')
            if not isinstance(root_session, str) or not root_session: return unavailable
            binding = await self._handle_session_binding_read({'sessionID':root_session})
            manager = get_session_manager_instance(); state = getattr(manager,'lifecycle_context',None) if manager else None
            auth = getattr(state,'auth_manager',None); barrier = getattr(state,'account_request_barrier',None)
            supervisor = getattr(state,'mimo_supervisor',None)
            pool = supervisor.owner_lifecycle_pool() if supervisor is not None else None
            worker = pool.worker_for_owner(owner) if pool else None
            holder = self._managed_provider_generation; generation = getattr(worker,'installed_generation',None)
            chat = binding.get('stableChatID'); workspace_id = binding.get('authorityWorkspaceID')
            workspace = worker._bridge.mapped_session_workspace(chat) if worker and chat else None
            session = manager.get_session(chat) if manager and chat else None
            if (not auth or not barrier or not worker or not holder or type(generation) is not int or not workspace
                    or not session or session.owner != owner or getattr(session,'incognito',False) or getattr(session,'archived',False)
                    or binding.get('owner') != owner or chat != entry.get('stableChatID')
                    or workspace_id != entry.get('authorityWorkspaceID')
                    or getattr(session,'workspace_id',None) != workspace_id): return unavailable
            frozen_binding = dict(binding)
            source = {name:request[name] for name in ('messageID','actorID','rootOperationID','executionRevision','innerTurnID','toolCallID','toolName','argsSHA256')}
            source['stableChatID'] = chat
            if identity['sessionID'] != root_session: source['sourceSessionID'] = identity['sessionID']
            async def current():
                if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                        or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker or getattr(worker,'_bridge',None) is not self
                        or not worker.is_alive() or self._managed_provider_generation != holder
                        or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) != holder
                        or getattr(worker,'installed_generation',None) != generation
                        or manager.get_session(chat) is not session or session.owner != owner
                        or getattr(session,'workspace_id',None) != workspace_id
                        or getattr(session,'incognito',False) or getattr(session,'archived',False)
                        or await self._handle_session_binding_read({'sessionID':root_session}) != frozen_binding): return False
                async with self._managed_execution_lock:
                    if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                            or entry.get('backgroundStartClosing') or entry.get('policyRevision') != policy
                            or entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is not admission): return False
                response = await worker.session_background_read_source_request(chat,root_session,workspace,source,owner=owner)
                if response.status_code != 200 or response.json() != {'state':'current','argsSHA256':digest}: return False
                async with self._managed_execution_lock:
                    live = (self._managed_actor_turns.get(key) is entry and entry.get('state') == 'active'
                            and not entry.get('backgroundStartClosing') and entry.get('policyRevision') == policy
                            and entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is admission)
                return (live and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                        and supervisor.owner_lifecycle_pool() is pool and pool.worker_for_owner(owner) is worker and getattr(worker,'_bridge',None) is self
                        and worker.is_alive() and self._managed_provider_generation == holder
                        and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) == holder
                        and getattr(worker,'installed_generation',None) == generation
                        and manager.get_session(chat) is session and session.owner == owner
                        and getattr(session,'workspace_id',None) == workspace_id
                        and not getattr(session,'incognito',False) and not getattr(session,'archived',False)
                        and await self._handle_session_binding_read({'sessionID':root_session}) == frozen_binding)
            scope = dict(account_id=account,owner=owner,session_id=chat,workspace_id=workspace_id,workspace=workspace)
            async with barrier.admitted(owner):
                if not await current(): return unavailable
                result = {'action':action,'items':None,'job':None,'frame':None,'timed_out':None,'partial':None}
                if action == 'list':
                    page = await asyncio.to_thread(bg_jobs.bound_shell_page,**scope,limit=limit)
                    items = []
                    for item in page:
                        items.append(await asyncio.to_thread(bg_jobs.bound_shell_status,item['jobID'],**scope))
                    result.update(items=items,partial=len(page) == limit)
                elif action == 'tail':
                    frame = await asyncio.to_thread(bg_jobs.bound_shell_tail,job_id,**scope,cursor=cursor,limit=limit)
                    status = await asyncio.to_thread(bg_jobs.bound_shell_status,job_id,**scope)
                    frame['status'] = status['status']; result['frame'] = frame
                else:
                    deadline = time.monotonic() + timeout
                    while True:
                        status = await asyncio.to_thread(bg_jobs.bound_shell_status,job_id,**scope)
                        if not await current(): return unavailable
                        if action == 'poll' or status['status'] not in ('reserved','spawning','running','reconciling'):
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0: break
                        await asyncio.sleep(min(0.25,remaining))
                        if not await current(): return unavailable
                    result['job'] = status
                    if action == 'wait' and status['status'] != 'unknown':
                        result['timed_out'] = status['status'] in ('reserved','spawning','running','reconciling')
                if not await current(): return unavailable
                return validate_managed_method_result(method,{'ok':True,'result':result})
        except asyncio.CancelledError:
            raise
        except Exception:
            return unavailable

    async def _handle_managed_background_terminate(self, params):
        """Issue one exact control; acknowledgement never means child exit."""
        from core.models import get_session_manager_instance
        from src import bg_jobs
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/background-job/terminate'
        unavailable = {'ok':False,'result':None}
        try:
            request = validate_managed_method_request(method,params)
            identity = {field:request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
            args = request['args']
            if (type(args) is not dict or set(args) != {'action','job_id'} or args.get('action') != 'terminate'
                    or type(args.get('job_id')) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',args['job_id'])):
                return unavailable
            job_id = args['job_id']
            digest = hashlib.sha256(json.dumps(['openclank.bg-terminate.v1',job_id],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            if request['argsSHA256'] != digest: return unavailable
            key = self._managed_turn_key(identity)
            async with self._managed_execution_lock:
                entry = self._managed_actor_turns.get(key)
                if not isinstance(entry,dict) or entry.get('state') != 'active': return unavailable
                owner = entry.get('owner'); account = entry.get('ownerAccountID') or entry.get('foregroundAccountID')
                policy = entry.get('policyRevision')
                admission = entry.get('backgroundStartAdmitted',{}).get(request['toolCallID'])
                if not isinstance(admission,dict) or not isinstance(account,str) or not account: return unavailable
            decision = await self._handle_managed_tool_admit({**identity,'toolName':'manage_bg_jobs','kind':'tool','toolCallID':request['toolCallID']})
            if decision.get('allowed') is not True: return unavailable
            resolved = await self.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
            root_session = resolved.get('rootSessionID')
            if not isinstance(root_session, str) or not root_session: return unavailable
            binding = await self._handle_session_binding_read({'sessionID':root_session})
            manager = get_session_manager_instance(); state = getattr(manager,'lifecycle_context',None) if manager else None
            auth = getattr(state,'auth_manager',None); barrier = getattr(state,'account_request_barrier',None)
            supervisor = getattr(state,'mimo_supervisor',None)
            pool = supervisor.owner_lifecycle_pool() if supervisor is not None else None
            worker = pool.worker_for_owner(owner) if pool else None
            holder = self._managed_provider_generation; generation = getattr(worker,'installed_generation',None)
            chat = binding.get('stableChatID'); workspace_id = binding.get('authorityWorkspaceID')
            workspace = worker._bridge.mapped_session_workspace(chat) if worker and chat else None
            session = manager.get_session(chat) if manager and chat else None
            if (not auth or not barrier or not worker or not holder or type(generation) is not int or not workspace
                    or not session or session.owner != owner or getattr(session,'incognito',False) or getattr(session,'archived',False)
                    or binding.get('owner') != owner or chat != entry.get('stableChatID')
                    or workspace_id != entry.get('authorityWorkspaceID')
                    or getattr(session,'workspace_id',None) != workspace_id): return unavailable
            frozen_binding = dict(binding)
            source = {name:request[name] for name in ('messageID','actorID','rootOperationID','executionRevision','innerTurnID','toolCallID','toolName','argsSHA256')}
            source['stableChatID'] = chat
            if identity['sessionID'] != root_session: source['sourceSessionID'] = identity['sessionID']
            async def current():
                if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                        or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker or getattr(worker,'_bridge',None) is not self
                        or not worker.is_alive() or self._managed_provider_generation != holder
                        or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) != holder
                        or getattr(worker,'installed_generation',None) != generation
                        or manager.get_session(chat) is not session or session.owner != owner
                        or getattr(session,'workspace_id',None) != workspace_id
                        or getattr(session,'incognito',False) or getattr(session,'archived',False)
                        or await self._handle_session_binding_read({'sessionID':root_session}) != frozen_binding): return False
                async with self._managed_execution_lock:
                    if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                            or entry.get('backgroundStartClosing') or entry.get('policyRevision') != policy
                            or entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is not admission): return False
                response = await worker.session_background_terminate_source_request(chat,root_session,workspace,source,owner=owner)
                if response.status_code != 200 or response.json() != {'state':'current','argsSHA256':digest}: return False
                async with self._managed_execution_lock:
                    live = (self._managed_actor_turns.get(key) is entry and entry.get('state') == 'active'
                            and not entry.get('backgroundStartClosing') and entry.get('policyRevision') == policy
                            and entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is admission)
                return (live and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                        and supervisor.owner_lifecycle_pool() is pool and pool.worker_for_owner(owner) is worker and getattr(worker,'_bridge',None) is self
                        and worker.is_alive() and self._managed_provider_generation == holder
                        and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) == holder
                        and getattr(worker,'installed_generation',None) == generation
                        and manager.get_session(chat) is session and session.owner == owner
                        and getattr(session,'workspace_id',None) == workspace_id
                        and not getattr(session,'incognito',False) and not getattr(session,'archived',False)
                        and await self._handle_session_binding_read({'sessionID':root_session}) == frozen_binding)
            scope = dict(account_id=account,owner=owner,session_id=chat,workspace_id=workspace_id,workspace=workspace)
            control_identity = {field:request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID','messageID','toolCallID','toolName')}
            async def issue():
                async with barrier.admitted(owner):
                    if not await current(): return unavailable
                    try:
                        outcome = await asyncio.to_thread(bg_jobs.request_bound_shell_terminate,job_id,**scope,
                            control_identity=control_identity,args_sha256=digest)
                    except bg_jobs.JobStoreError:
                        outcome = 'unavailable'
                    except Exception:
                        outcome = 'unknown'
                    # Once issued, a changed source cannot erase the durable fact.
                    try:
                        if not await current(): outcome = 'unknown'
                    except Exception:
                        outcome = 'unknown'
                    return validate_managed_method_result(method,{'ok':True,'result':{'state':outcome}})
            async with self._managed_execution_lock:
                if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                        or entry.get('backgroundStartClosing') or entry.get('policyRevision') != policy
                        or entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is not admission): return unavailable
                material = (request['messageID'],request['toolName'],digest)
                if admission.get('state') == 'admitted':
                    task = asyncio.create_task(issue())
                    admission.update(state='started',material=material,task=task)
                    task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                elif admission.get('state') == 'started' and admission.get('material') == material:
                    task = admission.get('task')
                    if not isinstance(task,asyncio.Task): return unavailable
                else: return unavailable
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            return unavailable

    async def _handle_managed_background_write(self, params):
        """Send one bounded opaque input; acceptance never means child exit."""
        from core.models import get_session_manager_instance
        from src import bg_jobs
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/background-job/write'
        unavailable = {'ok':False,'result':None}
        try:
            request = validate_managed_method_request(method,params)
            identity = {field:request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
            args = request['args']
            if (type(args) is not dict or set(args) != {'action','job_id','data'} or args.get('action') != 'write'
                    or type(args.get('job_id')) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',args['job_id'])
                    or type(args.get('data')) is not str): return unavailable
            job_id=args['job_id'];payload=args['data'].encode('utf-8','strict')
            if not 1<=len(payload)<=16384:return unavailable
            payload_hash=hashlib.sha256(payload).hexdigest()
            digest=hashlib.sha256(json.dumps(['openclank.bg-write.v1',job_id,payload_hash,len(payload)],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            if request['argsSHA256'] != digest: return unavailable
            key = self._managed_turn_key(identity)
            async with self._managed_execution_lock:
                entry = self._managed_actor_turns.get(key)
                if not isinstance(entry,dict) or entry.get('state') != 'active': return unavailable
                owner = entry.get('owner'); account = entry.get('ownerAccountID') or entry.get('foregroundAccountID')
                policy = entry.get('policyRevision')
                admission = entry.get('backgroundStartAdmitted',{}).get(request['toolCallID'])
                if not isinstance(admission,dict) or not isinstance(account,str) or not account: return unavailable
            decision = await self._handle_managed_tool_admit({**identity,'toolName':'manage_bg_jobs','kind':'tool','toolCallID':request['toolCallID']})
            if decision.get('allowed') is not True: return unavailable
            resolved = await self.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
            root_session = resolved.get('rootSessionID')
            if not isinstance(root_session, str) or not root_session: return unavailable
            binding = await self._handle_session_binding_read({'sessionID':root_session})
            manager = get_session_manager_instance(); state = getattr(manager,'lifecycle_context',None) if manager else None
            auth = getattr(state,'auth_manager',None); barrier = getattr(state,'account_request_barrier',None)
            supervisor = getattr(state,'mimo_supervisor',None)
            pool = supervisor.owner_lifecycle_pool() if supervisor is not None else None
            worker = pool.worker_for_owner(owner) if pool else None
            holder = self._managed_provider_generation; generation = getattr(worker,'installed_generation',None)
            chat = binding.get('stableChatID'); workspace_id = binding.get('authorityWorkspaceID')
            workspace = worker._bridge.mapped_session_workspace(chat) if worker and chat else None
            session = manager.get_session(chat) if manager and chat else None
            if (not auth or not barrier or not worker or not holder or type(generation) is not int or not workspace
                    or not session or session.owner != owner or getattr(session,'incognito',False) or getattr(session,'archived',False)
                    or binding.get('owner') != owner or chat != entry.get('stableChatID')
                    or workspace_id != entry.get('authorityWorkspaceID')
                    or getattr(session,'workspace_id',None) != workspace_id): return unavailable
            frozen_binding = dict(binding)
            source = {name:request[name] for name in ('messageID','actorID','rootOperationID','executionRevision','innerTurnID','toolCallID','toolName','argsSHA256')}
            source['stableChatID'] = chat
            if identity['sessionID'] != root_session: source['sourceSessionID'] = identity['sessionID']
            async def current():
                if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                        or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker or getattr(worker,'_bridge',None) is not self
                        or not worker.is_alive() or self._managed_provider_generation != holder
                        or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) != holder
                        or getattr(worker,'installed_generation',None) != generation
                        or manager.get_session(chat) is not session or session.owner != owner
                        or getattr(session,'workspace_id',None) != workspace_id
                        or getattr(session,'incognito',False) or getattr(session,'archived',False)
                        or await self._handle_session_binding_read({'sessionID':root_session}) != frozen_binding): return False
                async with self._managed_execution_lock:
                    if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                            or entry.get('backgroundStartClosing') or entry.get('policyRevision') != policy
                            or entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is not admission): return False
                response = await worker.session_background_write_source_request(chat,root_session,workspace,source,owner=owner)
                if response.status_code != 200 or response.json() != {'state':'current','argsSHA256':digest}: return False
                async with self._managed_execution_lock:
                    live = (self._managed_actor_turns.get(key) is entry and entry.get('state') == 'active'
                            and not entry.get('backgroundStartClosing') and entry.get('policyRevision') == policy
                            and entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is admission)
                return (live and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                        and supervisor.owner_lifecycle_pool() is pool and pool.worker_for_owner(owner) is worker and getattr(worker,'_bridge',None) is self
                        and worker.is_alive() and self._managed_provider_generation == holder
                        and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) == holder
                        and getattr(worker,'installed_generation',None) == generation
                        and manager.get_session(chat) is session and session.owner == owner
                        and getattr(session,'workspace_id',None) == workspace_id
                        and not getattr(session,'incognito',False) and not getattr(session,'archived',False)
                        and await self._handle_session_binding_read({'sessionID':root_session}) == frozen_binding)
            scope = dict(account_id=account,owner=owner,session_id=chat,workspace_id=workspace_id,workspace=workspace)
            control_identity = {field:request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID','messageID','toolCallID','toolName')}
            async def issue():
                async with barrier.admitted(owner):
                    if not await current(): return unavailable
                    try:
                        prior=await asyncio.to_thread(bg_jobs.bound_shell_input_status,job_id,**scope,control_identity=control_identity,args_sha256=digest,payload_sha256=payload_hash,length=len(payload))
                        if prior is not None:
                            return validate_managed_method_result(method,{'ok':True,'result':{'state':prior}}) if await current() else unavailable
                        nonce_hash=await asyncio.to_thread(bg_jobs.bound_shell_input_target,job_id,**scope)
                        approval_binding=hashlib.sha256(json.dumps(['openclank.bg-input-approval.v1',account,job_id,nonce_hash,control_identity,payload_hash,len(payload)],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                        async def approval_progress(value):
                            if (type(value) is not dict or value.get('type')!='permission_request' or type(value.get('data')) is not dict or not await current()): raise ValueError('shell input approval unavailable')
                            data=value['data'];request_id=data.get('request_id')
                            if (type(request_id) is not str or not request_id.startswith('shell_perm_') or data.get('session_id')!=chat or type(data.get('detail')) is not dict or data.get('options')!=['once','reject']): raise ValueError('shell input approval unavailable')
                            context=self._session_context.get(root_session) or {}
                            if context.get('interaction_policy')=='fail_on_interaction':raise ValueError('shell approval interaction unavailable')
                            from src.openclank.transcript_projection import get_projection
                            projection=await asyncio.to_thread(get_projection,chat,owner=owner)
                            if (not projection or projection.get('mimo_session_id')!=root_session or projection.get('active_turn_id')!=identity['rootOperationID'] or not await current()): raise ValueError('shell approval projection changed')
                            async with self._managed_execution_lock:
                                if (self._managed_actor_turns.get(key) is not entry or entry.get('state')!='active' or entry.get('backgroundStartClosing') or entry.get('policyRevision')!=policy or entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is not admission): raise ValueError('shell approval turn closed')
                                admission['shellApproval']=(request_id,owner,chat,account)
                            req=PermissionRequest(request_id=request_id,tool_call={},raw_input=data['detail'],options=data['options'],title=str(data.get('permission_type') or ''),session_id=root_session,odysseus_session_id=chat,owner=owner,workspace=workspace,authority_workspace_id=workspace_id,turn_id=identity['rootOperationID'],revision=int(projection.get('transcript_revision') or 0))
                            from src.shell_policy import current_shell_approval
                            from src.openclank.tui_interactions import ManagedShellApproval
                            pending=current_shell_approval(request_id,owner=owner,session_id=chat,account_id=account)
                            if pending is None:raise ValueError('shell approval unavailable')
                            source_identity=tuple((field,identity.get(field)) for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')) + tuple((field,request.get(field)) for field in ('messageID','toolCallID','toolName')) + (('jobID',job_id),)
                            evidence=ManagedShellApproval(owner,account,chat,req,pending,source_identity,current,policy,holder,generation)
                            async with self._managed_execution_lock:
                                if (self._managed_actor_turns.get(key) is not entry or entry.get('state')!='active'
                                        or entry.get('backgroundStartClosing') or entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is not admission
                                        or admission.get('shellApproval')!=(request_id,owner,chat,account)):raise ValueError('shell approval turn closed')
                                admission['shellApprovalEvidence']=evidence
                            await self._surface_permission(req)
                            if not await current():raise ValueError('shell approval authority changed')
                        from src.shell_policy import require_shell_stdin_approval
                        try:
                            await require_shell_stdin_approval(ctx={'owner':owner,'session_id':chat,'workspace':workspace,'authority_workspace_id':workspace_id,'progress_cb':approval_progress,'approval_account_id':account,'approval_current':current},binding=approval_binding,length=len(payload))
                        finally:
                            async with self._managed_execution_lock:
                                admission.pop('shellApproval',None)
                                admission.pop('shellApprovalEvidence',None)
                        if not await current():return unavailable
                        from contextlib import contextmanager
                        @contextmanager
                        def issue_fence():
                            with self._stdin_issue_lock:
                                if (self._managed_actor_turns.get(key) is not entry or entry.get('state')!='active'
                                    or entry.get('backgroundStartClosing') or entry.get('policyRevision')!=policy
                                    or entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is not admission
                                    or self._managed_provider_generation!=holder or getattr(self,'_ownerAdmissionHolderStopped',False)
                                    or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker
                                    or getattr(worker,'_bridge',None) is not self or not worker.is_alive()
                                    or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder
                                    or getattr(worker,'installed_generation',None)!=generation): raise bg_jobs.JobStoreError('bound input unavailable')
                                yield
                        outcome = await asyncio.to_thread(bg_jobs.request_bound_shell_input,job_id,**scope,
                            control_identity=control_identity,args_sha256=digest,payload=payload,expected_nonce_hash=nonce_hash,issue_fence=issue_fence)
                    except bg_jobs.JobStoreError:
                        outcome = 'unavailable'
                    except Exception:
                        outcome = 'unknown'
                    # Once issued, a changed source cannot erase the durable fact.
                    try:
                        if not await current(): outcome = 'unknown'
                    except Exception:
                        outcome = 'unknown'
                    return validate_managed_method_result(method,{'ok':True,'result':{'state':outcome}})
            async with self._managed_execution_lock:
                if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                        or entry.get('backgroundStartClosing') or entry.get('policyRevision') != policy
                        or entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is not admission): return unavailable
                material = (request['messageID'],request['toolName'],digest)
                if admission.get('state') == 'admitted':
                    task = asyncio.create_task(issue())
                    admission.update(state='started',material=material,task=task)
                    task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                elif admission.get('state') == 'started' and admission.get('material') == material:
                    task = admission.get('task')
                    if not isinstance(task,asyncio.Task): return unavailable
                else: return unavailable
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            return unavailable

    async def _handle_managed_background_retire(self, params):
        """Retire one exact settled view; preserve row and sidecars."""
        from core.models import get_session_manager_instance
        from src import bg_jobs
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/background-job/retire'
        unavailable = {'ok':False,'result':None}
        try:
            request = validate_managed_method_request(method,params)
            identity = {field:request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
            args = request['args']
            if (type(args) is not dict or set(args) != {'action','job_id'} or args.get('action') != 'retire'
                    or type(args.get('job_id')) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',args['job_id'])):
                return unavailable
            job_id = args['job_id']
            digest = hashlib.sha256(json.dumps(['openclank.bg-retire.v1',job_id],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            if request['argsSHA256'] != digest: return unavailable
            key = self._managed_turn_key(identity)
            async with self._managed_execution_lock:
                entry = self._managed_actor_turns.get(key)
                if not isinstance(entry,dict) or entry.get('state') != 'active': return unavailable
                owner = entry.get('owner'); account = entry.get('ownerAccountID') or entry.get('foregroundAccountID')
                policy = entry.get('policyRevision')
                admission = entry.get('backgroundStartAdmitted',{}).get(request['toolCallID'])
                if not isinstance(admission,dict) or not isinstance(account,str) or not account: return unavailable
            decision = await self._handle_managed_tool_admit({**identity,'toolName':'manage_bg_jobs','kind':'tool','toolCallID':request['toolCallID']})
            if decision.get('allowed') is not True: return unavailable
            resolved = await self.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
            root_session = resolved.get('rootSessionID')
            if not isinstance(root_session, str) or not root_session: return unavailable
            binding = await self._handle_session_binding_read({'sessionID':root_session})
            manager = get_session_manager_instance(); state = getattr(manager,'lifecycle_context',None) if manager else None
            auth = getattr(state,'auth_manager',None); barrier = getattr(state,'account_request_barrier',None)
            supervisor = getattr(state,'mimo_supervisor',None)
            pool = supervisor.owner_lifecycle_pool() if supervisor is not None else None
            worker = pool.worker_for_owner(owner) if pool else None
            holder = self._managed_provider_generation; generation = getattr(worker,'installed_generation',None)
            chat = binding.get('stableChatID'); workspace_id = binding.get('authorityWorkspaceID')
            workspace = worker._bridge.mapped_session_workspace(chat) if worker and chat else None
            session = manager.get_session(chat) if manager and chat else None
            if (not auth or not barrier or not worker or not holder or type(generation) is not int or not workspace
                    or not session or session.owner != owner or getattr(session,'incognito',False) or getattr(session,'archived',False)
                    or binding.get('owner') != owner or chat != entry.get('stableChatID')
                    or workspace_id != entry.get('authorityWorkspaceID')
                    or getattr(session,'workspace_id',None) != workspace_id): return unavailable
            frozen_binding = dict(binding)
            source = {name:request[name] for name in ('messageID','actorID','rootOperationID','executionRevision','innerTurnID','toolCallID','toolName','argsSHA256')}
            source['stableChatID'] = chat
            if identity['sessionID'] != root_session: source['sourceSessionID'] = identity['sessionID']
            async def current():
                if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                        or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker or getattr(worker,'_bridge',None) is not self
                        or not worker.is_alive() or self._managed_provider_generation != holder
                        or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) != holder
                        or getattr(worker,'installed_generation',None) != generation
                        or manager.get_session(chat) is not session or session.owner != owner
                        or getattr(session,'workspace_id',None) != workspace_id
                        or getattr(session,'incognito',False) or getattr(session,'archived',False)
                        or await self._handle_session_binding_read({'sessionID':root_session}) != frozen_binding): return False
                async with self._managed_execution_lock:
                    if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                            or entry.get('backgroundStartClosing') or entry.get('policyRevision') != policy
                            or entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is not admission): return False
                response = await worker.session_background_retire_source_request(chat,root_session,workspace,source,owner=owner)
                if response.status_code != 200 or response.json() != {'state':'current','argsSHA256':digest}: return False
                async with self._managed_execution_lock:
                    live = (self._managed_actor_turns.get(key) is entry and entry.get('state') == 'active'
                            and not entry.get('backgroundStartClosing') and entry.get('policyRevision') == policy
                            and entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is admission)
                return (live and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                        and supervisor.owner_lifecycle_pool() is pool and pool.worker_for_owner(owner) is worker and getattr(worker,'_bridge',None) is self
                        and worker.is_alive() and self._managed_provider_generation == holder
                        and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) == holder
                        and getattr(worker,'installed_generation',None) == generation
                        and manager.get_session(chat) is session and session.owner == owner
                        and getattr(session,'workspace_id',None) == workspace_id
                        and not getattr(session,'incognito',False) and not getattr(session,'archived',False)
                        and await self._handle_session_binding_read({'sessionID':root_session}) == frozen_binding)
            scope = dict(account_id=account,owner=owner,session_id=chat,workspace_id=workspace_id,workspace=workspace)
            control_identity = {field:request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID','messageID','toolCallID','toolName')}
            async def issue():
                async with barrier.admitted(owner):
                    if not await current(): return unavailable
                    try:
                        outcome = await asyncio.to_thread(bg_jobs.retire_bound_shell,job_id,**scope)
                    except bg_jobs.JobStoreError:
                        outcome = 'unavailable'
                    except Exception:
                        outcome = 'unknown'
                    # Once issued, a changed source cannot erase the durable fact.
                    try:
                        if not await current(): outcome = 'unknown'
                    except Exception:
                        outcome = 'unknown'
                    return validate_managed_method_result(method,{'ok':True,'result':{'state':outcome}})
            async with self._managed_execution_lock:
                if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                        or entry.get('backgroundStartClosing') or entry.get('policyRevision') != policy
                        or entry.get('backgroundStartAdmitted',{}).get(request['toolCallID']) is not admission): return unavailable
                material = (request['messageID'],request['toolName'],digest)
                if admission.get('state') == 'admitted':
                    task = asyncio.create_task(issue())
                    admission.update(state='started',material=material,task=task)
                    task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                elif admission.get('state') == 'started' and admission.get('material') == material:
                    task = admission.get('task')
                    if not isinstance(task,asyncio.Task): return unavailable
                else: return unavailable
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            return unavailable

    async def _handle_managed_python_kernel_call(self, params):
        if not self._python_kernel_available():
            raise ValueError('managed Python kernel unavailable')
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        from src.openclank.python_kernel import KernelScope
        from src.openclank.python_kernel_protocol import project_model_result
        from src.openclank.python_kernel_artifacts import publish_kernel_png
        from src.openclank.files_facade import ProviderContext
        from types import SimpleNamespace
        import hashlib
        method = '_openclank/session/v1/python-kernel/call'
        request = validate_managed_method_request(method, params)
        identity = {name: request[name] for name in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
        key = self._managed_turn_key(identity); call_id = request['callID']
        args = request['request']; operation = args['operation']
        async with self._managed_execution_lock:
            entry = self._managed_actor_turns.get(key)
            if not isinstance(entry, dict) or entry.get('state') != 'active': raise ValueError('kernel turn unavailable')
            owner, account, policy = entry.get('owner'), entry.get('ownerAccountID'), entry.get('policyRevision')
            calls = entry.setdefault('pythonKernelCalls', {})
            if len(calls) >= 128 and call_id not in calls: raise ValueError('kernel call limit reached')
            if operation == 'cancel':
                record = calls.setdefault(call_id, {'cancelled':True})
                record['cancelled'] = True
                reservation = record.get('reservation'); kernels = record.get('manager'); task = record.get('task')
                if reservation is not None: kernels.revoke_call(reservation)
        if operation == 'cancel':
            if reservation is not None:
                await self._drain_python_kernel_call(kernels, reservation)
            if task is not None:
                try: await asyncio.shield(task)
                except Exception: pass
            return validate_managed_method_result(method, {'ok':True,'result':None})
        decision = await self._handle_managed_tool_admit({**identity, 'toolName':'python', 'kind':'tool'})
        if decision.get('allowed') is not True or not isinstance(account,str) or not account: raise ValueError('kernel authority unavailable')
        resolved = await self.resolve_managed_actor_execution(identity['sessionID'], identity['rootOperationID'], identity['actorID'], identity['executionRevision'])
        binding = await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']})
        manager = get_session_manager_instance(); state = getattr(manager, 'lifecycle_context', None) if manager is not None else None
        auth = getattr(state, 'auth_manager', None); barrier = getattr(state, 'account_request_barrier', None)
        kernels = getattr(state, 'python_kernel_manager', None); supervisor = getattr(state, 'mimo_supervisor', None)
        pool = supervisor.owner_lifecycle_pool() if supervisor is not None else None
        worker = pool.worker_for_owner(owner) if pool else None
        holder = self._managed_provider_generation; generation = getattr(worker, 'installed_generation', None)
        if (kernels is None or barrier is None or auth is None or worker is None or getattr(worker,'_bridge',None) is not self
            or not holder or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) != holder
            or type(generation) is not int or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
            or binding.get('owner') != owner or binding.get('stableChatID') != entry.get('stableChatID')
            or binding.get('authorityWorkspaceID') != entry.get('authorityWorkspaceID')): raise ValueError('kernel scope unavailable')
        scope = KernelScope(account, binding['authorityWorkspaceID'], binding['stableChatID'], identity['sessionID'], identity['actorID'], holder, generation)
        frozen_binding = {name:binding[name] for name in ('engineSessionID','stableChatID','owner','canonicalCwd','authorityWorkspaceID','mappingRevision')}
        call_args = {name:value for name,value in args.items() if name != 'operation'}
        artifact_output = call_args.pop('artifact_output', None)
        if artifact_output is not None:
            name = artifact_output['name']
            if (operation != 'execute' or not name.lower().endswith('.png') or name in {'.','..'} or not name.strip() == name
                or len(name.encode('utf-8')) > 240 or any(c in name for c in ('/','\\','\x00'))):
                raise ValueError('kernel artifact destination unavailable')
        if operation == 'start': call_args['directory'] = binding['canonicalCwd']
        async with self._managed_execution_lock:
            if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active' or entry.get('pythonKernelClosing')
                or entry.get('ownerAccountID') != account or entry.get('policyRevision') != policy or call_id in calls
                or self._kernel_retired or identity['sessionID'] in self._kernel_retired_sessions
                or scope.chat_id in self._kernel_retired_chats): raise ValueError('kernel turn changed')
            reservation = kernels.reserve_call(scope,call_id,turn_key=json.dumps(key,separators=(',',':'),ensure_ascii=True))
            record = {'reservation':reservation,'manager':kernels,'cancelled':False,'completed':False}
            calls[call_id] = record; self._kernel_scopes.add(scope)
            async def execute():
                async with barrier.admitted(owner):
                    current_binding = await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']})
                    if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                        or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker
                        or self._managed_provider_generation != holder or getattr(worker,'installed_generation',None) != generation
                        or any(current_binding.get(name)!=value for name,value in frozen_binding.items())):
                        raise ValueError('kernel authority changed')
                    async with self._managed_execution_lock:
                        if self._managed_actor_turns.get(key) is not entry or entry.get('pythonKernelClosing') or record['cancelled']:
                            raise ValueError('kernel call revoked')
                    async def publication_current():
                        try:
                            current_binding = await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']})
                            async with self._managed_execution_lock:
                                active = (self._managed_actor_turns.get(key) is entry and entry.get('state') == 'active'
                                    and not entry.get('pythonKernelClosing') and not record['cancelled'] and not reservation.revoked
                                    and entry.get('ownerAccountID') == account and entry.get('policyRevision') == policy)
                            return bool(active and all(current_binding.get(n) == v for n,v in frozen_binding.items())
                                and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                                and supervisor.owner_lifecycle_pool() is pool and pool.worker_for_owner(owner) is worker
                                and self._managed_provider_generation == holder and getattr(worker,'installed_generation',None) == generation
                                and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) == holder
                                and (artifact_output is None or repository.generation() == files_generation))
                        except Exception:
                            return False
                    destination = facade = context = store = None
                    if artifact_output is not None:
                        repository = getattr(state,'files_policy_repository',None)
                        factory = getattr(state,'files_facade_factory',None)
                        store = getattr(getattr(state,'account_owner_lifecycle',None),'artifact_store',None)
                        if repository is None or not callable(factory) or store is None: raise ValueError('kernel artifact authority unavailable')
                        files_generation = repository.generation()
                        context = ProviderContext(account,owner,files_generation,bool(auth.is_admin(owner)),binding['authorityWorkspaceID'])
                        facade = factory(SimpleNamespace(app=SimpleNamespace(state=state)),context)
                        destination = await facade.capture_media_destination(context,resource_ref=artifact_output['destination_ref'])
                        if not await publication_current(): raise ValueError('kernel artifact authority changed')
                        call_args['artifact_enabled'] = True
                    value = await asyncio.to_thread(kernels.invoke_reserved,reservation,operation,**call_args)
                    payload = value.pop('_artifact_payload', None)
                    if artifact_output is not None:
                        value['output_scope'] = 'python_text_and_artifacts'
                        value.setdefault('artifact_status', 'none')
                    if payload is not None:
                        operation_id = 'pykernel-' + hashlib.sha256((scope.account_id + '\0' + scope.chat_id + '\0' + reservation.turn_key + '\0' + call_id).encode('utf-8')).hexdigest()
                        value.update(await publish_kernel_png(encoded=payload,store=store,account=account,facade=facade,context=context,
                            destination=destination,name=artifact_output['name'],operation_id=operation_id,current=publication_current))
                    return value
            task = asyncio.create_task(execute()); record['task'] = task
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            value = await asyncio.shield(task)
            current_binding = await self._handle_session_binding_read({'sessionID':resolved['rootSessionID']})
            if any(current_binding.get(name) != expected for name, expected in frozen_binding.items()):
                raise ValueError('kernel binding changed; effects may have occurred')
            projected = validate_managed_method_result(method, {'ok':True,'result':project_model_result(value)})
            async with self._managed_execution_lock:
                if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active' or entry.get('pythonKernelClosing')
                    or record['cancelled'] or entry.get('ownerAccountID') != account or entry.get('policyRevision') != policy
                    or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker
                    or self._managed_provider_generation != holder or getattr(worker,'installed_generation',None) != generation
                    or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner):
                    kernels.revoke_call(reservation)
                    raise ValueError('kernel publication unavailable')
                kernels.complete_call(reservation); record['completed'] = True
            return projected
        except BaseException as exc:
            async with self._managed_execution_lock:
                record['cancelled'] = True
                kernels.revoke_call(reservation)
            await self._drain_python_kernel_call(kernels,reservation)
            if not task.done():
                try: await asyncio.shield(task)
                except Exception: pass
            if isinstance(exc, asyncio.CancelledError): raise
            raise ValueError('kernel operation unavailable; effects may have occurred') from None

    async def _handle_managed_actor_frame_call(self, params: dict) -> dict:
        """One original actor frame and one original Files publication."""
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        from src.openclank.files_facade import ProviderContext
        from src.openclank.native_capture_session_linux import CaptureScope, CaptureSession
        from src.desktop_capture import capture_enabled, publish_actor_frame
        from src.openclank.workspace_policy_service import resolve_owned_workspace
        method='_openclank/session/v1/actor-frame/call';unavailable={'ok':False}
        try:
            request=validate_managed_method_request(method,params); encoded=request['argsJSON'].encode('utf-8','strict')
            if len(encoded)>16384 or hashlib.sha256(encoded).hexdigest()!=request['argsSHA256']:return unavailable
            def unique(pairs):
                out={}
                for k,v in pairs:
                    if k in out:raise ValueError('duplicate frame input')
                    out[k]=v
                return out
            decoded=json.loads(encoded,object_pairs_hook=unique)
            if not isinstance(decoded,list) or len(decoded)!=2 or decoded[0]!='openclank.actor-frame.v1':return unavailable
            args=decoded[1]
            if (type(args) is not dict or set(args)!={'action','target','destination_ref','name'} or args.get('action')!='frame' or args.get('target')!='portal'
                or not isinstance(args.get('destination_ref'),str) or not args['destination_ref'] or len(args['destination_ref'].encode('utf-8'))>8192
                or not isinstance(args.get('name'),str) or not args['name'] or args['name'].strip()!=args['name'] or not args['name'].lower().endswith('.png')
                or len(args['name'].encode('utf-8'))>240 or any(ch in args['name'] for ch in ('/','\\','\x00')) or any(ord(ch)<32 or ord(ch)==127 for ch in args['name'])):return unavailable
            identity={field:request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
            key=self._managed_turn_key(identity);call_id=request['toolCallID'];material=(request['messageID'],request['toolName'],request['argsSHA256'])
            operation='actorframe-'+hashlib.sha256(json.dumps([identity,request['messageID'],call_id],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            async with self._managed_execution_lock:
                entry=self._managed_actor_turns.get(key)
                if not isinstance(entry,dict) or entry.get('state')!='active':return unavailable
                calls=entry.setdefault('actorFrameCalls',{});record=calls.get(call_id)
                if record is not None and record['identity']!=material:return unavailable
                if record is None:
                    if request['mode'] not in {'issue','cancel'} or len(calls)>=128:return unavailable
                    record={'identity':material,'invalidated':asyncio.Event(),'task':None};calls[call_id]=record
                if request['mode']=='cancel':
                    record['invalidated'].set();task=record.get('task')
                else:task=None
            if request['mode']=='cancel':
                if isinstance(task,asyncio.Task):
                    while not task.done():
                        try:await asyncio.shield(task)
                        except asyncio.CancelledError:continue
                        except Exception:break
                return {'ok':True,'result':{'state':'unknown','operation_id':operation}}
            owner,account,policy=entry.get('owner'),entry.get('ownerAccountID'),entry.get('policyRevision')
            if not isinstance(account,str) or not account or not capture_enabled(owner):return unavailable
            if (await self._handle_managed_tool_admit({**identity,'toolName':'capture_desktop','kind':'tool','toolCallID':call_id})).get('allowed') is not True:return unavailable
            resolved=await self.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
            root_session=resolved.get('rootSessionID')
            if not isinstance(root_session,str) or not root_session:return unavailable
            binding=await self._handle_session_binding_read({'sessionID':root_session})
            manager=get_session_manager_instance();state=getattr(manager,'lifecycle_context',None) if manager else None
            auth=getattr(state,'auth_manager',None);barrier=getattr(state,'account_request_barrier',None)
            repository=getattr(state,'files_policy_repository',None);supervisor=getattr(state,'mimo_supervisor',None)
            factory=getattr(state,'files_facade_state_factory',None);pool=supervisor.owner_lifecycle_pool() if supervisor else None
            worker=pool.worker_for_owner(owner) if pool else None;holder=self._managed_provider_generation
            generation=getattr(worker,'installed_generation',None);process=getattr(worker,'_proc',None)
            chat=binding.get('stableChatID');workspace_id=binding.get('authorityWorkspaceID')
            workspace=worker._bridge.mapped_session_workspace(chat) if worker and chat else None
            session=manager.get_session(chat) if manager and chat else None
            if (not auth or not barrier or not repository or not callable(factory) or not pool or not worker or not process or not holder
                or type(generation) is not int or not workspace or not workspace_id or not session or session.owner!=owner
                or getattr(session,'incognito',False) or getattr(session,'archived',False) or binding.get('owner')!=owner
                or chat!=entry.get('stableChatID') or workspace_id!=entry.get('authorityWorkspaceID') or getattr(session,'workspace_id',None)!=workspace_id):return unavailable
            files_binding=resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True)
            if files_binding.workspace.id!=workspace_id or files_binding.path!=workspace:return unavailable
            files_generation=repository.generation();owner_key=pool._key(owner);epoch=pool._owner_lifecycle_epoch(owner_key)
            source={field:request[field] for field in ('messageID','actorID','rootOperationID','executionRevision','innerTurnID','toolCallID','toolName','argsSHA256')};source['stableChatID']=chat
            if identity['sessionID']!=root_session: source['sourceSessionID']=identity['sessionID']
            invalidated=record['invalidated']
            def sync_current(_scope):
                try:
                    pool._assert_owner_lifecycle(owner_key,epoch)
                    return bool(not invalidated.is_set() and self._managed_actor_turns.get(key) is entry and entry.get('state')=='active'
                        and not entry.get('backgroundStartClosing') and entry.get('policyRevision')==policy and entry.get('ownerAccountID')==account
                        and self._managed_provider_generation==holder and supervisor.owner_lifecycle_pool() is pool and pool.worker_for_owner(owner) is worker
                        and worker._bridge is self and worker._proc is process and worker.is_alive() and worker.installed_generation==generation
                        and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)==holder)
                except Exception:return False
            async def current():
                try:
                    if (getattr(manager,'lifecycle_context',None) is not state or getattr(state,'auth_manager',None) is not auth
                        or getattr(state,'account_request_barrier',None) is not barrier or getattr(state,'files_policy_repository',None) is not repository
                        or getattr(state,'mimo_supervisor',None) is not supervisor or getattr(state,'files_facade_state_factory',None) is not factory):return False
                    if not sync_current(None) or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner or barrier._fenced(owner_key) or repository.generation()!=files_generation or not capture_enabled(owner):return False
                    if manager.get_session(chat) is not session or session.owner!=owner or getattr(session,'workspace_id',None)!=workspace_id or getattr(session,'incognito',False) or getattr(session,'archived',False):return False
                    if await self._handle_session_binding_read({'sessionID':root_session})!=binding:return False
                    if resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True)!=files_binding:return False
                    if (await self._handle_managed_tool_admit({**identity,'toolName':'capture_desktop','kind':'tool','toolCallID':call_id})).get('allowed') is not True:return False
                    response=await worker.session_frame_source_request(chat,root_session,workspace,source,owner=owner)
                    if response.status_code!=200 or response.json()!={'state':'current','argsSHA256':request['argsSHA256']}:return False
                    return bool(sync_current(None) and getattr(manager,'lifecycle_context',None) is state
                        and getattr(state,'auth_manager',None) is auth and getattr(state,'account_request_barrier',None) is barrier
                        and getattr(state,'files_policy_repository',None) is repository and getattr(state,'mimo_supervisor',None) is supervisor
                        and getattr(state,'files_facade_state_factory',None) is factory and auth.account_id(owner)==account
                        and auth.username_for_account_id(account)==owner and repository.generation()==files_generation and not barrier._fenced(owner_key)
                        and manager.get_session(chat) is session and session.owner==owner and getattr(session,'workspace_id',None)==workspace_id
                        and not getattr(session,'incognito',False) and not getattr(session,'archived',False) and capture_enabled(owner)
                        and resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True)==files_binding)
                except Exception:return False
            async def execute():
                async with barrier.admitted(owner):
                    if not await current():return {'state':'unavailable','operation_id':operation}
                    facade=factory(workspace);context=ProviderContext(account,owner,files_generation,bool(auth.is_admin(owner)),workspace_id)
                    destination=await facade.capture_media_destination(context,resource_ref=args['destination_ref'])
                    _provider,destination_ref=facade._provider_for_ref(context,args['destination_ref'],capability='write')
                    record['destination_id']=destination_ref.stable_id
                    record['destination_revision']=dict(destination['destination_revision'])
                    if not await current():return {'state':'unavailable','operation_id':operation}
                    scope=CaptureScope(owner,account,workspace_id,call_id,hashlib.sha256((holder+':'+str(generation)+':'+str(epoch)).encode()).hexdigest())
                    capture=CaptureSession(scope,sync_current,invalidated,int(time.time()*1000)+30000,frames=True)
                    async with capture as _active:
                        frame=await capture.latest_frame()
                        if frame is None:return {'state':'no_frame','operation_id':operation}
                        if not await current():return {'state':'unavailable','operation_id':operation}
                        record['frame_sha256']=frame['sha256'];record['frame_size']=len(frame['png']);record['pixel_size']=frame['pixel_size']
                        return await publish_actor_frame(frame=frame,facade=facade,context=context,destination=destination,name=args['name'],operation_id=operation,current=current)
            # Bind the child to this original call before any portal request.
            async with self._managed_execution_lock:
                if self._managed_actor_turns.get(key) is not entry or entry.get('state')!='active' or entry.get('backgroundStartClosing'):return unavailable
                calls=entry.setdefault('actorFrameCalls',{})
                if calls.get(call_id) is not record or record['identity']!=material or invalidated.is_set():return unavailable
                task=record.get('task')
                if task is None:
                    if request['mode']!='issue':return unavailable
                    task=asyncio.create_task(execute());record['task']=task
                    task.add_done_callback(lambda done:None if done.cancelled() else done.exception())
            try:
                result=await asyncio.shield(task)
            except asyncio.CancelledError:
                invalidated.set()
                while not task.done():
                    try:await asyncio.shield(task)
                    except asyncio.CancelledError:continue
                    except Exception:break
                raise
            except Exception:result={'state':'unknown','operation_id':operation}
            if not await current():return unavailable
            if request['mode']=='status' and result.get('state')=='unknown':
                try:
                    facade=factory(workspace);context=ProviderContext(account,owner,files_generation,bool(auth.is_admin(owner)),workspace_id)
                    original=await asyncio.to_thread(facade._load_operation,context,operation)
                    original_binding=(original.get('receipt') or {}).get('_import_binding') if isinstance(original,dict) else None
                    if (not isinstance(original_binding,dict) or original.get('owner')!=account or original_binding.get('account_id')!=account
                        or original_binding.get('workspace_id')!=workspace_id or original_binding.get('generation')!=files_generation
                        or original_binding.get('operation_id')!=operation or original_binding.get('item_id')!='output'
                        or original_binding.get('name')!=args['name'] or original_binding.get('destination_resource')!=record.get('destination_id')
                        or original_binding.get('destination_revision')!=record.get('destination_revision')
                        or original_binding.get('upload_digest')!='sha256:'+str(record.get('frame_sha256')) or original_binding.get('upload_size')!=record.get('frame_size')
                        or original_binding.get('derived_source')!={'frame_operation_id':operation,'frame_sha256':record.get('frame_sha256')}):raise ValueError('frame receipt changed')
                    receipt=await facade.operation_receipt(context,operation_id=operation)
                    items=receipt.get('items') if isinstance(receipt,dict) else None
                    if (await current() and receipt.get('state')=='complete' and isinstance(items,list) and len(items)==1 and items[0].get('item_id')=='output'
                        and items[0].get('outcome')=='committed' and isinstance(items[0].get('resource_ref'),str) and record.get('frame_sha256')):
                        result={'state':'captured','operation_id':operation,'resource_ref':items[0]['resource_ref'],'sha256':record['frame_sha256'],'pixel_size':record['pixel_size']}
                except Exception:pass
            return validate_managed_method_result(method,{'ok':True,'result':result})
        except asyncio.CancelledError:raise
        except Exception:return unavailable

    async def _handle_managed_shared_text_call(self, params: dict) -> dict:
        """Route one claimed original managed call to the existing shared-text owner."""
        from core.database import SessionLocal
        from core.models import get_session_manager_instance
        from src.published_files import PublishedFileService
        from src.openclank.shared_text_service import SharedTextService
        from src.openclank.shared_workbook_service import SharedWorkbookService
        from src.openclank.shared_slides_service import SharedSlidesService
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result, _validated_contract
        from src.openclank.workspace_policy_service import resolve_owned_workspace
        method = '_openclank/session/v1/shared-text/call'; unavailable = {'ok':False}
        try:
            request = validate_managed_method_request(method, params)
            encoded = request['argsJSON'].encode('utf-8','strict')
            if len(encoded)>131072 or hashlib.sha256(encoded).hexdigest()!=request['argsSHA256']: return unavailable
            def unique(pairs):
                out={}
                for name,value in pairs:
                    if name in out: raise ValueError('duplicate selector')
                    out[name]=value
                return out
            decoded=json.loads(encoded,object_pairs_hook=unique,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))
            if type(decoded) is not list or len(decoded)!=2 or decoded[0] not in ('openclank.shared-text.v1','openclank.shared-workbook.v1','openclank.shared-slides.v1'): return unavailable
            workbook=decoded[0]=='openclank.shared-workbook.v1'; slides=decoded[0]=='openclank.shared-slides.v1'
            args=decoded[1]; contract,validator=_validated_contract()
            arg_schema='managedSharedSlidesArgs' if slides else 'managedSharedWorkbookArgs' if workbook else 'managedSharedTextArgs'
            if next(validator({'$ref':'#/$defs/'+arg_schema,'$defs':contract['$defs']}).iter_errors(args),None) is not None: return unavailable
            identity={field:request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
            key=self._managed_turn_key(identity);call_id=request['toolCallID']; frozen=(request['messageID'],request['toolName'],request['argsSHA256'])
            async with self._managed_execution_lock:
                entry=self._managed_actor_turns.get(key)
                if not isinstance(entry,dict) or entry.get('state')!='active': return unavailable
                owner,account,policy=entry.get('owner'),entry.get('ownerAccountID'),entry.get('policyRevision')
            if not isinstance(account,str) or not account: return unavailable
            if (await self._handle_managed_tool_admit({**identity,'toolName':'shared_text','kind':'tool','toolCallID':call_id})).get('allowed') is not True: return unavailable
            resolved=await self.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
            root_session=resolved.get('rootSessionID')
            if not isinstance(root_session,str) or not root_session: return unavailable
            binding=await self._handle_session_binding_read({'sessionID':root_session})
            manager=get_session_manager_instance();state=getattr(manager,'lifecycle_context',None) if manager else None
            auth=getattr(state,'auth_manager',None);barrier=getattr(state,'account_request_barrier',None)
            repository=getattr(state,'files_policy_repository',None);supervisor=getattr(state,'mimo_supervisor',None)
            factory=getattr(state,'files_facade_state_factory',None);pool=supervisor.owner_lifecycle_pool() if supervisor else None
            worker=pool.worker_for_owner(owner) if pool else None;holder=self._managed_provider_generation
            generation=getattr(worker,'installed_generation',None);process=getattr(worker,'_proc',None)
            chat=binding.get('stableChatID'); workspace_id=binding.get('authorityWorkspaceID')
            workspace=worker._bridge.mapped_session_workspace(chat) if worker and chat else None
            session=manager.get_session(chat) if manager and chat else None
            if (not auth or not barrier or not repository or not callable(factory) or not pool or not worker or not process or not holder
                    or type(generation) is not int or not workspace or not workspace_id or not session or session.owner!=owner
                    or getattr(session,'incognito',False) or getattr(session,'archived',False) or binding.get('owner')!=owner
                    or chat!=entry.get('stableChatID') or workspace_id!=entry.get('authorityWorkspaceID') or getattr(session,'workspace_id',None)!=workspace_id): return unavailable
            files_binding=resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True)
            if files_binding.workspace.id!=workspace_id or files_binding.path!=workspace: return unavailable
            files_generation=repository.generation();owner_key=pool._key(owner);epoch=pool._owner_lifecycle_epoch(owner_key)
            source={field:request[field] for field in ('messageID','actorID','rootOperationID','executionRevision','innerTurnID','toolCallID','toolName','argsSHA256')};source['stableChatID']=chat;source['sourceSessionID']=identity['sessionID']
            initiator={'version':1,**identity,'messageID':request['messageID'],'toolCallID':call_id,'argsSHA256':request['argsSHA256'],'stableChatID':chat}
            operation_id='agent-'+hashlib.sha256(b'openclank.shared-text.managed.v1\0'+json.dumps(initiator,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8','strict')).hexdigest()
            async def current():
                try:
                    pool._assert_owner_lifecycle(owner_key,epoch)
                    if (getattr(manager,'lifecycle_context',None) is not state or getattr(state,'auth_manager',None) is not auth
                        or getattr(state,'account_request_barrier',None) is not barrier or getattr(state,'files_policy_repository',None) is not repository
                        or getattr(state,'mimo_supervisor',None) is not supervisor or getattr(state,'files_facade_state_factory',None) is not factory
                        or barrier._fenced(owner_key) or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
                        or repository.generation()!=files_generation or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker
                        or worker._bridge is not self or worker._proc is not process or not worker.is_alive() or worker.installed_generation!=generation
                        or self._managed_provider_generation!=holder or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder
                        or manager.get_session(chat) is not session or session.owner!=owner or getattr(session,'workspace_id',None)!=workspace_id
                        or getattr(session,'incognito',False) or getattr(session,'archived',False)
                        or self._managed_actor_turns.get(key) is not entry or entry.get('state')!='active' or entry.get('policyRevision')!=policy
                        or entry.get('ownerAccountID')!=account or entry.get('backgroundStartClosing')): return False
                    if await self._handle_session_binding_read({'sessionID':root_session})!=binding: return False
                    if resolve_owned_workspace(repository,workspace_id=workspace_id,owner_username=owner,auth_manager=auth,purpose='agent_workspace',observation_only=True)!=files_binding: return False
                    if (await self._handle_managed_tool_admit({**identity,'toolName':'shared_text','kind':'tool','toolCallID':call_id})).get('allowed') is not True: return False
                    pool._assert_owner_lifecycle(owner_key,epoch)
                    return bool(getattr(manager,'lifecycle_context',None) is state and getattr(state,'auth_manager',None) is auth
                        and getattr(state,'account_request_barrier',None) is barrier and getattr(state,'files_policy_repository',None) is repository
                        and getattr(state,'mimo_supervisor',None) is supervisor and getattr(state,'files_facade_state_factory',None) is factory
                        and not barrier._fenced(owner_key) and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                        and repository.generation()==files_generation and supervisor.owner_lifecycle_pool() is pool and pool.worker_for_owner(owner) is worker
                        and worker._bridge is self and worker._proc is process and worker.is_alive() and worker.installed_generation==generation
                        and self._managed_provider_generation==holder and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)==holder
                        and manager.get_session(chat) is session and session.owner==owner and getattr(session,'workspace_id',None)==workspace_id
                        and not getattr(session,'incognito',False) and not getattr(session,'archived',False)
                        and self._managed_actor_turns.get(key) is entry and entry.get('state')=='active' and entry.get('policyRevision')==policy
                        and entry.get('ownerAccountID')==account and not entry.get('backgroundStartClosing'))
                except Exception: return False
            async def verify_source():
                try:
                    if not await current(): return False
                    response=await worker.session_shared_text_source_request(chat,root_session,workspace,source,owner=owner)
                    return bool(response.status_code==200 and response.json()=={'state':'current','argsSHA256':request['argsSHA256']} and await current())
                except Exception: return False
            async with barrier.admitted(owner):
                if not await verify_source(): return unavailable
                def provider_for_owner(context): return factory(context.workspace_id)._providers.get('host')
                service=(SharedSlidesService if slides else SharedWorkbookService if workbook else SharedTextService)(service=PublishedFileService(session_factory=SessionLocal),auth=auth,barrier=barrier,
                    store=repository,provider_for_owner=provider_for_owner,original_current=current,verify_original_source=verify_source)
                async def execute():
                    if args['action']=='read': return await service.read(recipient_account=account,publication_id=args['publication_id'])
                    if args['action']=='commit':
                        return await service.commit(recipient_account=account,publication_id=args['publication_id'],operation_id=operation_id,
                            base_text=args['base_text'],base_sha256=args['base_sha256'],client_digest=None,initiator=initiator,
                            **({'objects':args['objects']} if slides else {'cells':args['cells']} if workbook else {'edits':args['edits']}))
                    return await service.status(recipient_account=account,publication_id=args['publication_id'],operation_id=args['operation_id'],client_digest=args['client_digest'])
                async with self._managed_execution_lock:
                    if self._managed_actor_turns.get(key) is not entry or entry.get('state')!='active': return unavailable
                    calls=entry.setdefault('sharedTextCalls',{});record=calls.get(call_id)
                    if record is not None and record['identity']!=frozen: return unavailable
                    if record is None:
                        if request['mode']!='issue' or len(calls)>=128: return unavailable
                        task=asyncio.create_task(execute());task.add_done_callback(lambda done:None if done.cancelled() else done.exception())
                        record={'identity':frozen,'task':task};calls[call_id]=record
                    task=record['task']
                cancelled=False
                while True:
                    try: value=await asyncio.shield(task);break
                    except asyncio.CancelledError:
                        if task.cancelled(): return unavailable
                        cancelled=True
                if cancelled: raise asyncio.CancelledError
                if not await current(): return unavailable
                return validate_managed_method_result(method,{'ok':True,'result':value})
        except asyncio.CancelledError: raise
        except Exception: return unavailable

    async def _handle_managed_media_job_call(self, params: dict) -> dict:
        """Invoke the existing J0 owner for an exact fresh managed tool call."""
        from core.models import get_session_manager_instance
        from src.openclank.agent_media import AgentMediaService
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/media-job/call'
        unavailable = {'ok': False}
        try:
            request = validate_managed_method_request(method, params)
            encoded = request['argsJSON'].encode('utf-8', errors='strict')
            if len(encoded) > 262144 or hashlib.sha256(encoded).hexdigest() != request['argsSHA256']:
                return unavailable
            def unique_pairs(pairs):
                result = {}
                for name, value in pairs:
                    if name in result: raise ValueError('duplicate media selector')
                    result[name] = value
                return result
            def finite_float(value):
                import math
                number = float(value)
                if not math.isfinite(number): raise ValueError('invalid media number')
                return number
            decoded = json.loads(encoded, object_pairs_hook=unique_pairs, parse_float=finite_float,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError('invalid media number')))
            if not isinstance(decoded, list) or len(decoded) != 2 or decoded[0] != 'openclank.media-job.v1': return unavailable
            args = decoded[1]
            from src.openclank.managed_protocol import _validated_contract
            contract, validator_type = _validated_contract()
            if next(validator_type({'$ref':'#/$defs/managedMediaJobArgs', '$defs':contract['$defs']}).iter_errors(args), None) is not None:
                return unavailable
            identity = {field: request[field] for field in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
            key = self._managed_turn_key(identity)
            async with self._managed_execution_lock:
                entry = self._managed_actor_turns.get(key)
                if not isinstance(entry, dict) or entry.get('state') != 'active': return unavailable
                owner, account, policy = entry.get('owner'), entry.get('ownerAccountID'), entry.get('policyRevision')
            if not isinstance(account, str) or not account: return unavailable
            if (await self._handle_managed_tool_admit({**identity, 'toolName':'media_job', 'kind':'tool', 'toolCallID':request['toolCallID']})).get('allowed') is not True: return unavailable
            resolved = await self.resolve_managed_actor_execution(identity['sessionID'], identity['rootOperationID'], identity['actorID'], identity['executionRevision'])
            root_session = resolved.get('rootSessionID')
            if not isinstance(root_session, str) or not root_session: return unavailable
            binding = await self._handle_session_binding_read({'sessionID':root_session})
            manager = get_session_manager_instance(); state = getattr(manager, 'lifecycle_context', None) if manager else None
            auth = getattr(state, 'auth_manager', None); barrier = getattr(state, 'account_request_barrier', None)
            repository = getattr(state, 'files_policy_repository', None); supervisor = getattr(state, 'mimo_supervisor', None)
            pool = supervisor.owner_lifecycle_pool() if supervisor is not None else None
            worker = pool.worker_for_owner(owner) if pool else None
            holder = self._managed_provider_generation; generation = getattr(worker, 'installed_generation', None)
            process = getattr(worker, '_proc', None); chat = binding.get('stableChatID'); workspace_id = binding.get('authorityWorkspaceID')
            workspace = worker._bridge.mapped_session_workspace(chat) if worker and chat else None
            session = manager.get_session(chat) if manager and chat else None
            factory = getattr(state, 'media_agent_service_factory', None)
            material_factory = getattr(state, 'material_agent_service_factory', None)
            sprite_factory = getattr(state, 'sprite_agent_service_factory', None)
            trellis_factory = getattr(state, 'trellis_agent_service_factory', None)
            hunyuan_factory = getattr(state, 'hunyuan_agent_service_factory', None)
            meshy_rig_factory = getattr(state, 'meshy_rig_agent_service_factory', None)
            voidless_factory = getattr(state, 'voidless_agent_service_factory', None)
            blender_factory = getattr(state, 'blender_agent_service_factory', None)
            provider_store = getattr(state, 'provider_store', None)
            if (not auth or not barrier or not repository or not worker or not process or not holder or type(generation) is not int
                    or not workspace or not workspace_id or not session or session.owner != owner or getattr(session, 'incognito', False)
                    or getattr(session, 'archived', False) or binding.get('owner') != owner or chat != entry.get('stableChatID')
                    or workspace_id != entry.get('authorityWorkspaceID') or not callable(factory) or provider_store is None): return unavailable
            from src.openclank.workspace_policy_service import resolve_owned_workspace
            files_binding = resolve_owned_workspace(repository, workspace_id=workspace_id, owner_username=owner,
                auth_manager=auth, purpose='agent_workspace', observation_only=True)
            if files_binding.workspace.id != workspace_id or files_binding.path != workspace: return unavailable
            generation_policy = repository.generation()
            owner_key = pool._key(owner)
            owner_epoch = pool._owner_lifecycle_epoch(owner_key)
            pool._assert_owner_lifecycle(owner_key, owner_epoch)
            source = {field:request[field] for field in ('messageID','actorID','rootOperationID','executionRevision','innerTurnID','toolCallID','toolName','argsSHA256')}
            source['stableChatID'] = chat
            if identity['sessionID'] != root_session: source['sourceSessionID'] = identity['sessionID']
            async with barrier.admitted(owner):
                response = await worker.session_media_source_request(chat, root_session, workspace, source, owner=owner)
                if response.status_code != 200 or response.json() != {'state':'current','argsSHA256':request['argsSHA256']}: return unavailable
                async def current():
                    try:
                        if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                                or getattr(state, 'auth_manager', None) is not auth or getattr(state, 'account_request_barrier', None) is not barrier
                                or getattr(state, 'files_policy_repository', None) is not repository or repository.generation() != generation_policy
                                or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker
                                or worker._bridge is not self or worker._proc is not process or not worker.is_alive()
                                or self._managed_provider_generation != holder or worker.installed_generation != generation
                                or manager.get_session(chat) is not session or session.owner != owner
                                or getattr(session,'incognito',False) or getattr(session,'archived',False)
                                or await self._handle_session_binding_read({'sessionID':root_session}) != binding): return False
                        if resolve_owned_workspace(repository, workspace_id=workspace_id, owner_username=owner, auth_manager=auth,
                                purpose='agent_workspace', observation_only=True) != files_binding: return False
                        async with self._managed_execution_lock:
                            if self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active' or entry.get('policyRevision') != policy: return False
                        if (await self._handle_managed_tool_admit({**identity,'toolName':'media_job','kind':'tool','toolCallID':request['toolCallID']})).get('allowed') is not True: return False
                        # No awaits after this final original-owner/currentness fence.
                        pool._assert_owner_lifecycle(owner_key, owner_epoch)
                        return (getattr(manager, 'lifecycle_context', None) is state
                            and getattr(state, 'auth_manager', None) is auth and getattr(state, 'account_request_barrier', None) is barrier
                            and getattr(state, 'files_policy_repository', None) is repository and getattr(state, 'mimo_supervisor', None) is supervisor
                            and getattr(state, 'provider_store', None) is provider_store and getattr(state, 'media_agent_service_factory', None) is factory
                            and getattr(state, 'material_agent_service_factory', None) is material_factory
                            and getattr(state, 'sprite_agent_service_factory', None) is sprite_factory
                            and getattr(state, 'trellis_agent_service_factory', None) is trellis_factory
                            and getattr(state, 'hunyuan_agent_service_factory', None) is hunyuan_factory
                            and getattr(state, 'meshy_rig_agent_service_factory', None) is meshy_rig_factory
                            and getattr(state, 'voidless_agent_service_factory', None) is voidless_factory
                            and getattr(state, 'blender_agent_service_factory', None) is blender_factory
                            and not barrier._fenced(owner_key) and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                            and repository.generation() == generation_policy and supervisor.owner_lifecycle_pool() is pool
                            and pool.worker_for_owner(owner) is worker and worker._bridge is self and worker._proc is process and worker.is_alive()
                            and self._managed_provider_generation == holder and worker.installed_generation == generation
                            and manager.get_session(chat) is session and session.owner == owner
                            and not getattr(session, 'incognito', False) and not getattr(session, 'archived', False)
                            and self._managed_actor_turns.get(key) is entry and entry.get('state') == 'active' and entry.get('policyRevision') == policy)
                    except Exception:
                        return False
                if not await current(): return unavailable
                authority = {'account_id':account,'owner':owner,'session_id':chat,'workspace_id':workspace_id,
                    'actor_id':identity['actorID'],'root_operation_id':identity['rootOperationID'],'turn_id':identity['innerTurnID'],
                    'message_id':request['messageID'],'tool_call_id':request['toolCallID'],'policy_generation':generation_policy,
                    'execution_revision':identity['executionRevision']}
                async def session_current(candidate):
                    return (candidate.get('account_id') == account and candidate.get('session_id') == chat
                            and candidate.get('workspace_id') == workspace_id and await current())
                async def tripo_current():
                    if not await current(): raise ValueError('media authority changed')
                tripo_factory=getattr(state,'tripo_agent_service_factory',None)
                fal_factory=getattr(state,'fal_agent_service_factory',None)
                fal_mesh_factory=getattr(state,'fal_mesh_agent_service_factory',None)
                async def fal_mesh_current():
                    if getattr(state,'fal_mesh_agent_service_factory',None) is not fal_mesh_factory or not await current():
                        raise ValueError('media authority changed')
                async def blender_current():
                    if getattr(state,'blender_agent_service_factory',None) is not blender_factory or not await current():
                        raise ValueError('media authority changed')
                async def trellis_current():
                    if getattr(state,'trellis_agent_service_factory',None) is not trellis_factory or not await current():
                        raise ValueError('media authority changed')
                async def hunyuan_current():
                    if getattr(state,'hunyuan_agent_service_factory',None) is not hunyuan_factory or not await current():
                        raise ValueError('media authority changed')
                    return True
                async def meshy_rig_current():
                    if getattr(state,'meshy_rig_agent_service_factory',None) is not meshy_rig_factory or not await current():
                        raise ValueError('media authority changed')
                    return True
                async def voidless_current():
                    if getattr(state,'voidless_agent_service_factory',None) is not voidless_factory or not await current():
                        raise ValueError('media authority changed')
                    return True
                async def sprite_current():
                    if getattr(state,'sprite_agent_service_factory',None) is not sprite_factory or not await current():
                        raise ValueError('media authority changed')
                async def material_current():
                    if getattr(state,'material_agent_service_factory',None) is not material_factory or not await current():
                        raise ValueError('media authority changed')
                service = AgentMediaService(media_service=factory(workspace=workspace_id, session_current=session_current,
                    before_dispatch=current), provider_store=provider_store, current=current,
                    tripo_service=(tripo_factory(workspace=workspace_id,session_current=session_current,before_dispatch=tripo_current) if callable(tripo_factory) else None),
                    fal_service=(fal_factory(workspace=workspace_id,session_current=session_current,before_dispatch=tripo_current) if callable(fal_factory) else None),
                    fal_mesh_service=(fal_mesh_factory(workspace=workspace_id,session_current=session_current,before_dispatch=fal_mesh_current) if callable(fal_mesh_factory) else None),
                    material_service=(material_factory(workspace=workspace_id,session_current=session_current,before_dispatch=material_current) if callable(material_factory) else None),
                    sprite_service=(sprite_factory(workspace=workspace_id,session_current=session_current,before_dispatch=sprite_current) if callable(sprite_factory) else None),
                    trellis_service=(trellis_factory(workspace=workspace_id,session_current=session_current,before_dispatch=trellis_current) if callable(trellis_factory) else None),
                    hunyuan_service=(hunyuan_factory(workspace=workspace_id,session_current=session_current,before_dispatch=hunyuan_current) if callable(hunyuan_factory) else None),
                    meshy_rig_service=(meshy_rig_factory(workspace=workspace_id,session_current=session_current,before_dispatch=meshy_rig_current) if callable(meshy_rig_factory) else None),
                    voidless_service=(voidless_factory(workspace=workspace_id,session_current=session_current,before_dispatch=voidless_current) if callable(voidless_factory) else None),
                    blender_services=({**{operation:blender_factory(workspace=workspace_id,session_current=session_current,before_dispatch=blender_current,operation=operation) for operation in ('image.render','material.bake')},
                        **{'image.render:'+variant:blender_factory(workspace=workspace_id,session_current=session_current,before_dispatch=blender_current,operation='image.render',variant=variant) for variant in ('four_heading','rig_clip')}} if callable(blender_factory) else {}))
                action = args['action']
                record = None
                def on_reserved(job_id):
                    if not isinstance(job_id, str) or not job_id or len(job_id) > 256 or not isinstance(record, dict):
                        raise ValueError('media reservation unavailable')
                    previous = record.get('job_id')
                    if previous is not None and previous != job_id: raise ValueError('media reservation changed')
                    record['job_id'] = job_id
                async def execute():
                    if action == 'profiles': return await service.profiles(authority=authority, operation=args.get('operation'))
                    if action == 'inspect': return {'inspection':await service.inspect(authority=authority, **{k:args[k] for k in ('operation','inputs','parameters')})}
                    if action == 'submit':
                        return await service.submit(authority=authority, on_reserved=on_reserved, **{k:args[k] for k in ('operation','profile_id','inputs','parameters','destination')})
                    return await service.action(authority=authority, job_id=args['job_id'], action=action)
                async with self._managed_execution_lock:
                    if self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active': return unavailable
                    calls = entry.setdefault('mediaJobCalls', {})
                    call_key = request['toolCallID']
                    frozen = (request['messageID'], request['toolName'], request['argsSHA256'])
                    record = calls.get(call_key)
                    if record is not None and record['identity'] != frozen: return unavailable
                    if record is None:
                        if request['mode'] != 'issue' or len(calls) >= 128: return unavailable
                        record = {'identity':frozen}
                        task = asyncio.create_task(execute())
                        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                        record['task'] = task
                        calls[call_key] = record
                    task = record['task']
                cancelled = False
                failed = False
                while True:
                    try:
                        value = await asyncio.shield(task)
                        break
                    except asyncio.CancelledError:
                        if task.cancelled():
                            failed = True
                            break
                        cancelled = True
                    except Exception:
                        failed = True
                        break
                if cancelled: raise asyncio.CancelledError
                if not await current(): return unavailable
                if failed:
                    job_id = record.get('job_id')
                    if action != 'submit' or not isinstance(job_id, str): return unavailable
                    # This reads only the original locally retained job; it cannot issue provider work.
                    value = await service.snapshot(authority=authority, job_id=job_id)
                    if not await current(): return unavailable
                return validate_managed_method_result(method, {'ok':True, **value})
        except Exception:
            return unavailable

    async def _handle_managed_theme_emoji_call(self, params: dict) -> dict:
        """Route a private tool call through current host authority."""
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        from routes.theme_emoji_routes import managed_theme_emoji_call
        method = '_openclank/session/v1/theme-emoji/call'
        request = validate_managed_method_request(method, params)
        fields = ('sessionID', 'rootOperationID', 'actorID', 'executionRevision', 'innerTurnID')
        identity = {field: request[field] for field in fields}
        key = self._managed_turn_key(identity)
        async with self._managed_execution_lock:
            entry = self._managed_actor_turns.get(key)
            if not isinstance(entry, dict) or entry.get('state') != 'active':
                raise ValueError('theme emoji turn unavailable')
            owner = entry.get('owner')
            launch_account = entry.get('ownerAccountID')
            policy_revision = entry.get('policyRevision')
        decision = await self._handle_managed_tool_admit({**identity, 'toolName': 'theme_emoji_assets', 'kind': 'tool', 'toolOperation':request['operation']})
        if decision.get('allowed') is not True:
            raise ValueError('theme emoji authority unavailable')
        if not isinstance(launch_account, str) or not launch_account:
            raise ValueError('theme emoji launch account unavailable')
        manager = get_session_manager_instance()
        state = getattr(manager, 'lifecycle_context', None) if manager is not None else None
        auth = getattr(state, 'auth_manager', None)
        barrier = getattr(state, 'account_request_barrier', None)
        account_id = auth.account_id(owner) if auth is not None and isinstance(owner, str) else None
        if account_id != launch_account or auth.username_for_account_id(account_id) != owner:
            raise ValueError('theme emoji account unavailable')
        operation = request['operation']
        if operation == 'status':
            value = await managed_theme_emoji_call(owner=owner, account_id=account_id, operation=operation,
                auth_manager=auth, account_barrier=barrier, scheduler=getattr(state, 'task_scheduler', None))
            decision = await self._handle_managed_tool_admit({**identity, 'toolName': 'theme_emoji_assets', 'kind': 'tool', 'toolOperation':request['operation']})
            if decision.get('allowed') is not True:
                raise ValueError('theme emoji authority changed')
        else:
            # Scheduling the one authorized coroutine is the linearization
            # point; it cannot perform async work until this lock is released.
            async with self._managed_execution_lock:
                current = self._managed_actor_turns.get(key)
                if (current is not entry or current.get('state') != 'active'
                        or current.get('ownerAccountID') != account_id
                        or current.get('policyRevision') != policy_revision):
                    raise ValueError('theme emoji turn changed')
                permit = _ThemeEmojiReservation(key, owner, account_id, operation, policy_revision)
                permit.consume()
                task = asyncio.create_task(managed_theme_emoji_call(owner=owner, account_id=account_id,
                    operation=operation, auth_manager=auth, account_barrier=barrier,
                    scheduler=getattr(state, 'task_scheduler', None), reservation=permit))
                # A disconnected caller must not leave a task exception
                # unobserved; the admitted service keeps its own barrier.
                task.add_done_callback(lambda finished: None if finished.cancelled() else finished.exception())
            value = await asyncio.shield(task)
        if value.get('operation') != operation:
            raise ValueError('theme emoji result changed')
        return validate_managed_method_result(method, value)

    async def _handle_skill_metadata_current(self,params:dict)->dict:
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import validate_managed_method_request,validate_managed_method_result
        from src.openclank.prepared_discovery import PreparedSkillsReader
        method='_openclank/session/v1/skill/metadata-current'
        try:
            request=validate_managed_method_request(method,params)
            fields=('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')
            identity={key:request[key] for key in fields};proof={key:request[key] for key in ('skillID','revision','contentHash','snapshotSHA256','metadataReceiptSHA256')}
            manager=get_session_manager_instance();state=getattr(manager,'lifecycle_context',None) if manager is not None else None
            reader=getattr(state,'prepared_discovery',None) if state is not None else None
            if type(reader) is not PreparedSkillsReader:raise ValueError('skill metadata unavailable')
            result=await reader.metadata_current(self,identity,proof)
            return validate_managed_method_result(method,result)
        except Exception:return validate_managed_method_result(method,{'state':'unavailable'})

    async def _handle_managed_prepared_read(self, params: dict) -> dict:
        """Explicit prepared reads use only turn-local, non-authoritative selectors."""
        import math
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method='_openclank/session/v1/memory/prepared'
        try:
            request=validate_managed_method_request(method,params)
            fields=('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')
            identity={field:request[field] for field in fields};kind=request['kind'];key=self._managed_turn_key(identity)
            manager=get_session_manager_instance();state=getattr(manager,'lifecycle_context',None) if manager is not None else None
            reader=getattr(state,'prepared_discovery',None) if state is not None else None
            from src.openclank.prepared_discovery import PreparedSkillsReader
            if type(reader) is not PreparedSkillsReader: raise ValueError('prepared reader unavailable')
            before=await reader._actor(self,identity,require_skill=kind=='skill')
            async with self._managed_execution_lock:
                entry=self._managed_actor_turns.get(key)
                if type(entry) is not dict or entry.get('state')!='active': raise ValueError('prepared turn unavailable')
                entries=entry.setdefault('preparedReadSelectors',{})
                if type(entries) is not dict or len(entries)>128: raise ValueError('prepared capacity unavailable')
                selector=request['cursor'] if request['action']=='page' else request['token']
                retained=None if selector is None else entries.get(selector)
                expected_type={'page':'cursor','hydrate':'item','snapshot_read':'snapshot'}[request['action']]
                if selector is not None and (type(retained) is not dict or retained.get('kind')!=kind or
                    retained.get('type')!=expected_type):
                    raise ValueError('prepared selector unavailable')
                selected=None if retained is None else retained['value']
            if request['action']=='page':
                result=(await reader.page(self,identity,limit=request['limit'],cursor=selected) if kind=='skill'
                    else await reader.knowledge(self,identity,limit=request['limit'],cursor=selected))
            elif request['action']=='hydrate':
                result=(await reader.hydrate(self,identity,descriptor=selected) if kind=='skill'
                    else await reader.knowledge(self,identity,limit=1,expected=selected))
                expansion=None
                if kind=='knowledge' and type(result) is dict and result.get('state')=='ready':
                    try:
                        body=json.loads(result.get('body',''))
                        if type(body) is dict and body.get('version')=='openclank.analysis-snapshot-memory.v1':
                            expansion=await reader.snapshot(self,identity,item=selected)
                            if expansion is not None and expansion.get('body')!=result.get('body'): expansion=None
                    except (ValueError,TypeError): pass
            else:
                if kind!='knowledge' or type(selected) is not dict:raise ValueError('snapshot selector unavailable')
                result=await reader.snapshot(self,identity,item=selected['item'],offset=selected['offset'],proof=selected['proof'])
                if result is None:raise ValueError('snapshot unavailable')
            if await reader._actor(self,identity,require_skill=kind=='skill')!=before: raise ValueError('prepared turn changed')
            if type(result) is not dict: raise ValueError('prepared result unavailable')
            if result.get('state') in ('unavailable','stale'):
                return validate_managed_method_result(method,{'state':result['state']})
            async with self._managed_execution_lock:
                if self._managed_actor_turns.get(key) is not entry or entry.get('state')!='active' or entry.get('preparedReadSelectors') is not entries:
                    raise ValueError('prepared turn changed')
                if request['action']=='hydrate':
                    body=result.get('body')
                    if result.get('state')!='ready' or type(body) is not str or len(body.encode('utf-8'))>16384: raise ValueError('prepared body unavailable')
                    response={'state':'ready','kind':kind,'body':body}
                    if kind=='knowledge' and expansion is not None:
                        existing=retained.get('snapshotToken')
                        if existing is not None:
                            saved=entries.get(existing)
                            if type(saved) is not dict or saved.get('type')!='snapshot' or saved.get('value')!={'item':selected,'proof':expansion['proof'],'offset':0}:raise ValueError('snapshot selector changed')
                            token=existing
                        elif len(entries)<128:
                            token=secrets.token_urlsafe(24)
                        else:
                            return validate_managed_method_result(method,{'state':'unavailable'})
                        if token is not None:
                            descriptor={'token':token,'sha256':expansion['sha256'],'sizeBytes':expansion['totalBytes'],'captureOrigin':expansion['captureOrigin']}
                            candidate={**response,'expansion':descriptor}
                            if len(json.dumps(candidate,ensure_ascii=False,separators=(',',':')).encode('utf-8'))<=16384:
                                if existing is None:
                                    entries[token]={'kind':'knowledge','type':'snapshot','value':{'item':selected,'proof':expansion['proof'],'offset':0}}
                                    retained['snapshotToken']=token
                                response=candidate
                elif request['action']=='snapshot_read':
                    complete=result.get('complete'); end=result.get('endByte')
                    if type(complete) is not bool or type(end) is not int or type(result.get('fragment')) is not str or len(result['fragment'].encode('utf-8'))>8192:raise ValueError('snapshot fragment unavailable')
                    next_token=None
                    if not complete:
                        next_token=retained.get('nextToken')
                        expected={'item':selected['item'],'proof':selected['proof'],'offset':end}
                        if next_token is not None:
                            next_entry=entries.get(next_token)
                            if type(next_entry) is not dict or next_entry.get('type')!='snapshot' or next_entry.get('value')!=expected:raise ValueError('snapshot continuation changed')
                        elif len(entries)<128:
                            next_token=secrets.token_urlsafe(24);entries[next_token]={'kind':'knowledge','type':'snapshot','value':expected};retained['nextToken']=next_token
                        else:raise ValueError('snapshot capacity unavailable')
                    response={'state':'ready','kind':'knowledge','fragment':result['fragment'],'startByte':result['startByte'],
                        'endByte':end,'totalBytes':result['totalBytes'],'sha256':result['sha256'],'captureOrigin':result['captureOrigin'],
                        'operation':result['operation'],'representation':'json_text_fragment','complete':complete,'nextToken':next_token}
                    if len(json.dumps(response,ensure_ascii=False,separators=(',',':')).encode('utf-8'))>16384:raise ValueError('snapshot fragment too large')
                else:
                    items=result.get('items');next_cursor=result.get('next');complete=result.get('complete')
                    if (result.get('state') not in ('ready','partial') or type(items) is not list or len(items)>request['limit']
                        or type(complete) is not bool or (result['state']=='partial' and complete)): raise ValueError('prepared page unavailable')
                    if len(entries)+len(items)+(next_cursor is not None)>128:
                        return validate_managed_method_result(method,{'state':'partial','kind':kind,'items':[],'cursor':None,'complete':False})
                    visible=[]
                    for item in items:
                        if type(item) is not dict: raise ValueError('prepared item unavailable')
                        name=item.get('name') if kind=='skill' else None
                        trust=item.get('trust')
                        reference=item.get('reference') if kind=='skill' else item.get('selected',{}).get('reference')
                        display_id=reference.get('skill_id' if kind=='skill' else 'block_id') if type(reference) is dict else None
                        display_type='skill' if kind=='skill' else item.get('record_type')
                        if ((name is not None and (type(name) is not str or not name or len(name)>512)) or
                            (kind=='skill' and trust not in ('verified','waived','legacy')) or
                            (kind=='knowledge' and (type(trust) not in (int,float) or not math.isfinite(trust))) or
                            type(display_id) is not str or not display_id or len(display_id)>512 or
                            type(display_type) is not str or not display_type or len(display_type)>128):
                            raise ValueError('prepared item unavailable')
                        token=secrets.token_urlsafe(24);entries[token]={'kind':kind,'type':'item','value':item}
                        visible.append({'token':token,'name':name,'trust':trust,'trustBasis':'skill_publication' if kind=='skill' else 'knowledge_trust_score',
                            'display':{'id':display_id,'type':display_type}})
                    cursor=None
                    if next_cursor is not None:
                        cursor=secrets.token_urlsafe(24);entries[cursor]={'kind':kind,'type':'cursor','value':next_cursor}
                    response={'state':result['state'],'kind':kind,'items':visible,'cursor':cursor,'complete':complete}
            return validate_managed_method_result(method,response)
        except asyncio.CancelledError:
            raise
        except Exception:
            return {'state':'unavailable'}

    async def _handle_managed_context_augment(self, params: dict) -> dict:
        """Optional ephemeral context under the exact active C2 turn and one deadline."""
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        from src.openclank.memory_context_admission import S07ContextEvidence
        method = '_openclank/session/v1/context/augment'
        trace = logger.isEnabledFor(logging.DEBUG); s07_trace = False
        phase = ['validate', asyncio.get_running_loop().time()]; timings = {}; outcome = ['rejected']
        def mark(name):
            if trace and s07_trace:
                now = asyncio.get_running_loop().time(); timings[phase[0]] = round((now-phase[1])*1000, 3); phase[:] = [name, now]
        try:
            request = validate_managed_method_request(method, params)
            fields = ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')
            identity = {field:request[field] for field in fields}
            if request.get('kind') == 'deliverable_appearance':
                if identity['actorID'] != 'main': raise ValueError('appearance unavailable')
                deadline = asyncio.get_running_loop().time() + request['remainingMs'] / 1000.0
                async with asyncio.timeout_at(deadline):
                    before = await self._resolve_standing_guidance_turn(identity)
                    manager = get_session_manager_instance()
                    state = getattr(manager, 'lifecycle_context', None) if manager is not None else None
                    auth = getattr(state, 'auth_manager', None) if state is not None else None
                    owner, account, chat = before.get('owner'), before.get('ownerAccountID'), before.get('stableChatID')
                    if not auth or not owner or not account or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner:
                        raise ValueError('appearance owner unavailable')
                    def read_origin():
                        from core.database import SessionLocal
                        from sqlalchemy import text
                        from src.openclank.deliverable_appearance import validate_deliverable_appearance
                        with SessionLocal() as db:
                            row = db.execute(text("SELECT m.metadata, m.content, m.source_body_revision, m.source_body_hash FROM chat_messages m JOIN sessions s ON s.id=m.session_id WHERE m.id=:root AND m.session_id=:chat AND m.role='user' AND s.owner=:owner"),
                                {'root':identity['rootOperationID'], 'chat':chat, 'owner':owner}).one_or_none()
                            if row is None or not row[0] or len(row[0].encode('utf-8')) > 65536: return None
                            metadata = json.loads(row[0])
                            if type(metadata) is not dict or metadata.get('root_operation_id') != identity['rootOperationID']: return None
                            source = json.dumps(list(row), ensure_ascii=True, separators=(',', ':'))
                            return (validate_deliverable_appearance(metadata.get('deliverable_appearance')),
                                    hashlib.sha256(source.encode('utf-8')).hexdigest())
                    origin = await asyncio.to_thread(read_origin)
                    if (not origin or await self._resolve_standing_guidance_turn(identity) != before
                            or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner):
                        raise ValueError('appearance changed')
                    if await asyncio.to_thread(read_origin) != origin or await self._resolve_standing_guidance_turn(identity) != before:
                        raise ValueError('appearance source changed')
                    return validate_managed_method_result(method, {'kind':'deliverable_appearance','state':'ready','appearance':origin[0]})
            session_context = self._session_context.get(identity['sessionID'], {})
            if (session_context.get('personal_context_allowed') is not True
                    or session_context.get('root_operation_id') != identity['rootOperationID']):
                if request.get('kind') == 'standing_guidance':
                    return {'kind': 'standing_guidance', 'state': 'unavailable'}
                return {'state': 'skipped'}
            if request.get('kind') == 'standing_guidance':
                if identity['actorID'] != 'main': raise ValueError('guidance unavailable')
                deadline = asyncio.get_running_loop().time() + request['remainingMs'] / 1000.0
                before = await self._resolve_standing_guidance_turn(identity)
                manager = get_session_manager_instance(); state = getattr(manager, 'lifecycle_context', None) if manager is not None else None
                callback = getattr(state, 's10_guidance_select_callback', None) if state is not None else None
                if not callable(callback): raise ValueError('guidance unavailable')
                async with asyncio.timeout_at(deadline):
                    value = await callback(self, identity, deadline)
                    if (type(value) is not tuple or len(value) != 3 or type(value[0]) is not str or not value[0]
                            or len(value[0].encode('utf-8')) > 16384 or value[2] not in ('complete', 'partial')
                            or await self._resolve_standing_guidance_turn(identity) != before):
                        raise ValueError('guidance unavailable')
                    text, evidence, coverage = value; digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
                    async with self._managed_execution_lock:
                        entry = self._managed_actor_turns.get(self._managed_turn_key(identity))
                        if not isinstance(entry, dict) or entry.get('state') != 'active' or entry.get('policyRevision') != before.get('policyRevision'):
                            raise ValueError('guidance turn changed')
                        entries = entry.setdefault('contextAttestations', {})
                        if type(entries) is not dict or len(entries) >= 16: raise ValueError('guidance capacity unavailable')
                        token = secrets.token_urlsafe(24)
                        entries[token] = {'kind':'standing_guidance', 'textSHA256':digest, 'evidence':evidence}
                    return validate_managed_method_result(method, {'kind':'standing_guidance','state':'ready','text':text,'attestationID':token,'textSHA256':digest,'coverage':coverage})
            if (identity['actorID'] != 'main' or len(request['query'].encode('utf-8')) > 8192
                    or len(request['context'].encode('utf-8')) > 16000): raise ValueError('context unavailable')
            loop = asyncio.get_running_loop(); deadline = loop.time() + request['remainingMs'] / 1000.0
            s07_trace = True; phase[:] = ['entry_resolve', loop.time()]
            async with asyncio.timeout_at(deadline):
                before = await self.resolve_managed_recall_turn(identity)
                manager = get_session_manager_instance(); state = getattr(manager, 'lifecycle_context', None) if manager is not None else None
                from src.openclank.memery_preferences import owner_preferences
                recall_prefs = await asyncio.to_thread(owner_preferences, before["owner"])
                if not recall_prefs["memery_prompt_recall_enabled"]:
                    return {"state": "skipped"}
                limit = recall_prefs["memery_prompt_recall_limit"]
                remaining = recall_prefs["memery_prompt_recall_chars"]
                # Legacy and native stores expose scoped search without revision receipts.
                # A guarded store must always pass the source and revision checks below.
                from src.memery_provider import MemeryProvider
                from src.memory_provider import NativeMemoryProvider
                provider = self._memory_provider
                if (isinstance(provider, NativeMemoryProvider) or
                        isinstance(provider, MemeryProvider) and not provider._guarded_v17):
                    from src.prompt_security import untrusted_context_message
                    query = request["query"].removeprefix(f"[turn_source_id={identity['rootOperationID']}]\n")
                    mark("lookup")
                    result = await state.managed_memery_search(self, {
                        "identity": identity, "invocationID": None,
                        "search": {"query": query, "limit": limit, "tier": "curated"},
                    })
                    mark("selection")
                    rows = json.loads(result["qualified_text"])["results"]
                    citations = []
                    for row in rows[:limit]:
                        record = row.get("record") or {}
                        record_id, text = record.get("id"), record.get("content")
                        if (isinstance(record_id, str) and 0 < len(record_id) <= 512
                                and isinstance(text, str) and text.strip() and text not in request["context"]):
                            excerpt = text[:min(remaining, 1024)]
                            citations.append(f"- [Memery {record_id}] {excerpt}")
                            remaining -= len(excerpt)
                            if not remaining: break
                    if await self.resolve_managed_recall_turn(identity) != before:
                        raise ValueError("Memery turn changed")
                    if await asyncio.to_thread(owner_preferences, before["owner"]) != recall_prefs:
                        return {"state": "skipped"}
                    if not citations:
                        outcome[0] = "empty"
                        return {"state": "empty"}
                    card = untrusted_context_message("saved memory: relevant context", "\n".join(citations))
                    outcome[0] = "found"
                    return validate_managed_method_result(method, {"state": "found", "text": card["content"]})
                callback = getattr(state, 's07_context_augment_callback', None) if state is not None else None
                if not callable(callback): raise ValueError('context callback unavailable')
                key = self._managed_turn_key(identity)
                async with self._managed_execution_lock:
                    entry = self._managed_actor_turns.get(key)
                    if not isinstance(entry, dict) or entry.get('state') != 'active': raise ValueError('context turn changed')
                    boundary = _S07RecallBoundary(_S07_BOUNDARY_SEAL, self, identity, entry, before, deadline, recall_prefs)
                try:
                    await boundary.assert_current(self, identity)
                    mark('callback')
                    value = await callback(self, identity, request['query'], request['context'], deadline, _boundary=boundary)
                    mark('final_resolve')
                    if value == 'empty':
                        outcome[0] = 'empty'
                        return {'state': 'empty'}
                    if loop.time() >= deadline:
                        outcome[0] = 'timeout'
                        return {'state': 'timeout'}
                    if (type(value) is not tuple or len(value) != 2 or type(value[0]) is not str
                            or not value[0] or len(value[0].encode('utf-8')) > 16000 or type(value[1]) is not S07ContextEvidence
                            or await self.resolve_managed_recall_turn(identity) != before):
                        raise ValueError('context selection unavailable')
                    await boundary.assert_current(self, identity)
                    if await asyncio.to_thread(owner_preferences, before["owner"]) != recall_prefs:
                        return {"state": "skipped"}
                    mark('attestation')
                    text, evidence = value; digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
                    async with self._managed_execution_lock:
                        entry = self._managed_actor_turns.get(key)
                        if (entry is not boundary._entry or entry.get('state') != 'active'
                                or self._managed_provider_generation != boundary._holder or loop.time() >= deadline
                                or any(entry.get(field) != boundary.snapshot.get(field) for field in
                                       ('owner','stableChatID','authorityWorkspaceID','mappingRevision','workspaceRevision','mapRevision','policyRevision','ownerAccountID'))):
                            raise ValueError('context turn changed')
                        entries = entry.setdefault('contextAttestations', {})
                        if type(entries) is not dict or len(entries) >= 16: raise ValueError('context capacity unavailable')
                        token = secrets.token_urlsafe(24)
                        entries[token] = {'textSHA256':digest, 'evidence':evidence}
                    result = validate_managed_method_result(method, {'state':'ready','text':text,'attestationID':token,'textSHA256':digest})
                    outcome[0] = 'ready'
                    return result
                finally:
                    boundary.close()
        except asyncio.CancelledError:
            if s07_trace: outcome[0] = 'timeout' if asyncio.get_running_loop().time() >= deadline else 'cancelled'
            raise
        except Exception as exc:
            if s07_trace:
                if isinstance(exc, TimeoutError):
                    outcome[0] = 'timeout'
                    return {'state': 'timeout'}
                outcome[0] = (type(exc).__name__ if type(exc).__name__ in {'ValueError','PermissionError','RuntimeError','OSError'} else 'exception')
            return {'kind':params['kind'],'state':'unavailable'} if isinstance(params, dict) and params.get('kind') in ('standing_guidance','deliverable_appearance') else {'state':'unavailable'}
        finally:
            if trace and s07_trace:
                mark('done')
                logger.debug('S07_GATEWAY outcome=%s phases_ms=%s', outcome[0], timings)

    async def _handle_managed_context_delivered(self, params: dict) -> dict:
        """Consume the first exact context ACK using only host-held evidence."""
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/context/delivered'
        trace = logger.isEnabledFor(logging.DEBUG)
        loop = asyncio.get_running_loop()
        phase = ['validation', loop.time()]
        timings = {}
        outcome = ['rejected']
        def mark(name):
            if trace:
                now = loop.time()
                timings[phase[0]] = round(timings.get(phase[0], 0) + (now - phase[1]) * 1000, 3)
                phase[:] = [name, now]
        s07_trace = not (type(params) is dict and params.get('kind') == 'standing_guidance')
        try:
            request = validate_managed_method_request(method, params)
            fields = ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')
            identity = {field:request[field] for field in fields}
            standing = request.get('kind') == 'standing_guidance'
            proof = {field:request[field] for field in (('attestationID','textSHA256','dispatchID','bodySHA256','systemSHA256','wireProfile') if standing else ('attestationID','textSHA256','contextPartID','dispatchID','bodySHA256'))}
            if identity['actorID'] != 'main': raise ValueError('context delivery unavailable')
            resolve = self._resolve_standing_guidance_turn if standing else self.resolve_managed_recall_turn
            mark('entry_resolve')
            await resolve(identity)
            mark('attestation')
            key = self._managed_turn_key(identity)
            async with self._managed_execution_lock:
                entry = self._managed_actor_turns.get(key); entries = entry.get('contextAttestations') if entry else None
                attestation = entries.get(proof['attestationID']) if type(entries) is dict else None
                if (not isinstance(entry, dict) or entry.get('state') != 'active' or type(attestation) is not dict
                        or attestation.get('textSHA256') != proof['textSHA256']
                        or (attestation.get('kind') == 'standing_guidance') != standing): raise ValueError('context attestation changed')
                prior = attestation.get('delivery')
                if prior is not None:
                    if prior['proof'] != proof: raise ValueError('context delivery conflict')
                else:
                    manager = get_session_manager_instance(); state = getattr(manager, 'lifecycle_context', None) if manager is not None else None
                    callback = getattr(state, 's10_guidance_delivery_callback' if standing else 's07_context_delivery_callback', None) if state is not None else None
                    if not callable(callback): raise ValueError('context delivery callback unavailable')
                    prior = {'proof':proof,'task':asyncio.create_task(callback(self, identity, attestation['evidence'], proof))}
                    attestation['delivery'] = prior
                task = prior['task']
            mark('callback')
            accepted = bool(await asyncio.shield(task))
            mark('final_resolve')
            if accepted: await resolve(identity)
            mark('result')
            result = validate_managed_method_result(method, {'accepted':accepted})
            if accepted: outcome[0] = 'accepted'
            return result
        except asyncio.CancelledError:
            outcome[0] = 'cancelled'
            raise
        except Exception as exc:
            outcome[0] = ('timeout' if isinstance(exc, TimeoutError) else
                          type(exc).__name__ if type(exc).__name__ in {'ValueError','PermissionError','RuntimeError','OSError'} else 'exception')
            return {'accepted':False}
        finally:
            if trace and s07_trace:
                mark('done')
                logger.debug('S07_DELIVERY_GATEWAY outcome=%s phases_ms=%s', outcome[0], timings)

    async def _handle_managed_recall_admitted(self, params: dict) -> dict:
        """Consume only an exact active host attestation after native wire ACK."""
        import re
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = "_openclank/session/v1/recall/admitted"
        try:
            request = validate_managed_method_request(method, params)
            fields = ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')
            identity = {field:request[field] for field in fields}
            proof = {field:request[field] for field in ('toolCallID','attestationID','resultSHA256','dispatchID','bodySHA256')}
            if (any(type(proof[field]) is not str or not proof[field] or proof[field] != proof[field].strip()
                    or len(proof[field]) > 512 for field in ('toolCallID','attestationID','dispatchID'))
                    or any(type(proof[field]) is not str or re.fullmatch(r'[0-9a-f]{64}',proof[field]) is None
                           for field in ('resultSHA256','bodySHA256'))):
                raise ValueError('recall delivery unavailable')
            key = self._managed_turn_key(identity)
            async with self._managed_execution_lock:
                entry = self._managed_actor_turns.get(key)
                calls = entry.get('feedbackInvocations') if entry else None
                if not isinstance(calls, dict) or entry.get('state') != 'active' or entry.get('actorID') != 'main':
                    raise ValueError('recall delivery unavailable')
                matches = [(inv,call) for inv,call in calls.items() if isinstance(call,dict)
                    and isinstance(call.get('admissionAttestation'),dict)
                    and call['admissionAttestation'].get('attestationID') == proof['attestationID']]
                if len(matches) != 1:
                    raise ValueError('recall delivery unavailable')
                invocation_id, call = matches[0]
                attestation = call['admissionAttestation']
                if call.get('toolCallID') != proof['toolCallID'] or attestation.get('resultSHA256') != proof['resultSHA256']:
                    raise ValueError('recall delivery changed')
                prior = attestation.get('admissionDispatch')
                if prior is not None:
                    if prior['dispatchID'] == proof['dispatchID'] and prior['bodySHA256'] != proof['bodySHA256']:
                        raise ValueError('recall delivery conflict')
                else:
                    manager = get_session_manager_instance()
                    state = getattr(manager, 'lifecycle_context', None) if manager is not None else None
                    callback = getattr(state, 's06_admitted_callback', None) if state is not None else None
                    if not callable(callback):
                        raise ValueError('recall delivery callback unavailable')
                    task = asyncio.create_task(callback(self, identity, invocation_id, attestation['observations'], proof))
                    prior = {'dispatchID':proof['dispatchID'], 'bodySHA256':proof['bodySHA256'], 'task':task}
                    attestation['admissionDispatch'] = prior
                task = prior['task']
            accepted = bool(await asyncio.shield(task))
            if accepted:
                await self.resolve_managed_feedback_invocation(identity, invocation_id)
            return validate_managed_method_result(method, {'accepted':accepted})
        except asyncio.CancelledError:
            raise
        except Exception:
            return {'accepted':False}

    async def run_managed_feedback_call(self, identity: dict, invocation_id: str, request_hash: str, operation):
        """Run one fixed host recall; same-call retries share its actual outcome."""
        if (type(request_hash) is not str or len(request_hash) != 64
            or any(c not in '0123456789abcdef' for c in request_hash) or not callable(operation)):
            raise ValueError('feedback invocation unavailable')
        await self.resolve_managed_feedback_invocation(identity, invocation_id)
        key = self._managed_turn_key(identity)
        async with self._managed_execution_lock:
            entry = self._managed_actor_turns.get(key)
            calls = entry.get('feedbackInvocations') if entry else None
            call = calls.get(invocation_id) if isinstance(calls, dict) else None
            if not isinstance(call, dict) or entry.get('state') != 'active':
                raise ValueError('feedback invocation unavailable')
            if call.get('requestHash') not in (None, request_hash):
                raise ValueError('feedback invocation request conflict')
            task = call.get('task')
            if task is None:
                if call.get('started'):
                    raise ValueError('feedback invocation outcome unavailable')
                call['requestHash'] = request_hash
                call['started'] = True
                task = asyncio.create_task(operation())
                call['task'] = task
            if not isinstance(task, asyncio.Task):
                raise ValueError('feedback invocation outcome unavailable')
        return await asyncio.shield(task)

    async def _input_origin_current(self, identity, generation, digest):
        """Recheck a complete logical source set; no provider-wire claim is made."""
        try:
            key=self._managed_turn_key(identity)
            async with self._managed_execution_lock:
                entry=self._managed_actor_turns.get(key)
                if not isinstance(entry,dict) or entry.get('state') != 'active': return False
                state=entry.get('inputOriginSet')
                privacy=copy.deepcopy(entry.get('receiptPrivacy'))
                if privacy is not None and (type(privacy) is not dict or type(privacy.get('receipts')) is not tuple): return False
                if state is None:
                    return (identity['actorID'] != 'main' and generation == 0 and digest == hashlib.sha256(b'[]').hexdigest()
                            and (privacy is None or privacy['receipts'] == ()))
                if (not state.get('valid') or state.get('generation') != generation or state.get('sha256') != digest): return False
                receipts=copy.deepcopy(state['receipts']); root_session=entry['rootSessionID']
                if identity['actorID']=='main' and identity['sessionID']==root_session:
                    original=entry.get('forwardPairInput'); root=state.get('root')
                    if (type(original) is not dict or type(root) is not dict or original.get('identity')!=identity
                            or any(original.get(src)!=root.get(dst) for src,dst in
                                (('messageID','rootMessageID'),('partID','rootPartID'),('textSHA256','rootTextSHA256')))):return False
                admitted={(item['requestID'],item['payloadHash']) for item in receipts}
                if privacy is not None and any(item not in admitted for item in privacy['receipts']): return False
                owner=entry['owner']; account=entry['ownerAccountID']; chat=entry['stableChatID']; holder=self._managed_provider_generation
            from core.models import get_session_manager_instance
            manager=get_session_manager_instance(); lifecycle=getattr(manager,'lifecycle_context',None) if manager else None
            auth=getattr(lifecycle,'auth_manager',None) if lifecycle else None
            supervisor=getattr(lifecycle,'mimo_supervisor',None) if lifecycle else None
            pool=supervisor.owner_lifecycle_pool() if supervisor else None
            worker=pool.worker_for_owner(owner) if pool else None
            if (not auth or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
                    or not worker or worker._bridge is not self or not worker.is_alive() or not holder): return False
            binding=await self._handle_session_binding_read({'sessionID':root_session})
            if identity['actorID']=='main' and identity['sessionID']==root_session:
                response=await worker.session_http_request(chat,'GET','message/'+urllib.parse.quote(root['rootMessageID'],safe=''),owner=owner)
                if response.status_code!=200 or len(response.content)>1024*1024:return False
                message=response.json()
                if (type(message) is not dict or type(message.get('info')) is not dict or type(message.get('parts')) is not list
                        or message['info'].get('id')!=root['rootMessageID'] or message['info'].get('sessionID')!=identity['sessionID']
                        or message['info'].get('role')!='user' or message['info'].get('agentID') not in (None,'main')):return False
                parts=message['parts']; originals=original.get('layout')
                if type(originals) is not list:return False
                visible=[part for part in parts if type(part) is dict and part.get('type')=='text' and not part.get('ignored')]
                if len(visible)<len(originals) or any(part.get('type')!='text' for part in parts):return False
                for expected,part in zip(originals,visible):
                    if (type(part.get('text')) is not str or bool(part.get('synthetic'))!=expected.get('synthetic')
                            or hashlib.sha256(part['text'].encode('utf-8')).hexdigest()!=expected.get('sha256')):return False
                if any(not part.get('synthetic') for part in visible[len(originals):]):return False
                matches=[part for part in visible if part.get('id')==root['rootPartID']]
                if (len(matches)!=1 or matches[0].get('synthetic')
                        or hashlib.sha256(matches[0]['text'].encode('utf-8')).hexdigest()!=root['rootTextSHA256']):return False
            suffix='actors/' + urllib.parse.quote(identity['actorID'],safe='') + '/input'
            fields=('owner','stableChatID','sessionID','actorID','requestID','payloadHash','mapRevision','mappingRevision','workspaceRevision','executionRevision','messageID','firstIngressNonce')
            for item in receipts:
                if root_session == identity['sessionID']:
                    response=await worker.session_http_request(chat,'GET',suffix+'?requestID='+urllib.parse.quote(item['requestID'],safe=''),owner=owner)
                else:
                    response=await worker.session_child_input_request(chat,identity['sessionID'],'GET',suffix,None,owner=owner,request_id=item['requestID'])
                if response.status_code != 200:return False
                rows=response.json()
                if type(rows) is not list or len(rows)!=1 or type(rows[0]) is not dict:return False
                row=rows[0];queue=row.get('queueDisposition')
                if (row.get('state')!='consumed' or type(queue) is not dict or queue.get('delivered') is not True
                        or any(row.get(field)!=item.get(field) for field in fields)
                        or queue.get('receiptID')!=item['queueReceiptID'] or queue.get('deliveryMessageID')!=item['deliveryMessageID']):return False
            if await self._handle_session_binding_read({'sessionID':root_session}) != binding:return False
            async with self._managed_execution_lock:
                context=self._session_context.get(root_session); registry=self._receipt_origin_registry.get(root_session)
                if (self._managed_actor_turns.get(key) is not entry or entry.get('inputOriginSet') is not state
                        or entry.get('receiptPrivacy') != privacy
                        or (identity['actorID']=='main' and identity['sessionID']==root_session and entry.get('forwardPairInput') is not original)
                        or self._managed_provider_generation!=holder or pool.worker_for_owner(owner) is not worker
                        or worker._bridge is not self or not worker.is_alive()
                        or (receipts and (not registry or registry.get('context') is not context))):return False
                now=time.monotonic()
                for item in receipts:
                    slot=registry['slots'].get((owner,account,chat,item['sessionID'],item['actorID'],item['requestID']))
                    if (not slot or slot.get('nonce')!=item.get('firstIngressNonce') or slot.get('payloadHash')!=item['payloadHash']
                            or slot.get('holder')!=holder or slot.get('expires',0)<=now or slot.get('binding')!=binding
                            or not callable(slot.get('current')) or slot['current']() is not True):return False
                return True
        except Exception:
            return False

    async def _handle_input_origin_sync(self, params: dict) -> dict:
        """Fence consumption and retain only a complete, re-read logical source set."""
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = '_openclank/session/v1/input-origin/sync'
        request = validate_managed_method_request(method, params)
        identity = {name:request[name] for name in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
        key = self._managed_turn_key(identity)
        generation = request['sourceSetGeneration']
        async with self._managed_execution_lock:
            entry = self._managed_actor_turns.get(key)
            if not isinstance(entry, dict) or entry.get('state') != 'active':
                raise ValueError('input origin turn unavailable')
            state = entry.get('inputOriginSet') or {'generation':0, 'valid':True, 'receipts':[], 'root':None}
            if request['phase'] == 'invalidate':
                if generation != state['generation'] + 1:
                    return validate_managed_method_result(method, {'accepted':False,'sourceSetGeneration':generation})
                entry['inputOriginSet'] = {'generation':generation, 'valid':False, 'receipts':state['receipts'], 'root':state.get('root')}
                return validate_managed_method_result(method, {'accepted':True,'sourceSetGeneration':generation})
            if generation != state['generation'] or state.get('valid'):
                return validate_managed_method_result(method, {'accepted':False,'sourceSetGeneration':generation})
            receipts = request['receipts']
            digest = hashlib.sha256(json.dumps(receipts,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()
            if (digest != request['sourceSetSHA256'] or len(receipts) < len(state['receipts'])
                    or receipts[:len(state['receipts'])] != state['receipts']
                    or len({item['requestID'] for item in receipts}) != len(receipts)):
                return validate_managed_method_result(method, {'accepted':False,'sourceSetGeneration':generation})
            original_state = state
            root_session = entry.get('rootSessionID')
            root_fields=('rootMessageID','rootPartID','rootTextSHA256')
            root={field:request[field] for field in root_fields} if all(field in request for field in root_fields) else None
            if state.get('root') is not None and root != state['root']:
                return validate_managed_method_result(method, {'accepted':False,'sourceSetGeneration':generation})
            if identity['actorID']=='main' and identity['sessionID']==root_session:
                original=entry.get('forwardPairInput')
                if (type(root) is not dict or type(original) is not dict or original.get('identity')!=identity
                        or any(original.get(src)!=root.get(dst) for src,dst in
                            (('messageID','rootMessageID'),('partID','rootPartID'),('textSHA256','rootTextSHA256')))):
                    return validate_managed_method_result(method, {'accepted':False,'sourceSetGeneration':generation})
            owner = entry.get('owner')
            account = entry.get('ownerAccountID')
            chat = entry.get('stableChatID')
            holder = self._managed_provider_generation
        try:
            from core.models import get_session_manager_instance
            manager = get_session_manager_instance()
            lifecycle = getattr(manager,'lifecycle_context',None) if manager else None
            supervisor = getattr(lifecycle,'mimo_supervisor',None) if lifecycle else None
            pool = supervisor.owner_lifecycle_pool() if supervisor else None
            worker = pool.worker_for_owner(owner) if pool else None
            auth = getattr(lifecycle,'auth_manager',None) if lifecycle else None
            if (not worker or worker._bridge is not self or not worker.is_alive() or not holder
                    or not auth or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
):
                raise ValueError('input origin reader unavailable')
            suffix = 'actors/' + urllib.parse.quote(identity['actorID'],safe='') + '/input'
            fields=('owner','stableChatID','sessionID','actorID','requestID','payloadHash','mapRevision','mappingRevision','workspaceRevision','executionRevision','messageID','firstIngressNonce')
            for item in receipts:
                if root_session == identity['sessionID']:
                    response = await worker.session_http_request(chat,'GET',suffix + '?requestID=' +
                        urllib.parse.quote(item['requestID'],safe=''),owner=owner)
                else:
                    response = await worker.session_child_input_request(chat,identity['sessionID'],'GET',suffix,None,
                        owner=owner,request_id=item['requestID'])
                if response.status_code != 200: raise ValueError('input origin receipt unavailable')
                rows = response.json()
                if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict:
                    raise ValueError('input origin receipt unavailable')
                row=rows[0]; queue=row.get('queueDisposition')
                if (row.get('state') != 'consumed' or type(queue) is not dict or queue.get('delivered') is not True
                        or any(row.get(field) != item.get(field) for field in fields)
                        or queue.get('receiptID') != item['queueReceiptID'] or queue.get('deliveryMessageID') != item['deliveryMessageID']
                        or item['owner'] != owner or item['stableChatID'] != chat or item['sessionID'] != identity['sessionID']
                        or item['actorID'] != identity['actorID'] or not item.get('firstIngressNonce')):
                    raise ValueError('input origin receipt changed')
            async with self._managed_execution_lock:
                context=self._session_context.get(root_session)
                registry=self._receipt_origin_registry.get(root_session)
                if (self._managed_actor_turns.get(key) is not entry or entry.get('inputOriginSet') is not original_state
                        or self._managed_provider_generation != holder or pool.worker_for_owner(owner) is not worker
                        or worker._bridge is not self or not worker.is_alive()
                        or (receipts and (not registry or registry.get('context') is not context))):
                    raise ValueError('input origin scope changed')
                slots=registry['slots'] if registry else {}; now=time.monotonic()
                for item in receipts:
                    slot=slots.get((owner,account,chat,item['sessionID'],item['actorID'],item['requestID']))
                    if (not slot or slot.get('nonce') != item['firstIngressNonce'] or slot.get('payloadHash') != item['payloadHash']
                            or slot.get('holder') != holder or slot.get('expires',0) <= now
                            or any(slot['binding'].get(k) != item[k] for k in ('mapRevision','mappingRevision','workspaceRevision'))
                            or not callable(slot.get('current')) or slot['current']() is not True):
                        raise ValueError('input origin witness unavailable')
                entry['inputOriginSet']={'generation':generation,'valid':True,'receipts':copy.deepcopy(receipts),'sha256':digest,'root':root}
            return validate_managed_method_result(method, {'accepted':True,'sourceSetGeneration':generation,'sourceSetSHA256':digest})
        except Exception:
            return validate_managed_method_result(method, {'accepted':False,'sourceSetGeneration':generation})

    async def _handle_input_policy_resolve(self, params: dict) -> dict:
        from src.openclank.managed_protocol import validate_managed_method_request

        request = validate_managed_method_request(
            "_openclank/session/v1/input-policy/resolve", params
        )
        identity = {key: request[key] for key in (
            "sessionID", "rootOperationID", "actorID", "executionRevision", "innerTurnID"
        )}
        if self._managed_turn_key(identity) not in self._managed_actor_turns:
            raise ValueError("input policy resolution requires an active managed turn")
        # The normal/O08 host policy resolver is installed by the chat route
        # admission path. Until it is available for this exact root, leave all
        # receipts pending instead of consuming under stale or guessed grants.
        resolver = getattr(self, "_managed_input_policy_resolver", None)
        if resolver is None:
            raise ValueError("current managed input policy is unavailable")
        result = await resolver(identity, request["receipts"])
        from src.openclank.managed_protocol import validate_managed_method_result

        return validate_managed_method_result(
            "_openclank/session/v1/input-policy/resolve", result
        )

    async def resolve_managed_search_context(
        self,
        session_id: str,
        root_operation_id: str,
    ) -> dict[str, str]:
        """Return host-derived search authority for an active admitted turn.

        Session/root/caller values are claims until the existing binding and
        active-turn state accept them.  Caller is deliberately read only from
        trusted host context; model tool options are not consulted.
        """
        session = str(session_id or "").strip()
        root = str(root_operation_id or "").strip()
        if not session or not root:
            raise ValueError("managed search session and root operation are required")
        binding = await self._handle_session_binding_read({"sessionID": session})
        if self._managed_provider_generation is None:
            raise ValueError("managed provider generation is unavailable")
        context = self._session_context.get(session) or {}
        if str(context.get("root_operation_id") or "") != root:
            raise ValueError("managed search root operation is not active")
        if session not in self._turns:
            raise ValueError("managed search turn is not active")
        caller = str(context.get("trusted_caller") or "").strip().lower()
        if caller not in {"chat", "code", "research"}:
            raise ValueError("managed search caller is not trusted")
        if binding.get("owner") != self._owner or binding.get("engineSessionID") != session:
            raise ValueError("managed search binding identity mismatch")
        return {
            "owner": self._owner,
            "holderID": self._managed_provider_generation,
            "sessionID": session,
            "stableChatID": binding["stableChatID"],
            "rootOperationID": root,
            "caller": caller,
        }

    async def _bind_engine_session(
        self,
        chat_id: str,
        engine_id: str,
        *,
        expected_current: Optional[str] = None,
        expected_mapping_revision: Optional[int] = None,
        guard=None,
    ) -> dict:
        self._assert_host_directory_execution_active(chat_id, self._owner)
        # Per-chat create/remap CAS uses only the chat's own current/mapping
        # evidence.  A pre-read owner-wide map revision is not a valid expected
        # axis here: an unrelated chat's bind/remap/forget would otherwise fail
        # this chat even though its own mapping is unchanged.
        try:
            item = await self._durable_session_map.async_bind(
                chat_id,
                engine_id,
                expected_map_revision=None,
                expected_current=expected_current,
                expected_mapping_revision=expected_mapping_revision,
                guard=guard,
            )
        except Exception:
            # os.replace() may have succeeded while its directory fsync
            # reported an error. Re-read the authority before treating the
            # admission as lost; this is safe because the expected-current
            # CAS remains the winner test.
            current = await self._durable_session_map.async_lookup(chat_id)
            if not current or current.get("current") != engine_id:
                raise
            map_revision, mapping_revision = await self._durable_session_map.async_revisions(chat_id)
            item = dict(current)
            item.update({"mapRevision": map_revision, "mappingRevision": mapping_revision})
        self._session_map = await self._durable_session_map.async_flat_current()
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
        context = self._session_context.get(mimo_session_id) or {}
        chat_id = context.get('odysseus_session_id')
        if not chat_id and self._kernel_scopes:
            raise ValueError('kernel session retirement binding unavailable')
        await self._retire_python_kernels(mimo_session_id, chat_id)
        await self.question_handler.reject_session(mimo_session_id)
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

    def _bind_canonical_session(self, *args, **kwargs) -> None:
        if not self._provider_root_sync_guard.acquire(blocking=False):
            raise RuntimeError('provider root lifecycle is busy')
        try:
            return self._bind_canonical_session_unlocked(*args, **kwargs)
        finally:
            self._provider_root_sync_guard.release()

    def _bind_canonical_session_unlocked(
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
        defer_compensation_fence: bool = False,
    ) -> None:
        self._refuse_sync_provider_root_forget(mimo_session)
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
        self._persist_session_state(mimo_session, persist_binding=True, defer_compensation_fence=defer_compensation_fence)

    async def _complete_compensation_fence(self, exc: _BindingCompensationNeedsFence, *, guard=None) -> None:
        # Cancellation must not let a failed compensation escape before its
        # fail-closed fence has either completed or reported its own failure.
        task = asyncio.create_task(self._fence_admission_async(exc.chat_id, exc.reason, guard=guard))
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.done():
                    task.result()
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def _bind_canonical_session_async(self, *args, guard=None, **kwargs) -> None:
        chat_id = args[1]
        if guard is None:
            async with self._durable_session_map.async_chat_admission_lock(chat_id) as acquired:
                return await self._bind_canonical_session_async(*args, guard=acquired, **kwargs)
        self._durable_session_map.validate_chat_guard(chat_id, guard)
        entry = await self._durable_session_map.async_lookup(chat_id)
        if not entry or entry.get("current") != args[0]:
            raise RuntimeError("managed binding changed before canonical state persistence")
        try:
            self._bind_canonical_session(*args, **kwargs, defer_compensation_fence=True)
        except _BindingCompensationNeedsFence as exc:
            await self._complete_compensation_fence(exc, guard=guard)
            raise exc.original from exc

    def _persist_session_state(
        self,
        mimo_session: str,
        *,
        required: bool = False,
        expected_workspace_revision: Optional[int] = None,
        persist_binding: bool = False,
        defer_compensation_fence: bool = False,
    ) -> bool:
        context = self._session_context.get(mimo_session)
        state = self._session_state.get(mimo_session)
        if context and (context.get("incognito") or context.get("auxiliary")):
            if required:
                raise RuntimeError("private session state cannot be persisted")
            return False
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
        except Exception as persist_error:
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
                    if defer_compensation_fence:
                        raise _BindingCompensationNeedsFence(context["odysseus_session_id"], "state compensation", persist_error) from persist_error
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
                    if defer_compensation_fence:
                        raise _BindingCompensationNeedsFence(context["odysseus_session_id"], "state deletion compensation", persist_error) from persist_error
                    self._fence_admission(context.get("odysseus_session_id"), "state deletion compensation")
            raise

    def negotiated_state(self, odysseus_session: str) -> dict:
        mimo_session = self._session_map.get(odysseus_session)
        if mimo_session and mimo_session in self._session_state:
            state = dict(self._session_state[mimo_session])
        else:
            try:
                from src.openclank.transcript_projection import get_mimo_state

                state = dict(get_mimo_state(odysseus_session))
            except KeyError:
                return {}
        pending = state.get("pending_question") or {}
        req = self.question_handler.pending_requests.get(pending.get("request_id"))
        if not req or req._future.done() or req.odysseus_session_id != odysseus_session or req.mimo_session_id != mimo_session:
            state.pop("pending_question", None)
        return state

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
        return await self._set_session_config_option(mimo_session, config_id, value)

    async def _set_session_config_option(self, mimo_session: str, config_id: str, value: str) -> dict:
        state = self._session_state.get(mimo_session, {})
        if config_id == "agent_profile":
            # This is a host next-turn selection, distinct from public modes.
            eligible = {item.get("id") for item in (state.get("modes") or {}).get("availableModes", []) if isinstance(item, dict) and item.get("id") not in {"plan", "chat"}}
            if value and value not in eligible:
                raise ValueError("Named profile is not currently eligible for this session")
            state.setdefault("desired", {})["agent_profile"] = value
            self._persist_session_state(mimo_session)
            return dict(state)
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

    def _plan_question_context(self, session_id: str) -> dict:
        from src.openclank.transcript_projection import get_mimo_state
        context = dict(self._session_context.get(session_id) or {})
        try:
            state = get_mimo_state(context["odysseus_session_id"], owner=context["owner"])
            if state.get("managed_binding") != context.get("managed_binding"):
                raise ValueError("binding changed")
            plan = state.get("plan_state") or {}
            context.update(plan_revision=int(plan.get("revision") or 0), plan_digest=str(plan.get("digest") or ""), plan_account=getattr(self._turns.get(session_id), "owner_account_id", None))
        except (KeyError, ValueError, TypeError):
            context.update(plan_revision=0, plan_digest="")
        return context

    async def _surface_question(self, req: "PendingQuestion") -> None:
        q = self._queues.get(req.mimo_session_id)
        if q is None:
            raise RuntimeError(f"no active turn for session {req.mimo_session_id!r}")
        if req.blocking:
            state = self._session_state.setdefault(req.mimo_session_id, {})
            state["pending_question"] = req.payload()
            self._persist_session_state(req.mimo_session_id)
        await q.put({"sessionUpdate": "_question_request", "request": req})

    async def _clear_question(self, req: "PendingQuestion") -> None:
        state = self._session_state.get(req.mimo_session_id)
        if state is not None and (state.get("pending_question") or {}).get("request_id") == req.request_id:
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
            if approved and req.plan_digest and req.plan_account and getattr(self._turns.get(req.mimo_session_id), "owner_account_id", None) == req.plan_account:
                try:
                    def apply(plan):
                        if (plan.get("status") == "cleared" or plan.get("slice_coverage") == "unavailable"
                                or plan.get("revision") != req.plan_revision or plan.get("digest") != req.plan_digest):
                            raise ValueError("plan approval is stale")
                        plan.update(approved_revision=req.plan_revision, approved_digest=req.plan_digest, status="approved")
                        return plan
                    self._mutate_canonical_plan(req.mimo_session_id, apply)
                except (ValueError, KeyError, RuntimeError):
                    logger.info("Plan question approval unavailable")
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

    def _managed_history_tool_timing(self, item, session_id, owner, chat_id):
        from src.openclank.tool_timing_proof import verify_tool_timing, seal_tool_timing, verify_tool_activity, seal_tool_activity
        worker = getattr(self._managed_actor_execution_reader, "__self__", None)
        holder = self._managed_provider_generation
        process = getattr(worker, "_proc", None)
        if (worker is None or not holder or process is None or getattr(worker, "_bridge", None) is not self
                or getattr(worker, "_client", None) is not self._client or not worker.is_alive()
                or getattr(getattr(worker, "_managed_callbacks", None), "holder_id", None) != holder
                or self._managed_history_binding(session_id) != (owner, chat_id)):
            return None
        activity = isinstance(item.get('toolTiming'),dict) and item['toolTiming'].get('version') == 2
        evidence = verify_tool_activity(self._client, item, session_id) if activity else verify_tool_timing(self._client, item, session_id)
        if (evidence is None or getattr(worker, "_proc", None) is not process
                or getattr(worker, "_bridge", None) is not self or getattr(worker, "_client", None) is not self._client
                or not worker.is_alive() or self._managed_provider_generation != holder):
            return None
        from src.openclank.conversation_archive import get_conversation_archive
        account=get_conversation_archive().resolve_owner(owner)
        if activity:
            from core.models import get_session_manager_instance
            manager=get_session_manager_instance(); lifecycle=getattr(manager,'lifecycle_context',None) if manager else None
            auth=getattr(lifecycle,'auth_manager',None); barrier=getattr(lifecycle,'account_request_barrier',None)
            key=tuple(evidence[k] for k in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID'))
            entry=self._managed_actor_turns.get(key); root=self._turns.get(entry.get('rootSessionID')) if isinstance(entry,dict) else None
            if (not isinstance(entry,dict) or entry.get('state')!='active' or entry.get('owner')!=owner
                    or entry.get('ownerAccountID')!=account or entry.get('stableChatID')!=chat_id or entry.get('authority')
                    or entry.get('holderID')!=holder or self._managed_actor_executions.get(key[:4]) is not entry
                    or getattr(self,'_ownerAdmissionHolderStopped',False) or entry.get('ownerAdmissionStopped')
                    or barrier is None or entry.get('ownerAdmissionBarrier') is not barrier or entry.get('ownerAdmissionAccount')!=account
                    or entry.get('ownerAdmission') is None or getattr(entry['ownerAdmission'],'released',True)
                    or not callable(entry.get('originalCredentialCurrent')) or entry['originalCredentialCurrent']() is not True
                    or auth is None or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
                    or root is None or root.root_operation_id!=evidence['rootOperationID'] or root.owner_account_id!=account
                    or root.privacy_capture_veto):return None
            barrier.assert_live_admission(owner,entry['ownerAdmission'])
            return seal_tool_activity(account,chat_id,holder,evidence)
        return seal_tool_timing(account, chat_id, holder, evidence)

    async def _managed_history_query(self, params: dict) -> dict:
        if params.get('operation') not in ('exact_output', 'check_sources'):
            return await self._managed_history_callbacks.dispatch('_openclank/history/v1/query', params)
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        from core.models import get_session_manager_instance
        import sqlite3
        method = '_openclank/history/v1/query'
        request = validate_managed_method_request(method, params)
        operation = request['operation']
        unavailable = {'ok': False, 'operation': operation, 'result': (
            {'state': 'unavailable', 'page': None} if operation == 'exact_output'
            else {'state': 'unavailable', 'sources': []})}
        identity = {k: request[k] for k in ('sessionID', 'rootOperationID', 'actorID', 'executionRevision', 'innerTurnID')}
        key = self._managed_turn_key(identity)
        async def current():
            async with self._managed_execution_lock:
                entry = self._managed_actor_turns.get(key)
                if not isinstance(entry, dict) or entry.get('state') != 'active': return None
                owner, account, chat = entry.get('owner'), entry.get('ownerAccountID'), entry.get('stableChatID')
                barrier, admission = entry.get('ownerAdmissionBarrier'), entry.get('ownerAdmission')
                policy = entry.get('policyRevision')
            manager = get_session_manager_instance()
            lifecycle = getattr(manager, 'lifecycle_context', None) if manager else None
            auth = getattr(lifecycle, 'auth_manager', None)
            supervisor = getattr(lifecycle, 'mimo_supervisor', None)
            pool = supervisor.owner_lifecycle_pool() if supervisor is not None else None
            worker = pool.worker_for_owner(owner) if pool and owner else None
            session = manager.get_session(chat) if manager and chat else None
            context = self._session_context.get(identity['sessionID']) or {}
            if (not owner or not account or not chat or not auth or not barrier or not admission
                    or not await barrier.is_live(owner, admission) or auth.account_id(owner) != account
                    or auth.username_for_account_id(account) != owner or not session or session.owner != owner
                    or getattr(session, 'incognito', False) or getattr(session, 'archived', False)
                    or context.get('incognito') or context.get('auxiliary') or not worker
                    or not worker.is_alive() or getattr(worker, '_bridge', None) is not self
                    or self._managed_provider_generation != entry.get('holderID')): return None
            resolved = await self.resolve_managed_actor_execution(identity['sessionID'], identity['rootOperationID'],
                                                                  identity['actorID'], identity['executionRevision'])
            if (resolved.get('owner') != owner or resolved.get('stableChatID') != chat
                    or not resolved.get('holderID') or resolved.get('holderID') != entry.get('holderID')
                    or self._managed_history_binding(identity['sessionID']) != (owner, chat)): return None
            admitted = await self._handle_managed_tool_admit({**identity, 'toolName': 'browser_observe' if request.get('browserSnapshotRepeat') is not None else 'search_chats', 'kind': 'tool'})
            if (not admitted.get('allowed') or not await barrier.is_live(owner, admission)
                    or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                    or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker or not worker.is_alive()
                    or getattr(worker, '_bridge', None) is not self): return None
            async with self._managed_execution_lock:
                if (self._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                        or entry.get('policyRevision') != policy or entry.get('ownerAccountID') != account): return None
            return (entry, owner, account, chat, barrier, admission, policy, worker)
        async def row_preference_current(observed):
            selector = request.get('rowViewAdmission')
            if selector is None: return True
            try:
                entry, owner, account, chat, barrier, admission, policy, worker = observed
                root = self._turns.get(entry.get('rootSessionID'))
                retained = getattr(root, 'row_view_preference', None) if root is not None else None
                if (type(retained) is not tuple or len(retained) != 3 or root.root_operation_id != identity['rootOperationID']
                        or root.owner_account_id != account or root.provider_root_closing): return False
                preference, lease, holder = retained
                from src.agent_settings import AdmittedGcfPreference, agent_settings_revision, validate_agent_settings
                from src.openclank.mimo_supervisor import AgentWorkerLease
                if (type(preference) is not AdmittedGcfPreference or (preference.gcf_same_request_rows_requested is not True and preference.gcf_graph_references_requested is not True)
                        or preference.requested is not True or selector['settingsRevision'] != preference.revision
                        or type(lease) is not AgentWorkerLease or lease._released or lease.owner != owner
                        or lease.worker is not worker or lease.generation != getattr(worker, 'installed_generation', None)
                        or holder != entry.get('holderID') or holder != self._managed_provider_generation): return False
                def read_preference():
                    from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
                    with PREFERENCE_LOCK:
                        fresh = validate_agent_settings((_load_for_user(owner) or {}).get('agent_settings', {}))
                        return ((fresh.get('tokenomics', {}).get('gcf_same_request_rows') is True or fresh.get('tokenomics', {}).get('gcf_graph_references') is True)
                                and agent_settings_revision(fresh) == preference.revision)
                if not await asyncio.to_thread(read_preference): return False
                return (self._turns.get(entry.get('rootSessionID')) is root and root.row_view_preference is retained
                        and not lease._released and lease.worker is worker and worker.is_alive()
                        and lease.generation == getattr(worker, 'installed_generation', None)
                        and holder == self._managed_provider_generation and root.root_operation_id == identity['rootOperationID'])
            except Exception: return False
        async def session_preference_current(observed):
            selector = request.get('sessionViewAdmission')
            if selector is None: return True
            try:
                entry, owner, account, chat, barrier, admission, policy, worker = observed
                root = self._turns.get(entry.get('rootSessionID'))
                retained = getattr(root, 'row_view_preference', None) if root is not None else None
                if (type(retained) is not tuple or len(retained) != 3 or root.root_operation_id != identity['rootOperationID']
                        or root.owner_account_id != account or root.provider_root_closing): return False
                preference, lease, holder = retained
                from src.agent_settings import AdmittedGcfPreference, agent_settings_revision, validate_agent_settings
                from src.openclank.mimo_supervisor import AgentWorkerLease
                if (type(preference) is not AdmittedGcfPreference or preference.gcf_session_view_requested is not True
                        or preference.requested is not True or selector['settingsRevision'] != preference.revision
                        or type(lease) is not AgentWorkerLease or lease._released or lease.owner != owner
                        or lease.worker is not worker or lease.generation != getattr(worker, 'installed_generation', None)
                        or holder != entry.get('holderID') or holder != self._managed_provider_generation): return False
                def read_preference():
                    from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
                    with PREFERENCE_LOCK:
                        fresh = validate_agent_settings((_load_for_user(owner) or {}).get('agent_settings', {}))
                        return (fresh.get('tokenomics', {}).get('gcf_session_view') is True
                                and agent_settings_revision(fresh) == preference.revision)
                if not await asyncio.to_thread(read_preference): return False
                return (self._turns.get(entry.get('rootSessionID')) is root and root.row_view_preference is retained
                        and not lease._released and lease.worker is worker and worker.is_alive()
                        and lease.generation == getattr(worker, 'installed_generation', None)
                        and holder == self._managed_provider_generation and root.root_operation_id == identity['rootOperationID'])
            except Exception: return False
        async def browser_preference_current(observed):
            selector = request.get('browserSnapshotRepeat')
            if selector is None: return True
            try:
                entry, owner, account, chat, barrier, admission, policy, worker = observed
                sources=request['sources']
                if (len(sources)!=2 or sources[0]['partID']==sources[1]['partID']
                        or identity['actorID']!='main' or identity['sessionID']!=entry.get('rootSessionID')): return False
                root=self._turns.get(entry.get('rootSessionID'))
                retained=getattr(root,'browser_snapshot_preference',None) if root is not None else None
                offer=entry.get('browserSnapshotRepeatAdmission')
                if (type(retained) is not tuple or len(retained)!=3 or type(offer) is not dict
                        or root.root_operation_id!=identity['rootOperationID'] or root.owner_account_id!=account
                        or root.provider_root_closing or offer.get('settingsRevision')!=selector['settingsRevision']): return False
                pref, lease, holder=retained
                from src.agent_settings import AdmittedBrowserSnapshotPreference, agent_settings_revision, validate_agent_settings
                from src.openclank.mimo_supervisor import AgentWorkerLease
                if (type(pref) is not AdmittedBrowserSnapshotPreference or pref.requested is not True or pref.revision!=selector['settingsRevision']
                        or type(lease) is not AgentWorkerLease or lease._released or lease.owner!=owner or lease.worker is not worker
                        or lease.generation!=getattr(worker,'installed_generation',None) or holder!=entry.get('holderID')
                        or holder!=self._managed_provider_generation): return False
                def read_preference():
                    from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
                    with PREFERENCE_LOCK:
                        fresh=validate_agent_settings((_load_for_user(owner) or {}).get('agent_settings',{}))
                        return fresh.get('tokenomics',{}).get('browser_snapshot_repeat') is True and agent_settings_revision(fresh)==pref.revision
                if not await asyncio.to_thread(read_preference): return False
                resolved=await self.resolve_managed_actor_execution(identity['sessionID'],identity['rootOperationID'],identity['actorID'],identity['executionRevision'])
                binding=self._managed_actor_provider_binding(identity,resolved)
                if (binding['connection_id']!=offer.get('connectionID') or binding['provider_id']!=offer.get('providerID')
                        or binding['model_id']!=offer.get('modelID') or binding['model_route_id']!=offer.get('modelRouteID')): return False
                from src.openclank.browser_control_service import configured_browser_control_service
                service=configured_browser_control_service()
                workspace=entry.get('authorityWorkspaceID');revision=entry.get('workspaceRevision')
                if service is None or not workspace or type(revision) is not int: return False
                if service.current_turn_policy_revision(owner=owner,host_session_id=chat,workspace_id=workspace,
                        binding_revision=revision,tool_name='browser_observe',managed_identity=identity) is None: return False
                service.registry.resolve(target_id=selector['targetID'],owner_id=owner,host_session_id=chat,workspace_id=workspace,
                    binding_revision=revision,connection_generation=selector['connectionGeneration'],
                    navigation_generation=selector['navigationGeneration'],observation_generation=selector['latestObservationGeneration'])
                return (self._turns.get(entry.get('rootSessionID')) is root and root.browser_snapshot_preference is retained
                        and not lease._released and worker.is_alive() and holder==self._managed_provider_generation)
            except Exception: return False
        try:
            if operation == 'check_sources':
                import time
                deadline = time.monotonic() + 0.5
                async with asyncio.timeout(0.5):
                    if sum(request.get(k) is not None for k in ('rowViewAdmission','sessionViewAdmission','browserSnapshotRepeat')) > 1:
                        return validate_managed_method_result(method, unavailable)
                    before = await current()
                    if before is None or not await row_preference_current(before) or not await session_preference_current(before) or not await browser_preference_current(before): return validate_managed_method_result(method, unavailable)
                    _, _, account, chat, _, _, _, _ = before
                    sources = self._managed_history_callbacks.check_sources(
                        request['sources'], account=account, chat=chat, actor=identity['actorID'], deadline=deadline)
                    after = await current()
                    if after != before or sources is None: return validate_managed_method_result(method, unavailable)
                    again = self._managed_history_callbacks.check_sources(
                        request['sources'], account=account, chat=chat, actor=identity['actorID'], deadline=deadline)
                    if again != sources or not await row_preference_current(after) or not await session_preference_current(after) or not await browser_preference_current(after) or time.monotonic() >= deadline:
                        return validate_managed_method_result(method, unavailable)
                    return validate_managed_method_result(method, {'ok': True, 'operation': operation,
                        'result': {'state': 'ready', 'sources': sources}})
            before = await current()
            if before is None: return validate_managed_method_result(method, unavailable)
            _, _, account, chat, _, _, _, _ = before
            page = self._managed_history_callbacks.exact_output(request, account=account, chat=chat,
                                                                actor=identity['actorID'])
            after = await current()
            if after != before or page is None: return validate_managed_method_result(method, unavailable)
            if self._managed_history_callbacks.exact_output(request, account=account, chat=chat,
                                                            actor=identity['actorID']) != page:
                return validate_managed_method_result(method, unavailable)
            return validate_managed_method_result(method, {'ok': True, 'operation': 'exact_output',
                                                            'result': {'state': 'ready', 'page': page}})
        except (ValueError, KeyError, TypeError, sqlite3.Error, OSError, TimeoutError):
            return validate_managed_method_result(method, unavailable)

    async def _managed_history_tombstone(self, session_id, owner, chat_id, item):
        """Fence the admitted source through Native retirement and archive CAS."""
        from src.openclank.chat_source_bridge import guarded_source_mutation
        from src.openclank.conversation_archive import get_conversation_archive
        from src.openclank.skill_draft_retirement import retire_then_tombstone
        from core.models import get_session_manager_instance

        manager = get_session_manager_instance()
        state = getattr(manager, 'lifecycle_context', None) if manager else None
        def current():
            try:
                return (get_session_manager_instance() is manager and getattr(manager, 'lifecycle_context', None) is state
                        and self._managed_history_binding(session_id) == (owner, chat_id))
            except Exception:
                return False
        if not current():
            raise ValueError('managed history binding changed')
        actor = str(item.get('actorID') or 'main')
        message = str(item['messageID'])
        part = str(item['partID'])
        revision = item.get('revision')
        archive = get_conversation_archive()
        def pre_fence():
            if not current():
                raise ValueError('managed history binding changed')
        if state is None:
            raise ValueError('managed history lifecycle unavailable')
        async with guarded_source_mutation(state=state, owner=owner, chat_id=chat_id,
                message_ids=(message,), pre_fence=pre_fence) as capability:
            if capability is None:
                if not current():
                    raise ValueError('managed history binding changed')
                if revision is None:
                    previous = archive.get_part(owner=owner, chat_id=chat_id, actor_id=actor,
                        message_id=message, part_id=part)
                    if not previous.get('ok'):
                        return {'duplicate': True}
                    revision = previous['part']['revision']
                return archive.tombstone_part(owner=owner, chat_id=chat_id, actor_id=actor,
                    message_id=message, part_id=part, revision=int(revision), reason=item.get('tombstoneReason'))
            return await retire_then_tombstone(capability=capability, archive=archive, owner=owner,
                chat_id=chat_id, actor_id=actor, message_id=message, part_id=part,
                revision=revision, reason=item.get('tombstoneReason'), current=current)

    def _managed_history_binding(self, mimo_session_id: str) -> tuple[str, str]:
        """Return the current owner/chat only after durable admission checks."""
        context = self._session_context.get(mimo_session_id) or {}
        owner = str(context.get("owner") or self._owner or "").strip()
        chat_id = str(context.get("odysseus_session_id") or "").strip()
        if not owner or not chat_id:
            raise ValueError("managed history session is not bound")
        entry = self._durable_session_map.lookup(chat_id)
        if not entry or entry.get("current") != mimo_session_id:
            raise ValueError("managed history session is stale")
        from src.openclank.transcript_projection import get_managed_binding

        binding = get_managed_binding(chat_id, owner=owner)
        if (
            binding.get("owner") != owner
            or binding.get("stableChatID") != chat_id
            or binding.get("engineSessionID") != mimo_session_id
        ):
            raise ValueError("managed history binding mismatch")
        return owner, chat_id

    async def _handle_goal_completed(self, params: dict) -> dict:
        from datetime import datetime, UTC
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        from src.openclank.files_service_client import history_binding_for
        from src.openclank.achievement_producers import record_journal_activity
        method = "_openclank/session/v1/goal/completed"
        receipt = validate_managed_method_request(method, params)
        session_id = receipt["sessionID"]
        owner, chat_id = self._managed_history_binding(session_id)
        context = self._session_context[session_id]
        binding = history_binding_for(owner, "agent", context)
        account_id = str((binding or {}).get("account_id") or "")
        if not account_id:
            raise ValueError("Goal receipt has no authenticated account binding")
        # Owner/account/session come from admission, never caller facts. Only
        # journal IDs and trusted evidence references cross this private seam.
        replayed = await asyncio.to_thread(record_journal_activity, account_id, "goal.verified.completed",
                    receipt["journalID"], {"goalId": receipt["goalID"], "transition": "verified_completed",
                    "evidenceSatisfied": True, "evidenceRefs": receipt["evidenceRefs"], "modelClaimOnly": False},
                    occurred_at=datetime.fromtimestamp(receipt["verifiedAt"] / 1000, UTC).isoformat(),
                    workspace_id=str(context.get("workspace_id") or binding.get("workspace_id") or "default"))
        return validate_managed_method_result(method, {"accepted": True, "replayed": replayed})

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
            return await self._managed_child_binding_read(method, mimo_session_id)
        chat_id = str(context.get("odysseus_session_id") or "").strip()
        if context.get("private_authority"):
            validator = getattr(self, "_private_ghost_is_current", None)
            if (not callable(validator) or mimo_session_id not in self._turns or not context.get("root_operation_id")
                or not await asyncio.to_thread(validator, chat_id, self._owner, self._managed_provider_generation)):
                raise ValueError("private session authority is unavailable")
            return validate_managed_method_result(method, {
                "authority": context["private_authority"], "engineSessionID": mimo_session_id,
                "stableChatID": chat_id, "owner": self._owner, "canonicalCwd": context["physical_cwd"],
                "authorityWorkspaceID": context.get("authority_workspace_id") or "",
                "memoryWorkspaceID": context["memory_workspace"], "copalWorkspace": context["copal_workspace"],
                "memoryEnabled": False, "engineAliases": [],
            })
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
        required_strings = ("physicalCwd", "memoryWorkspaceID", "copalWorkspace")
        if any(not isinstance(binding.get(key), str) or not binding[key].strip() for key in required_strings):
            raise ValueError("managed binding is incomplete")
        # Explicit empty means the chat has no selected Files authority.
        # It is distinct from an omitted/malformed managed binding field.
        authority_workspace = binding.get("authorityWorkspaceID")
        if not isinstance(authority_workspace, str) or authority_workspace != authority_workspace.strip():
            raise ValueError("managed binding authority workspace is invalid")
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

    async def _managed_child_binding_read(self, method: str, child_session_id: str) -> dict:
        """Project current root Files authority to an already-started native child.

        This is deliberately read-only and never inserts child sessions into
        the durable host session map. A child receives the root's current
        owner/chat/workspace binding only after its own exact actor execution
        was admitted and native parentID/ActorRegistry lineage still matches.
        """
        from src.openclank.managed_protocol import validate_managed_method_result

        active = [
            item for item in self._managed_actor_turns.values()
            if item.get("owner") == self._owner and item.get("sessionID") == child_session_id
        ]
        if not active:
            raise ValueError("unmapped engine session has no active managed actor execution")
        roots = {item.get("rootSessionID") for item in active if item.get("rootSessionID")}
        root_ids = {item.get("rootOperationID") for item in active}
        if len(roots) != 1 or len(root_ids) != 1:
            raise ValueError("child session has conflicting active root authority")
        root_session_id = next(iter(roots))
        root_binding = await self._handle_session_binding_read({"sessionID": root_session_id})
        tree = await self._read_managed_actor_tree(root_session_id)
        if child_session_id not in tree["sessions"]:
            raise ValueError("child session is no longer in the current native parent tree")
        for entry in active:
            actor = self._find_actor_in_tree(tree, child_session_id, entry.get("actorID", ""))
            if actor is None or actor.get("status") != "running" or self._actor_revision(actor) != entry.get("executionRevision"):
                raise ValueError("child ActorRegistry execution changed before binding read")
        result = dict(root_binding)
        result["engineSessionID"] = child_session_id
        result["engineAliases"] = []
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
            async with self._durable_session_map.async_chat_admission_lock(chat_id, blocking=False) as guard:
                if guard is None:
                    return {"outcome": "rejected", "transitionID": transition_id, "committed": False, "code": "binding_unavailable"}
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
                entry = await self._durable_session_map.async_lookup(chat_id)
                if not entry or entry.get("current") != mimo_session_id or entry.get("revision") != previous_binding.get("mappingRevision"):
                    context["managed_binding"] = previous_binding
                    return {"outcome": "rejected", "transitionID": transition_id, "committed": False, "code": "stale_engine_session"}
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
                        defer_compensation_fence=True,
                    )
                except Exception as exc:
                    # Roll back live dictionaries before any awaited fence so a
                    # newer callback cannot be overwritten by a stale snapshot.
                    context.clear()
                    context.update(previous_context)
                    if had_state:
                        state.clear()
                        state.update(previous_state)
                    else:
                        self._session_state.pop(mimo_session_id, None)
                    if isinstance(exc, _BindingCompensationNeedsFence):
                        await self._complete_compensation_fence(exc, guard=guard)
                        exc = exc.original
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

    def _assert_host_directory_execution_active(self, chat_id: str, owner: str, *, allow_absent: bool = False) -> None:
        if not chat_id:
            return
        from src.openclank.transcript_projection import assert_directory_execution_active
        try:
            assert_directory_execution_active(chat_id, owner)
        except KeyError:
            if not allow_absent:
                raise

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
        provisional_session_id: Optional[str] = None,
    ) -> str:
        """Create a new mimo session and return the ses_… id directly.

        Also stores the available models from mimo's handshake so the bridge
        can match thesius model names to mimo model IDs later.
        When ``provisional_session_id`` is omitted the bridge reserves one
        first so create-and-publish always uses a known identity.
        """
        self._assert_host_directory_execution_active(odysseus_session, owner if owner is not None else self._owner, allow_absent=True)
        requested_workspace = str(cwd or "").strip()
        target_cwd = requested_workspace or self._cwd
        mcp_servers = ([
            lifetools_mcp_descriptor(
                python_kernel_enabled=self._python_kernel_available(),
                owner=owner if owner is not None else self._owner,
                authority_account_id=_lifetools_launch_account(owner if owner is not None else self._owner),
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
        # Reserve-without-create, then create-and-publish under the reserved
        # identity. Interruptions before SDK create delete nothing and name no
        # orphan; after create, recovery names this exact ses_… id.
        if provisional_session_id is None:
            reserve = await self._client.reserve_session()
            provisional_session_id = reserve["sessionID"]
        self._assert_host_directory_execution_active(odysseus_session, owner if owner is not None else self._owner, allow_absent=True)
        result = await self._client.new_session(
            target_cwd,
            mcp_servers=mcp_servers,
            provisional_session_id=provisional_session_id,
        )
        session_id = result["sessionId"]
        if session_id != provisional_session_id:
            raise RuntimeError(
                "engine create-and-publish did not keep the reserved provisional session id"
            )
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
        owner: Optional[str] = None,
        guard=None,
    ) -> None:
        """Destructively discard a privately created engine session.

        The engine requires the canonical physical cwd to resolve its durable
        store. Prefer the exact persisted candidate binding, falling back only
        to a caller-supplied canonical candidate cwd for the pre-binding
        failure window.
        """
        candidate_cwd = None
        binding_owner = owner if owner is not None else self._owner
        if chat_id:
            try:
                from src.openclank.transcript_projection import get_managed_binding

                binding = get_managed_binding(chat_id, owner=binding_owner)
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
                await self._fence_admission_async(chat_id, "candidate discard cwd unavailable", guard=guard)
            raise RuntimeError("candidate discard requires an authoritative canonical cwd")
        await self._retire_provider_root(session_id)
        self._pending_session_results.pop(session_id, None)
        self._session_models.pop(session_id, None)
        self._session_state.pop(session_id, None)
        self._receipt_origin_registry.pop(session_id, None)
        self._session_context.pop(session_id, None)
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
                await self._fence_admission_async(chat_id, "candidate engine discard", guard=guard)
            raise RuntimeError("candidate engine discard was not confirmed")

    def _fence_admission(self, chat_id: str, reason: str, *, guard=None) -> None:
        """Persist fail-closed authority when cross-store repair is unknown."""
        try:
            self._durable_session_map.fence(chat_id, guard=guard)
            self._session_map = self._durable_session_map.flat_current()
        except Exception as exc:
            logger.exception("unable to persist admission fence for %s (%s)", chat_id, reason)
            raise RuntimeError("managed admission fence could not be persisted") from exc

    async def _recover_orphan_binding(self, odysseus_session: str) -> bool:
        """Repair one SQLite ↔ session-map crash-seam candidate.

        Projection-first staging can leave a complete candidate binding whose
        mapping revision is exactly one past the authoritative map row.  That
        pair is an explicit recoverable fence: discard the unexposed candidate
        engine and rewrite the binding to the map winner.  Candidate-only
        caches are cleaned exactly with the discarded engine.
        """
        self._assert_host_directory_execution_active(odysseus_session, self._owner)
        from src.openclank.transcript_projection import get_managed_binding, save_managed_binding

        async with self._durable_session_map.async_chat_admission_lock(odysseus_session) as guard:
            target_entry = await self._durable_session_map.async_lookup(odysseus_session)
            target = target_entry.get("current") if target_entry else None
            if not target:
                return False
            orphan_binding = get_managed_binding(odysseus_session, owner=self._owner or None)
            current_mapping_revision = int(target_entry.get("revision") or 0)
            orphan_engine = orphan_binding.get("engineSessionID") if isinstance(orphan_binding, dict) else None
            orphan_map_revision = orphan_binding.get("mapRevision") if isinstance(orphan_binding, dict) else None
            orphan_mapping_revision = orphan_binding.get("mappingRevision") if isinstance(orphan_binding, dict) else None
            if not (
                isinstance(orphan_engine, str)
                and orphan_engine
                and orphan_engine != target
                and target in (orphan_binding.get("engineAliases") or [])
                and isinstance(orphan_map_revision, int)
                and isinstance(orphan_mapping_revision, int)
                and orphan_mapping_revision == current_mapping_revision + 1
            ):
                return False
            await self._discard_unbound_engine_session(
                orphan_engine,
                chat_id=odysseus_session,
                cwd=orphan_binding.get("physicalCwd"),
                owner=self._owner,
                guard=guard,
            )
            recovered = dict(orphan_binding)
            recovered["engineSessionID"] = target
            recovered["engineAliases"] = [
                value for value in (orphan_binding.get("engineAliases") or [])
                if value not in {target, orphan_engine}
            ]
            recovered["mapRevision"] = await self._durable_session_map.async_map_revision()
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
                await self._fence_admission_async(odysseus_session, "orphan binding recovery", guard=guard)
                raise RuntimeError("orphan managed binding recovery was not confirmed") from exc
            return True

    async def recover_admission_orphans(self) -> list[str]:
        """Ordinary-restart recovery for the SQLite ↔ session-map crash seam.

        Runs without a later remap: every mapped chat with a recoverable
        candidate fence is repaired before new admission work proceeds.
        """
        recovered: list[str] = []
        for chat_id in list(await self._durable_session_map.async_flat_current()):
            try:
                if await self._recover_orphan_binding(chat_id):
                    recovered.append(chat_id)
            except Exception:
                logger.exception("managed admission recovery failed for %s", chat_id)
                raise
        self._session_map = await self._durable_session_map.async_flat_current()
        return recovered

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

    def _compensate_staged_binding_if_current(
        self,
        chat_id: str,
        candidate_id: str,
        owner: str,
        previous_binding: Optional[dict],
    ) -> bool:
        """Restore a staged candidate only when it still owns the binding.

        A loser may have failed before its projection write because another
        process already published a winner.  In that case the current binding
        is evidence of the winner, not a failed candidate to compensate.
        """
        from src.openclank.transcript_projection import get_managed_binding

        current = get_managed_binding(chat_id, owner=owner)
        if not current or current.get("engineSessionID") != candidate_id:
            return False
        self._compensate_staged_binding(chat_id, candidate_id, owner, previous_binding)
        return True

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
        expected_current: Optional[str] | object = _EXPECTED_UNSET,
        expected_mapping_revision: Optional[int] = None,
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
        if expected_current is not _EXPECTED_UNSET and old_engine != expected_current:
            raise ValueError("managed candidate map current changed during admission")
        if expected_mapping_revision is not None and mapping_revision != int(expected_mapping_revision):
            raise ValueError("managed candidate mapping revision changed during admission")
        prior = dict(previous_binding) if isinstance(previous_binding, dict) else {}
        if prior:
            if prior.get("owner") != owner or prior.get("stableChatID") != chat_id:
                raise RuntimeError("managed binding identity mismatch")
            prior_mapping_revision = int(prior.get("mappingRevision") or 0)
            if prior_mapping_revision != mapping_revision:
                # A crash after projection-first staging leaves an orphaned
                # candidate while the old map row remains authoritative.  The
                # caller must destructively reconcile that candidate while
                # holding the cross-process admission lock; never overwrite
                # its binding here.
                if old_engine and prior.get("engineSessionID") != old_engine and old_engine in (prior.get("engineAliases") or []) and prior_mapping_revision == mapping_revision + 1:
                    raise RuntimeError("orphan candidate requires admission reconciliation")
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
        if owner is not None and owner != self._owner:
            raise ValueError("managed session owner does not match this bridge")
        effective_owner = owner if owner is not None else self._owner
        self._assert_host_directory_execution_active(odysseus_session, effective_owner)
        # Pending native history intents must not be reconstructed from the
        # preserved full transcript after a restart or generation replacement.
        # Explicit reconciliation reads the original native owner directly.
        try:
            from src.openclank.transcript_projection import get_mimo_state
            from src.openclank.session_operations import visibility
            visibility(get_mimo_state(odysseus_session, owner=effective_owner), require_settled=True)
        except KeyError:
            pass  # bounded auxiliary sessions have no canonical host row
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
        self._assert_host_directory_execution_active(odysseus_session, owner if owner is not None else self._owner)
        target_entry = await self._durable_session_map.async_lookup(odysseus_session)
        target = target_entry.get("current") if target_entry else None
        if target is None:
            effective_owner = owner if owner is not None else self._owner
            previous_binding = None
            expected_candidate_current: Optional[str] | object = None
            expected_candidate_mapping_revision = 0
            async with self._durable_session_map.async_chat_admission_lock(odysseus_session) as guard:
                if await self._durable_session_map.async_lookup(odysseus_session) is not None:
                    raise RuntimeError("managed session mapping appeared during fresh admission")
                expected_candidate_current = None
                expected_candidate_mapping_revision = 0
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
                            owner=effective_owner,
                            guard=guard,
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
                        await self._fence_admission_async(odysseus_session, "orphan managed binding", guard=guard)
                        raise RuntimeError("orphan managed binding could not be reconciled") from exc
                    previous_binding = None
            # Reserve-without-create, then create-and-publish under that exact
            # identity (see open_session). Candidate binding is staged after
            # create and before map publish so concurrent fresh admissions can
            # still race cleanly on CAS; interruptions before SDK create delete
            # nothing and name no orphan.
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
                async with self._durable_session_map.async_chat_admission_lock(odysseus_session) as guard:
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
                        expected_current=expected_candidate_current,
                        expected_mapping_revision=expected_candidate_mapping_revision,
                    )
                    await self._bind_engine_session(
                        odysseus_session,
                        new_id,
                        expected_current=None,
                        expected_mapping_revision=0,
                        guard=guard,
                    )
                self._publish_admitted_session(new_id)
                await self._bind_canonical_session_async(
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
                        await self._discard_unbound_engine_session(new_id, chat_id=odysseus_session, cwd=cwd or self._cwd, owner=effective_owner)
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
                    async with self._durable_session_map.async_chat_admission_lock(odysseus_session) as guard:
                        winner = (await self._durable_session_map.async_lookup(odysseus_session) or {}).get("current")
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
                                await self._fence_admission_async(odysseus_session, "candidate binding compensation", guard=guard)
                                preserve_candidate = True
                                raise
                            try:
                                current_entry = await self._durable_session_map.async_lookup(odysseus_session) or {}
                                await self._durable_session_map.async_forget(
                                    odysseus_session,
                                    expected_map_revision=None,
                                    expected_current=new_id,
                                    expected_mapping_revision=int(current_entry.get("revision") or 0),
                                    guard=guard,
                                )
                            except Exception:
                                await self._fence_admission_async(odysseus_session, "candidate map rollback", guard=guard)
                                preserve_candidate = True
                                raise
                            self._session_map = await self._durable_session_map.async_flat_current()
                        else:
                            self._compensate_staged_binding_if_current(
                                odysseus_session,
                                new_id,
                                effective_owner,
                                previous_binding,
                            )
                finally:
                    if not preserve_candidate and not candidate_discarded:
                        await self._discard_unbound_engine_session(new_id, chat_id=odysseus_session, cwd=cwd or self._cwd, owner=effective_owner)
                raise
            return new_id
        from src.openclank.transcript_projection import get_managed_binding
        async with self._durable_session_map.async_chat_admission_lock(odysseus_session) as guard:
            target_entry = self._durable_session_map.lookup(odysseus_session)
            target = target_entry.get("current") if target_entry else None
            if not target:
                raise RuntimeError("managed session mapping disappeared during admission")
        if await self._recover_orphan_binding(odysseus_session):
            async with self._durable_session_map.async_chat_admission_lock(odysseus_session) as guard:
                target_entry = self._durable_session_map.lookup(odysseus_session)
                target = target_entry.get("current") if target_entry else None
                if not target:
                    raise RuntimeError("managed session mapping disappeared during admission")
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
        expected_candidate_current = target
        expected_candidate_mapping_revision = int(target_entry.get("revision") or 0)
        # A mapped chat is host-owned state. A caller's cwd can seed only a new
        # chat; it must not revert a persisted engine workspace on reconnect.
        resume_cwd = str(restored_workspace or cwd or self._cwd).strip()
        mcp_servers = [
            lifetools_mcp_descriptor(
                python_kernel_enabled=self._python_kernel_available(),
                owner=persisted_owner,
                authority_account_id=_lifetools_launch_account(persisted_owner),
                session_id=odysseus_session,
                workspace=resume_cwd,
                authority_workspace_id=persisted_authority,
                memory_workspace_id=persisted_memory,
                memory_enabled=bool(with_memory),
                browser_control_enabled=True,
                browser_mutations_enabled=True,
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
            # OPENCLANK_HOME, or its store was wiped). The full chat history
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
                async with self._durable_session_map.async_chat_admission_lock(odysseus_session) as guard:
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
                        expected_current=expected_candidate_current,
                        expected_mapping_revision=expected_candidate_mapping_revision,
                    )
                    await self._bind_engine_session(
                        odysseus_session,
                        new_id,
                        expected_current=target,
                        expected_mapping_revision=int((target_entry or {}).get("revision") or 0),
                        guard=guard,
                    )
                self._publish_admitted_session(new_id)
                await self._bind_canonical_session_async(
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
                        await self._discard_unbound_engine_session(new_id, chat_id=odysseus_session, cwd=resume_cwd, owner=owner if owner is not None else self._owner)
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
                    async with self._durable_session_map.async_chat_admission_lock(odysseus_session) as guard:
                        winner = (await self._durable_session_map.async_lookup(odysseus_session) or {}).get("current")
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
                                await self._fence_admission_async(odysseus_session, "candidate binding compensation", guard=guard)
                                preserve_candidate = True
                                raise
                            try:
                                current_entry = await self._durable_session_map.async_lookup(odysseus_session) or {}
                                await self._durable_session_map.async_bind(
                                    odysseus_session,
                                    target,
                                    expected_map_revision=None,
                                    expected_current=new_id,
                                    expected_mapping_revision=int(current_entry.get("revision") or 0),
                                    guard=guard,
                                )
                                self._align_binding_to_map(
                                    odysseus_session,
                                    target,
                                    owner if owner is not None else self._owner,
                                )
                            except Exception:
                                await self._fence_admission_async(odysseus_session, "candidate map rollback or binding alignment", guard=guard)
                                preserve_candidate = True
                                raise
                            self._session_map = await self._durable_session_map.async_flat_current()
                        else:
                            self._compensate_staged_binding_if_current(
                                odysseus_session,
                                new_id,
                                owner if owner is not None else self._owner,
                                previous_binding,
                            )
                finally:
                    if not preserve_candidate and not candidate_discarded:
                        await self._discard_unbound_engine_session(new_id, chat_id=odysseus_session, cwd=resume_cwd, owner=owner if owner is not None else self._owner)
                raise
            return new_id

        # Refresh available models from mimo (handles reconnect after restart)
        models = result.get("models", {})
        available = models.get("availableModels", [])
        if available:
            self._session_models[target] = available
            self.available_models = available

        self._capture_handshake(target, result)
        await self._bind_canonical_session_async(
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
        self._assert_host_directory_execution_active(odysseus_session, self._owner)
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
                python_kernel_enabled=self._python_kernel_available(),
                owner=persisted_binding["owner"],
                authority_account_id=_lifetools_launch_account(persisted_binding["owner"]),
                session_id=odysseus_session,
                workspace=workspace,
                authority_workspace_id=str(persisted_binding["authorityWorkspaceID"]),
                memory_workspace_id=str(persisted_binding["memoryWorkspaceID"]),
                # Conversational memory enablement is a per-call carrier flag.
                # The managed binding entry itself stays installed either way.
                memory_enabled=bool(with_memory),
                browser_control_enabled=True,
                browser_mutations_enabled=True,
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
        # Map and owner-qualified projection are one logical authority pair.
        # Serialize the whole transition with the stable parent-level guard so
        # a concurrent candidate cannot stage or publish between our map CAS
        # and binding/projection cleanup.
        if not self._provider_root_sync_guard.acquire(blocking=False):
            raise RuntimeError('provider root lifecycle is busy')
        try:
            self._refuse_sync_provider_root_forget(self._session_map.get(odysseus_session, odysseus_session))
            with self._durable_session_map.chat_admission_lock(odysseus_session) as guard:
                self._forget_session_locked(odysseus_session, guard=guard)
        finally:
            self._provider_root_sync_guard.release()

    async def forget_session_async(self, odysseus_session: str, *, _chat_guard=None) -> None:
        """Forget authority without blocking the event loop on flock."""
        native = self._session_map.get(odysseus_session, odysseus_session)
        await self._retire_provider_root(native)
        if not self._provider_root_sync_guard.acquire(blocking=False):
            raise RuntimeError('provider root lifecycle is busy')
        try:
            if self._session_map.get(odysseus_session, odysseus_session) != native or self._turns.get(native) is not None:
                raise RuntimeError('provider root changed during async retirement')
            if _chat_guard is not None:
                self._durable_session_map.validate_chat_guard(odysseus_session, _chat_guard)
                await self._forget_session_locked_async(odysseus_session, guard=_chat_guard)
                return
            async with self._durable_session_map.async_chat_admission_lock(odysseus_session) as guard:
                await self._forget_session_locked_async(odysseus_session, guard=guard)
        finally:
            self._provider_root_sync_guard.release()

    async def _fence_admission_async(self, chat_id: str, reason: str, *, guard=None) -> None:
        """Persist fail-closed authority off the event loop."""
        try:
            await self._durable_session_map.async_fence(chat_id, guard=guard)
            self._session_map = await self._durable_session_map.async_flat_current()
        except Exception as exc:
            logger.exception("unable to persist admission fence for %s (%s)", chat_id, reason)
            raise RuntimeError("managed admission fence could not be persisted") from exc

    async def _forget_session_locked_async(self, odysseus_session: str, *, guard) -> None:
        """Map-first idempotent forget with every map op off the event loop.

        ``forget_session_async`` must not take ``fcntl.flock`` on the loop:
        one long map lock would freeze unrelated chats. All OwnerSessionMap
        calls therefore route through ``async_*`` / ``async_call``.
        """
        expected_engine = self._session_map.get(odysseus_session)
        if expected_engine is None:
            expected_engine = odysseus_session
        entry = await self._durable_session_map.async_lookup(odysseus_session)
        if entry is None:
            from src.openclank.transcript_projection import (
                delete_managed_binding,
                delete_projection,
                get_managed_binding,
            )

            try:
                orphan_binding = await asyncio.to_thread(
                    get_managed_binding, odysseus_session, owner=self._owner
                )
            except KeyError:
                return
            except Exception as exc:
                await self._fence_admission_async(
                    odysseus_session, "managed binding read during orphan forget", guard=guard
                )
                raise RuntimeError(
                    "managed binding authority could not be read during orphan forget"
                ) from exc
            if not orphan_binding:
                try:
                    # A previous map-first attempt may already have removed
                    # the binding and crashed before projection cleanup.  The
                    # projection is independently owner-qualified, so retry
                    # it even when the canonical binding is now empty.
                    await asyncio.to_thread(
                        delete_projection, odysseus_session, owner=self._owner
                    )
                except KeyError:
                    pass
                except Exception as exc:
                    raise RuntimeError(
                        "orphan managed projection cleanup was not confirmed"
                    ) from exc
                return
            try:
                await asyncio.to_thread(
                    delete_managed_binding,
                    odysseus_session,
                    owner=self._owner,
                    expected_workspace_revision=orphan_binding.get("workspaceRevision"),
                    expected_engine_session_id=orphan_binding.get("engineSessionID"),
                    expected_map_revision=orphan_binding.get("mapRevision"),
                    expected_mapping_revision=orphan_binding.get("mappingRevision"),
                )
                await asyncio.to_thread(
                    delete_projection, odysseus_session, owner=self._owner
                )
            except KeyError:
                return
            except Exception as exc:
                raise RuntimeError(
                    "orphan managed authority cleanup was not confirmed"
                ) from exc
            return
        if entry.get("current") != expected_engine:
            self._session_map = await self._durable_session_map.async_flat_current()
            return
        mimo_session = expected_engine
        map_revision, mapping_revision = await self._durable_session_map.async_revisions(
            odysseus_session
        )
        from src.openclank.transcript_projection import (
            delete_managed_binding,
            delete_projection,
            get_managed_binding,
        )

        try:
            previous_binding = await asyncio.to_thread(
                get_managed_binding, odysseus_session, owner=self._owner
            )
        except KeyError:
            previous_binding = None
        except Exception as exc:
            await self._fence_admission_async(
                odysseus_session, "managed binding read during forget", guard=guard
            )
            raise RuntimeError(
                "managed binding authority could not be read during forget"
            ) from exc
        try:
            await self._durable_session_map.async_forget(
                odysseus_session,
                expected_map_revision=None,
                expected_current=mimo_session,
                expected_mapping_revision=mapping_revision,
                guard=guard,
            )
        except Exception:
            raise
        if previous_binding:
            try:
                await asyncio.to_thread(
                    delete_managed_binding,
                    odysseus_session,
                    owner=self._owner,
                    expected_workspace_revision=previous_binding.get("workspaceRevision"),
                    expected_engine_session_id=mimo_session,
                    expected_map_revision=previous_binding.get("mapRevision"),
                    expected_mapping_revision=previous_binding.get("mappingRevision"),
                )
            except Exception as exc:
                # The map-first prefix is intentionally fail-closed.  The
                # owner-qualified binding remains available for idempotent
                # restart cleanup; the map is already absent, so do not add a
                # quarantine marker that would block that retry path.
                raise RuntimeError("managed binding deletion was not confirmed") from exc
        self._session_map = await self._durable_session_map.async_flat_current()
        self._session_models.pop(mimo_session, None)
        self._session_state.pop(mimo_session, None)
        self._receipt_origin_registry.pop(mimo_session, None)
        self._session_context.pop(mimo_session, None)
        self._queues.pop(mimo_session, None)
        try:
            await asyncio.to_thread(delete_projection, odysseus_session, owner=self._owner)
        except KeyError:
            pass
        except Exception as exc:
            raise RuntimeError("managed projection deletion was not confirmed") from exc

    def _forget_session_locked(self, odysseus_session: str, *, guard) -> None:
        # The local map identifies the engine this bridge owns.  A stale bridge
        # must verify that exact candidate against durable authority; it must
        # never adopt the fresh winner merely because it is the latest row.
        expected_engine = self._session_map.get(odysseus_session)
        if expected_engine is None:
            expected_engine = odysseus_session
        self._refuse_sync_provider_root_forget(expected_engine)
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
                self._fence_admission(odysseus_session, "managed binding read during orphan forget", guard=guard)
                raise RuntimeError("managed binding authority could not be read during orphan forget") from exc
            if not orphan_binding:
                try:
                    # A previous map-first attempt may already have removed
                    # the binding and crashed before projection cleanup.  The
                    # projection is independently owner-qualified, so retry
                    # it even when the canonical binding is now empty.
                    delete_projection(odysseus_session, owner=self._owner)
                except KeyError:
                    pass
                except Exception as exc:
                    raise RuntimeError("orphan managed projection cleanup was not confirmed") from exc
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
        )

        try:
            previous_binding = get_managed_binding(odysseus_session, owner=self._owner)
        except KeyError:
            previous_binding = None
        except Exception as exc:
            self._fence_admission(odysseus_session, "managed binding read during forget", guard=guard)
            raise RuntimeError("managed binding authority could not be read during forget") from exc
        try:
            self._durable_session_map.forget(
                odysseus_session,
                expected_map_revision=None,
                expected_current=mimo_session,
                expected_mapping_revision=mapping_revision,
                guard=guard,
            )
        except Exception:
            raise
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
                # The map-first prefix is intentionally fail-closed.  The
                # owner-qualified binding remains available for idempotent
                # restart cleanup; the map is already absent, so do not add a
                # quarantine marker that would block that retry path.
                raise RuntimeError("managed binding deletion was not confirmed") from exc
        self._session_map = self._durable_session_map.flat_current()
        self._session_models.pop(mimo_session, None)
        self._session_state.pop(mimo_session, None)
        self._receipt_origin_registry.pop(mimo_session, None)
        self._session_context.pop(mimo_session, None)
        self._turns.pop(mimo_session, None)
        self._queues.pop(mimo_session, None)
        try:
            delete_projection(odysseus_session, owner=self._owner)
        except KeyError:
            pass
        except Exception as exc:
            raise RuntimeError("managed projection deletion was not confirmed") from exc

    async def _maybe_inject_digest(
        self,
        messages: list,
        *,
        owner: Optional[str],
        incognito: bool,
        memory_read_allowed: bool = True,
        _factory_no_digest=None,
    ) -> list:
        """Render host-owned pins for this turn, keeping endorsed guidance at system tier."""
        if incognito or not memory_read_allowed or self._memory_provider is None:
            if callable(_factory_no_digest):
                try: _factory_no_digest()
                except Exception: pass
            return messages, ""
        if not hasattr(self._memory_provider, "digest"):
            if callable(_factory_no_digest):
                try: _factory_no_digest()
                except Exception: pass
            return messages, ""
        try:
            from src.memory_digest import DIGEST_FETCH_TIMEOUT_SECONDS

            digest = await asyncio.wait_for(
                self._memory_provider.digest(owner=owner or self._owner),
                timeout=DIGEST_FETCH_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("bridge memory digest unavailable, block dropped this turn: %s", exc)
            return messages, ""
        trusted_block, card = _split_memory_digest(digest, owner or self._owner)
        if not card:
            if not trusted_block and callable(_factory_no_digest):
                try: _factory_no_digest()
                except Exception: pass
            return messages, trusted_block
        from src.prompt_security import untrusted_context_message

        out = list(messages)
        insert_at = next(
            (index for index in range(len(out) - 1, -1, -1) if out[index].get("role") == "user"),
            len(out),
        )
        out.insert(insert_at, untrusted_context_message("memory bank index", card))
        return out, trusted_block

    async def _handle_memery_manage(self, params):
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        from src.openclank.managed_memery import manage

        method = "_openclank/session/v1/memory/manage"
        request = validate_managed_method_request(method, params)
        return validate_managed_method_result(method, await manage(self, request))

    async def _handle_memery_search(self, params):
        from core.models import get_session_manager_instance
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result

        method = "_openclank/session/v1/memory/search"
        request = validate_managed_method_request(method, params)
        state = get_session_manager_instance().lifecycle_context
        from src.openclank.memery_preferences import owner_preferences
        async with asyncio.timeout(10):
            if not (await asyncio.to_thread(owner_preferences, self._owner))["memery_agent_search_enabled"]:
                raise PermissionError("Agent Memery search is disabled")
            result = await state.managed_memery_search(self, request)
            if not (await asyncio.to_thread(owner_preferences, self._owner))["memery_agent_search_enabled"]:
                raise PermissionError("Agent Memery search is disabled")
        return validate_managed_method_result(method, result)

    async def _handle_code_context_read(self, params):
        """Existing managed channel, actual final request payload; no cache."""
        from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result
        method = "_openclank/session/v1/code-context/read"
        request = validate_managed_method_request(method, params)
        turn = request["turn"]
        if turn["sessionID"] != request["sessionID"]:
            raise ValueError("code context turn does not match session")
        key = (request["sessionID"], request["bindingRevision"], request["requestID"], *self._managed_turn_key(turn))
        jobs = self.__dict__.setdefault("_code_context_requests", {})
        result = dict(bindingRevision=request["bindingRevision"], requestID=request["requestID"], requestFingerprint=request["requestFingerprint"], card=None, reason="unavailable")
        if request.get("action") == "cancel":
            binding = await self._handle_session_binding_read({"sessionID": request["sessionID"]})
            if binding["workspaceRevision"] != request["bindingRevision"]:
                return validate_managed_method_result(method, result)
            entry = jobs.get(key)
            if entry and entry[1] == request["requestFingerprint"]:
                entry[0].cancel()
            result["reason"] = "cancelled"
            return validate_managed_method_result(method, result)
        if key in jobs or len(jobs) >= 16 or sum(k[0] == key[0] for k in jobs) >= 4:
            result["reason"] = "busy"
            return validate_managed_method_result(method, result)
        # Register synchronously before the FIRST binding/source await. ACP
        # dispatch creates callbacks in wire order, so immediate cancel cannot
        # overtake registration and be forgotten. No eviction of active tasks.
        task = asyncio.current_task()
        jobs[key] = (task, request["requestFingerprint"])
        try:
            return await self._run_code_context_read(request)
        except asyncio.CancelledError:
            result["reason"] = "cancelled"
            return validate_managed_method_result(method, result)
        finally:
            if jobs.get(key, (None,))[0] is task:
                jobs.pop(key, None)

    async def _cancel_code_context_requests(self, session_id):
        tasks = [entry[0] for key, entry in self.__dict__.get("_code_context_requests", {}).items() if key[0] == session_id and entry[0] is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _discard_private_session(self, chat_id: str, mimo_session: str) -> None:
        await self._retire_provider_root(mimo_session)
        self._receipt_origin_registry.pop(mimo_session, None)
        self._session_context.pop(mimo_session, None)
        self._session_state.pop(mimo_session, None)
        self._session_models.pop(mimo_session, None)
        if self._delete_session_callback:
            try:
                await self._delete_session_callback(chat_id, mimo_session_id=mimo_session)
            except Exception:
                logger.warning("private agent session cleanup failed")

    async def _run_code_context_read(self, request):
        from src.openclank.managed_protocol import validate_managed_method_result
        method = "_openclank/session/v1/code-context/read"
        turn = request["turn"]
        result = dict(bindingRevision=request["bindingRevision"], requestID=request["requestID"], requestFingerprint=request["requestFingerprint"], card=None, reason="unavailable")
        admission = {**turn, "toolName": "recall_memory", "kind": "memory_read"}
        if not (await self._handle_managed_tool_admit(admission)).get("allowed"):
            return validate_managed_method_result(method, result)
        binding = await self._handle_session_binding_read({"sessionID": request["sessionID"]})
        if binding.get("authority"):
            return validate_managed_method_result(method, result)
        context = self._session_context.get(request["sessionID"], {})
        if (binding["workspaceRevision"] != request["bindingRevision"] or not binding["memoryEnabled"] or not binding["authorityWorkspaceID"] or context.get("incognito") or context.get("auxiliary") or context.get("memory_read_allowed") is not True or self._memory_provider is None):
            return validate_managed_method_result(method, result)
        try:
            from core.models import get_session_manager_instance
            from src.constants import AUTH_FILE
            from src.tool_execution import _read_only_auth_snapshot
            from routes.prefs_routes import _load_for_user
            from src.openclank.file_policy import FilePolicyRepository
            from src.openclank.code_context import append_code_context
            messages = request["retainedMessages"]
            if len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode()) > 1024 * 1024:
                return validate_managed_method_result(method, result)
            manager=get_session_manager_instance()
            session=manager.get_session(binding["stableChatID"]) if manager is not None else None
            if session is None or session.owner != self._owner or session.workspace_id != binding["authorityWorkspaceID"] or getattr(session, "archived", False):
                return validate_managed_method_result(method, result)
            out = await append_code_context(messages=messages,message=request["query"],session=session,provider=self._memory_provider,
                repository=FilePolicyRepository(),auth_manager=_read_only_auth_snapshot(os.environ.get("OPEN_CLANK_AUTHORITY_AUTH_PATH") or AUTH_FILE),
                prefs=_load_for_user(self._owner),owner=self._owner,max_bytes=request["availableByteTokens"],max_tokens=request["availableByteTokens"],prefs_reader=lambda: _load_for_user(self._owner))
            current = await self._handle_session_binding_read({"sessionID": request["sessionID"]})
            current_context = self._session_context.get(request["sessionID"], {})
            if current != binding or manager.get_session(binding["stableChatID"]) is not session or session.owner != self._owner or session.workspace_id != binding["authorityWorkspaceID"] or getattr(session, "archived", False) or getattr(session, "incognito", False) or current_context.get("incognito") or current_context.get("auxiliary") or current_context.get("memory_read_allowed") is not True:
                return validate_managed_method_result(method, result)
            if not (await self._handle_managed_tool_admit(admission)).get("allowed"):
                return validate_managed_method_result(method, result)
            if len(out) == len(messages) + 1:
                at = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"), len(messages))
                card = out[at]
                if card.get("role") == "user" and isinstance(card.get("content"), str) and len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode()) <= request["availableByteTokens"]:
                    result.update(card=card["content"], reason=None)
        except Exception:
            pass
        return validate_managed_method_result(method, result)

    async def run_turn(
        self,
        odysseus_session: str,
        messages: list,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
        owner: Optional[str] = None,
        turn_envelope: Optional[dict] = None,
        execution_proof_callback=None,
        _direct_attachment=None,
        _gcf_lease=None,
    ) -> AsyncGenerator[str, None]:
        """Run a prompt turn via ACP, yielding the canonical Agent SSE contract.

        cwd: per-chat workspace override (else the bridge's global default).
        """
        self._assert_host_directory_execution_active(odysseus_session, owner if owner is not None else self._owner, allow_absent=True)
        browser_policy_scope = None
        prior_mimo_session = self._session_map.get(odysseus_session)
        prior_context = self._session_context.get(prior_mimo_session or "", {})
        mapped_workspace = self.mapped_session_workspace(odysseus_session)
        effective_cwd = str(
            (prior_context.get("workspace") or prior_context.get("cwd") or mapped_workspace)
            if prior_mimo_session is not None
            else (cwd or mapped_workspace)
        ).strip()
        from src.openclank.mimo_supervisor import AgentWorkerLease
        if type(_gcf_lease) is AgentWorkerLease:
            if (_gcf_lease.owner != owner or _gcf_lease.worker._bridge is not self or _gcf_lease._released):
                raise ValueError('original managed directory admission unavailable')
            # Canonicalize only the actual chosen cwd; this is cooperative
            # admission, not a claim about arbitrary tool paths.
            _gcf_lease.bind_directory(os.path.realpath(effective_cwd,strict=True))
        else:
            from core.models import get_session_manager_instance
            manager = get_session_manager_instance()
            lifecycle = getattr(manager,'lifecycle_context',None) if manager else None
            supervisor = getattr(lifecycle,'mimo_supervisor',None) if lifecycle else None
            pool = supervisor.owner_lifecycle_pool() if supervisor else None
            if pool is not None:
                pool._assert_host_directory_admission(None)
        # Keep this opaque owner-issued object out of JSON, logs and model input.
        envelope_source = dict(turn_envelope or {})
        factory_persona = envelope_source.pop('_private_factory_persona', None)
        style_turn = envelope_source.pop('_private_style_turn', None)
        original_credential_current = envelope_source.pop('_private_original_credential_current', None)
        connector_guard = envelope_source.pop('_private_connector_guard', None)
        private_media_source = envelope_source.pop('_private_media_source', None)
        private_media_views = envelope_source.pop('_private_media_views', None)
        envelope = json.loads(json.dumps(envelope_source, default=str))
        input_owner_receipt = envelope.pop("_host_current_input_owner", None)
        if owner:
            try:
                from src.openclank.browser_control_service import configured_browser_control_service
                browser_service = configured_browser_control_service()
                if browser_service is not None:
                    browser_service.retire_chat_turn_policies(
                        owner=str(owner).strip().lower(), host_session_id=odysseus_session,
                    )
            except Exception as exc:
                logger.info("stale Browser turn policy retirement unavailable: %s", type(exc).__name__)
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
        if not incognito and not auxiliary and prior_mimo_session:
            await self._retire_provider_root(prior_mimo_session)
        memory_read_allowed = bool(envelope.get("memory_read_allowed", True))
        personal_context_allowed = (
            memory_read_allowed and not incognito and not auxiliary
            and envelope.pop("_private_personal_context_allowed", False) is True
        )
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
            self._update_provider_root_context(mimo_session, {
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
                **({"private_authority": {"kind": "private", "generation": secrets.token_hex(32)}} if incognito and not auxiliary and callable(getattr(self, "_private_ghost_is_current", None)) else {}),
                "auxiliary": auxiliary,
                "is_admin": bool(envelope.get("is_admin")),
                "interaction_policy": envelope.get("interaction_policy", "interactive"),
            }, replace=True)
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
            await self._retire_provider_root(mimo_session)
            async with self._managed_execution_lock:
                existing_root = self._turns.get(mimo_session)
                if existing_root is not None and existing_root.foreground_attachment is not None:
                    raise ValueError('managed provider root changed before context replacement')
                existing_context = self._session_context.get(mimo_session, {})
                self._update_provider_root_context(mimo_session, {
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
            if incognito or auxiliary:
                await self._discard_private_session(odysseus_session, mimo_session)
            yield f'event: error\ndata: {json.dumps({"code": "ROOT_OPERATION_MISMATCH", "error": "The persisted turn operation identity did not match the execution envelope", "status": 409, "retryable": False})}\n\n'
            yield "data: [DONE]\n\n"
            return
        turn_id = metadata_root or supplied_root or _turn_source_id(messages)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}", turn_id):
            if incognito or auxiliary:
                await self._discard_private_session(odysseus_session, mimo_session)
            yield f'event: error\ndata: {json.dumps({"code": "INVALID_ROOT_OPERATION", "error": "The turn operation identity is invalid", "status": 409, "retryable": False})}\n\n'
            yield "data: [DONE]\n\n"
            return
        envelope["root_operation_id"] = turn_id
        envelope["root_turn_id"] = turn_id
        # This is a server-derived caller class. Ordinary managed agent turns
        # are chat; future trusted producer adapters may provide code/research
        # explicitly through the host envelope, never through model options.
        trusted_caller = str(envelope.get("trusted_caller") or "chat").strip().lower()
        if trusted_caller not in {"chat", "code", "research"}:
            if incognito or auxiliary:
                await self._discard_private_session(odysseus_session, mimo_session)
            yield f'event: error\ndata: {json.dumps({"code": "INVALID_TRUSTED_CALLER", "error": "The managed caller context is invalid", "status": 409, "retryable": False})}\n\n'
            yield "data: [DONE]\n\n"
            return
        # Host-side tool callbacks resolve this nonsecret context rather than
        # inventing an operation identity from the long-lived session ID.
        self._update_provider_root_context(mimo_session, {
            "root_operation_id": turn_id,
            "trusted_caller": trusted_caller,
            "memory_read_allowed": memory_read_allowed and not incognito and not auxiliary,
            "personal_context_allowed": personal_context_allowed,
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
        if not incognito and not auxiliary and control_state is not None:
            control_state["last_root_operation_id"] = turn_id
            self._persist_session_state(mimo_session)
        host_stage_timing = {"version": 1, "clock": "host_monotonic"}
        snapshot_started_ns = time.monotonic_ns()
        try:
            if incognito or auxiliary:
                raise KeyError("private turns have no transcript projection")
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
            host_stage_timing["chat_transcript_snapshot_ms"] = (time.monotonic_ns() - snapshot_started_ns) / 1_000_000
        except (KeyError, ValueError):
            # Bounded auxiliary calls deliberately have no canonical chat row.
            pass
        preparation_started_ns = time.monotonic_ns()
        state = _TurnState(envelope.get("max_tool_calls") or 0)
        state.root_operation_id = turn_id
        state.connector_ingress = connector_guard is not None
        state.forward_pair_eligible = not incognito and not auxiliary and not envelope.get('no_memory') and not envelope.get('compare_mode')
        if _direct_attachment is not None:
            state.foreground_attachment = await self._bind_direct_foreground_root(
                _direct_attachment, owner=owner if owner is not None else self._owner,
                host_session=odysseus_session, native_session=mimo_session, root=turn_id)
        try:
            from src.openclank.transcript_projection import verify_current_input_owner_receipt
            state.owner_account_id = verify_current_input_owner_receipt(
                input_owner_receipt, owner=owner if owner is not None else self._owner,
                chat_id=odysseus_session, root_operation_id=turn_id)
            if state.owner_account_id and callable(original_credential_current) and original_credential_current():
                state.original_credential_current = original_credential_current
            from src.openclank.transcript_projection import verify_current_input_theme_status
            state.theme_status_only = verify_current_input_theme_status(
                input_owner_receipt, owner=owner if owner is not None else self._owner,
                chat_id=odysseus_session, root_operation_id=turn_id)
            from src.openclank.transcript_projection import verify_current_input_code_activity
            state.code_activity_workspace_id = verify_current_input_code_activity(
                input_owner_receipt, owner=owner if owner is not None else self._owner,
                chat_id=odysseus_session, root_operation_id=turn_id)
        except Exception:
            state.owner_account_id = None
        candidate = envelope.get("model_request_options")
        if isinstance(candidate, dict):
            state.main_option_candidate = json.loads(json.dumps(candidate))
        state.raw_policy_ceiling = {
            "allowedTools": None if envelope.get("allowed_tools") is None else sorted(set(map(str, envelope.get("allowed_tools") or ()))),
            "disabledTools": sorted(set(map(str, envelope.get("disabled_tools") or ()))),
            "hiddenTools": sorted(set(map(str, envelope.get("hidden_tools") or ()))),
            "blockAllToolCalls": str(envelope.get("interaction_policy") or "").lower() in {"guide", "guide_only"},
            "choiceSnapshot": envelope.get("turn_policy_snapshot"),
            "inputText": _extract_user_text(messages),
        }
        await self._install_provider_root(mimo_session, state)

        desired = control_state.get("desired", {})
        profile = str(desired.get("agent_profile") or "")
        if envelope.get("mode") == "agent" and profile:
            envelope["agent_profile"] = profile
        else:
            envelope.pop("agent_profile", None)
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
            if incognito or auxiliary:
                await self._set_session_config_option(mimo_session, "mode", desired_mode)
            else:
                await self.set_config_option(odysseus_session, "mode", desired_mode, cwd=effective_cwd, owner=owner)
        except (ValueError, RPCError) as exc:
            if incognito or auxiliary:
                await self._retire_provider_root(mimo_session, expected=state)
                await self._discard_private_session(odysseus_session, mimo_session)
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
                option_envelope = envelope.get("model_request_options")
                selected_effort = None
                if isinstance(option_envelope, dict):
                    option_values = option_envelope.get("options")
                    if isinstance(option_values, dict):
                        candidate = option_values.get("reasoning_effort")
                        if isinstance(candidate, str) and candidate not in {"", "default", "inherit"}:
                            selected_effort = candidate
                config_model_id = f"{model_id}/{selected_effort}" if selected_effort else model_id
                if _debug_enabled():
                    logger.info(
                        "[debug] model match: thesius=%s → mimo=%s (available: %d models)",
                        model, model_id, len(self._session_models.get(mimo_session, [])),
                    )
                result = await self._client.set_session_config_option(
                    mimo_session, "model", config_model_id
                )
                selected_runtime_model = model_id
                self._capture_handshake(mimo_session, result)
                self._session_state[mimo_session].setdefault("desired", {})[
                    "model"
                ] = config_model_id
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
                if incognito or auxiliary:
                    await self._retire_provider_root(mimo_session, expected=state)
                    await self._discard_private_session(odysseus_session, mimo_session)
                yield f'data: {json.dumps({"type": "config_error", "config": "model", "requested": model, "available": available, "error": str(e)})}\n\n'
                yield f'event: error\ndata: {json.dumps({"error": f"Open Clank agent rejected model {model!r}; the prompt was not sent", "status": 409})}\n\n'
                yield "data: [DONE]\n\n"
                return

        # Create a queue for this session's notifications
        q: asyncio.Queue = asyncio.Queue()
        self._queues[mimo_session] = q

        # Build the prompt parts from odysseus messages
        factory_digest_absent = [False]
        messages, trusted_block = await self._maybe_inject_digest(
            messages,
            owner=owner,
            incognito=incognito or auxiliary,
            memory_read_allowed=personal_context_allowed,
            _factory_no_digest=lambda: factory_digest_absent.__setitem__(0, True),
        )
        if trusted_block:
            # T6: endorsed guidance rides the persona seam to TRUE system
            # tier (mimo applies envelope.system_prompt as PromptInput.system).
            # The demoted in-message copy is skipped in _build_prompt_parts.
            base_system = str(envelope.get("system_prompt") or "").rstrip()
            envelope["system_prompt"] = (
                f"{base_system}\n\n{trusted_block}" if base_system else trusted_block
            )
        # General entries are saved defaults subordinate to this turn's
        # explicit request. Reuse the host's pinned boundary snapshot; edits
        # while this turn runs affect subsequent operations only.
        from src.general_hex_runtime import turn_hex_snapshot
        from src.general_hex_composition import defaults_instruction
        factory_hex_absent = False
        factory_hex_snapshot = None
        factory_hex_defaults = None
        try:
            hex_snapshot = await asyncio.to_thread(
                turn_hex_snapshot, self._memory_provider, owner_id=owner, boundary_id=turn_id,
                workspace=effective_cwd or None,
                workspace_id=str(envelope.get("authority_workspace_id") or "") or None,
                task_id=odysseus_session, persist=not incognito,
            )
            envelope["general_hex_snapshot_ref"] = {
                "boundary_id": turn_id, "snapshot_hash": hex_snapshot["snapshot_hash"],
                "budget": hex_snapshot["budget"],
            }
            defaults = defaults_instruction(hex_snapshot)
            factory_hex_snapshot, factory_hex_defaults = hex_snapshot, defaults
            factory_hex_absent = (defaults == '' and isinstance(hex_snapshot.get('snapshot_hash'), str)
                and bool(re.fullmatch(r'[0-9a-f]{64}', hex_snapshot['snapshot_hash'])))
            if defaults:
                base_system = str(envelope.get("system_prompt") or "").rstrip()
                envelope["system_prompt"] = f"{base_system}\n\n{defaults}" if base_system else defaults
        except (ValueError, OSError) as exc:
            envelope["general_hex_snapshot_ref"] = {"state": "unavailable", "reason": str(exc)}
            logger.info("General Hex defaults unavailable: %s", exc)
        # Style is original-turn state, independent of optional memory or General Hex.
        style_value = None
        if style_turn is not None:
            from src.default_persona import read_style_turn
            from src.openclank.response_style import style_instruction
            style_value = read_style_turn(style_turn, owner=owner if owner is not None else self._owner,
                account_id=getattr(style_turn, 'account_id', None), chat_id=odysseus_session, root_operation_id=turn_id)
            if style_value is None:
                raise ValueError('original chat style unavailable')
            directive = style_instruction(style_value['mode'])
            if directive:
                base_system = str(envelope.get('system_prompt') or '').rstrip()
                envelope['system_prompt'] = f"{base_system}\n\n{directive}" if base_system else directive
        # Historical source evidence only: any unknown composer or changed
        # current account leaves it absent and never alters the prompt.
        if factory_persona is not None and factory_digest_absent[0] and not trusted_block and state.forward_pair_eligible:
            try:
                from src.default_persona import verify_factory_envelope, bind_factory_general_envelope
                from src.openclank.transcript_projection import verify_current_input_owner_receipt
                account = verify_current_input_owner_receipt(input_owner_receipt, owner=owner if owner is not None else self._owner,
                    chat_id=odysseus_session, root_operation_id=turn_id)
                from core.models import get_session_manager_instance
                manager = get_session_manager_instance()
                lifecycle = getattr(manager, 'lifecycle_context', None) if manager else None
                auth = getattr(lifecycle, 'auth_manager', None) if lifecycle else None
                actual_owner = owner if owner is not None else self._owner
                if (account and account == state.owner_account_id and auth is not None and
                        auth.account_id(actual_owner) == account and auth.username_for_account_id(account) == actual_owner):
                    if style_value is not None and style_value['mode'] != 'normal':
                        from src.default_persona import bind_factory_style_envelope
                        general = factory_hex_snapshot if factory_hex_defaults else None
                        state.factory_persona_source = bind_factory_style_envelope(factory_persona, style_turn, general,
                            str(envelope.get('system_prompt') or ''), owner=actual_owner, account_id=account,
                            chat_id=odysseus_session, root_operation_id=turn_id)
                    elif factory_hex_absent and verify_factory_envelope(factory_persona, owner=actual_owner, account_id=account, chat_id=odysseus_session,
                            root_operation_id=turn_id, system_prompt=str(envelope.get('system_prompt') or '')):
                        state.factory_persona_source = factory_persona
                    elif factory_hex_snapshot is not None and factory_hex_defaults:
                        state.factory_persona_source = bind_factory_general_envelope(factory_persona, factory_hex_snapshot, factory_hex_defaults,
                            str(envelope.get('system_prompt') or ''), owner=actual_owner, account_id=account,
                            chat_id=odysseus_session, root_operation_id=turn_id)
            except Exception:
                pass
        source_layout = {}
        prompt_parts = _build_prompt_parts(
            messages,
            _source_layout=source_layout,
            turn_id=turn_id,
            workspace=effective_cwd,
            authoritative_system=str(envelope.get("system_prompt") or ""),
        )
        # Optional image-source evidence is derived from the exact bounded read,
        # current private root admission and actual ACP bytes, never message metadata.
        if state.forward_pair_eligible and state.owner_account_id and state.original_credential_current and private_media_source:
            try:
                from src.openclank.media_source_evidence import _read_turn
                read_items=_read_turn(private_media_source,owner=owner if owner is not None else self._owner,
                    account=state.owner_account_id,chat=odysseus_session,root=turn_id)
                counts={}; media_budget=0
                for part in prompt_parts:
                    if type(part) is dict and part.get('type')=='image' and type(part.get('data')) is str:
                        media_budget+=len(part['data'])
                        if media_budget>_NATIVE_MEDIA_PAYLOAD_BYTES: raise ValueError('media bound')
                        raw=base64.b64decode(part['data'],validate=True)
                        if len(raw)>_NATIVE_MEDIA_SOURCE_BYTES: raise ValueError('media bound')
                        digest=hashlib.sha256(raw).hexdigest();counts[digest]=counts.get(digest,0)+1
                if read_items:
                    source_counts={}
                    for item in read_items:
                        digest=item['sourceSHA256']
                        if digest:source_counts[digest]=source_counts.get(digest,0)+1
                    items=[{**item,'sourceSHA256':item['sourceSHA256'] if item['sourceSHA256'] and counts.get(item['sourceSHA256'])==1 and source_counts[item['sourceSHA256']]==1 else None,
                            'providerDelivery':'unknown'} for item in read_items]
                    state.media_source_offer={'holder':self._managed_provider_generation,'items':items}
                    if private_media_views:
                        from src.openclank.media_source_evidence import _read_image_views
                        actual_owner=owner if owner is not None else self._owner
                        views=_read_image_views(private_media_views,owner=actual_owner,account=state.owner_account_id,
                            chat=odysseus_session,root=turn_id)
                        originals={item['uploadID']:item['sourceSHA256'] for item in items if item['sourceSHA256']}
                        if views and all(originals.get(receipt['uploadID'])==receipt['originalSHA256'] for receipt,_ in views):
                            state.media_view_offer={'token':private_media_views,'originals':originals,'root':state}
            except Exception:
                state.media_source_offer=None
        # Capture only the exact host-produced resources. Signed provenance is
        # verified again at the live turn callback, never from client metadata.
        generated_boundary=[]
        last_user=next((message for message in reversed(messages) if isinstance(message,dict) and message.get('role')=='user'),None)
        current_resources=(last_user.get('content') if isinstance(last_user,dict) else None)
        current_resources=current_resources if isinstance(current_resources,list) else []
        allowed_generated=[]
        for block in current_resources:
            resource=block.get('resource') if isinstance(block,dict) and block.get('type')=='resource' else None
            if isinstance(resource,dict) and isinstance(resource.get('text'),str) and isinstance(resource.get('_meta'),dict):
                allowed_generated.append((resource['text'],resource['_meta'].get('openclankGeneratedSummary')))
        for part in prompt_parts:
            resource=part.get('resource') if type(part) is dict and part.get('type')=='resource' else None
            meta=resource.get('_meta') if type(resource) is dict else None
            summary_envelope=meta.get('openclankGeneratedSummary') if type(meta) is dict else None
            text=resource.get('text') if type(resource) is dict else None
            if type(summary_envelope) is str and type(text) is str and (text,summary_envelope) in allowed_generated:
                try:
                    serialized=summary_envelope
                    if len(serialized.encode())<=16384 and len(generated_boundary)<10:
                        payload=json.loads(serialized)['payload']
                        generated_boundary.append({'envelope':serialized,'uploadID':payload['upload_id'],
                            'originSHA256':hashlib.sha256(serialized.encode()).hexdigest(),'textSHA256':hashlib.sha256(text.encode()).hexdigest()})
                except (ValueError,TypeError,KeyError): pass
        state.generated_summary_boundary=tuple(generated_boundary)
        # Freeze only bounded, content-free ordered wire evidence. It is a
        # candidate for later host source attestation, never authority itself.
        if state.owner_account_id and len(prompt_parts) <= 128:
            boundary = []
            total = 0
            for position, part in enumerate(prompt_parts):
                if type(part) is not dict or part.get('type') != 'text' or type(part.get('text')) is not str:
                    boundary = []
                    break
                encoded = part['text'].encode('utf-8')
                total += len(encoded)
                if total > 262144:
                    boundary = []
                    break
                boundary.append({'position': position, 'sha256': hashlib.sha256(encoded).hexdigest(),
                                 'synthetic': position < source_layout.get('sourcePosition', 0)})
            if boundary and len(boundary) == len(prompt_parts):
                current = messages[-1] if messages else None
                source_position = source_layout.get('sourcePosition')
                qualified = (source_layout.get('qualified') is True and type(source_position) is int
                    and 0 <= source_position < len(boundary) and isinstance(current, dict)
                    and current.get('role') == 'user' and type(current.get('content')) is str
                    and prompt_parts[source_position]['text'] == f"[turn_source_id={turn_id}]\n" + current['content'])
                state.maintenance_prompt_boundary = {
                    'parts': tuple(boundary), 'coverage': 'candidate' if qualified else 'incomplete',
                    'bodySHA256': hashlib.sha256(current['content'].encode('utf-8')).hexdigest() if qualified else None,
                    **({'sourcePosition':source_position} if qualified else {}),
                }
        prompt_meta = {"odysseus": envelope}
        actor_owner = await self._actor_owner_prompt_metadata(state, owner=owner if owner is not None else self._owner,
            host_session=odysseus_session, native_session=mimo_session, root=turn_id)
        if actor_owner is not None: prompt_meta['openclankActorOwner'] = actor_owner
        if _gcf_lease is not None:
            from src.agent_settings import AdmittedBrowserSnapshotPreference
            from src.openclank.mimo_supervisor import AgentWorkerLease
            browser_preference = getattr(_gcf_lease, '_browser_snapshot_preference', None)
            if (type(_gcf_lease) is AgentWorkerLease and not _gcf_lease._released
                    and getattr(_gcf_lease.worker, '_bridge', None) is self and _gcf_lease.owner == owner
                    and type(browser_preference) is AdmittedBrowserSnapshotPreference and browser_preference.requested is True):
                state.browser_snapshot_preference = (browser_preference, _gcf_lease, self._managed_provider_generation)
        if _gcf_lease is not None:
            from src.agent_settings import AdmittedGcfPreference
            from src.openclank.mimo_supervisor import AgentWorkerLease
            preference = getattr(_gcf_lease, '_gcf_preference', None)
            if (type(_gcf_lease) is AgentWorkerLease and not _gcf_lease._released
                    and getattr(_gcf_lease.worker, '_bridge', None) is self
                    and _gcf_lease.owner == owner and type(preference) is AdmittedGcfPreference):
                prompt_meta['openclankGcfAdmission'] = {
                    'version':1,'sessionID':mimo_session,'rootOperationID':turn_id,
                    'generation':_gcf_lease.generation,'settingsRevision':preference.revision,
                    'requested':preference.requested,'memoize':preference.gcf_memoization_requested,
                    'providerID':preference.provider_id,'modelID':preference.model_id,
                }
                if getattr(preference, 'gcf_graph_references_requested', False) is True:
                    prompt_meta['openclankGcfAdmission']['graphReferences'] = True
                if getattr(preference, 'gcf_same_request_rows_requested', False) is True:
                    prompt_meta['openclankGcfAdmission']['sameRequestRows'] = True
                if getattr(preference, 'gcf_session_view_requested', False) is True:
                    prompt_meta['openclankGcfAdmission']['sessionView'] = True
                if (getattr(preference, 'gcf_same_request_rows_requested', False) is True
                        or getattr(preference, 'gcf_graph_references_requested', False) is True
                        or getattr(preference, 'gcf_session_view_requested', False) is True):
                    state.row_view_preference = (preference, _gcf_lease, self._managed_provider_generation)
                if (getattr(preference, 'git_diff_no_color_requested', False) is True
                        and _gcf_lease._pool.worker_for_owner(owner) is _gcf_lease.worker
                        and _gcf_lease.worker.is_alive() and getattr(_gcf_lease.worker, '_proc', None) is not None
                        and getattr(_gcf_lease.worker, '_client', None) is self._client
                        and getattr(_gcf_lease.worker, 'installed_generation', None) == _gcf_lease.generation
                        and getattr(getattr(_gcf_lease.worker, '_managed_callbacks', None), 'holder_id', None) == self._managed_provider_generation
                        and _gcf_lease._pool._owner_lifecycle_epoch(owner) == _gcf_lease._owner_epoch
                        and state.owner_account_id and self._managed_provider_generation and not incognito and not auxiliary):
                    prompt_meta['openclankShellRewriteAdmission'] = {'version':1,
                        'profile':'git-diff-no-color-v1','settingsRevision':preference.revision}
                    state.shell_rewrite_preference = (preference, _gcf_lease, self._managed_provider_generation)
        if _literal_slash(messages): prompt_meta["openclankLiteralSlash"] = True
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
                try:
                    from core.models import get_session_manager_instance
                    adaptive = _adaptive_style_admission(bridge=self, state=state, lease=_gcf_lease,
                        preference=getattr(_gcf_lease, '_gcf_preference', None), style=style_turn,
                        owner=owner, account=state.owner_account_id, chat=odysseus_session,
                        session=mimo_session, root=turn_id, route=route_context, wire=managed_wire,
                        auth=getattr(getattr(get_session_manager_instance(), 'lifecycle_context', None), 'auth_manager', None),
                        incognito=incognito, auxiliary=auxiliary)
                    if adaptive is not None:
                        prompt_meta['openclankAdaptiveStyleAdmission'] = adaptive
                except Exception:
                    pass  # Optional evidence never changes ordinary dispatch.
                try:
                    # Optional T04 identity is private to this exact admitted turn;
                    # the GCF preference never grants the separate JSON profile.
                    if (_gcf_lease is not None and (getattr(preference, 'json_whitespace_requested', False) or getattr(preference, 'terminal_ansi_requested', False)
                                 or getattr(preference, 'html_markdown_requested', False) or getattr(preference, 'structured_search_requested', False)
                                 or getattr(preference, 'repeated_lines_requested', False) or getattr(preference, 'unified_diff_requested', False)
                                 or getattr(preference, 'schema_annotations_requested', False) or getattr(preference, 'toon_requested', False)
                                 or getattr(preference, 'source_outlines_requested', False) or getattr(preference, 'tap13_requested', False)
                                 or getattr(preference, 'gcf_argument_decode_requested', False) or getattr(preference, 'base64_lines_requested', False)
                                 or getattr(preference, 'ndjson_logs_requested', False) or getattr(preference, 'line_cap_requested', False) or getattr(preference, 'json_shape_requested', False) or getattr(preference, 'read_unnumbered_requested', False) or getattr(preference, 'progress_redraw_requested', False) or getattr(preference, 'lockfile_versions_requested', False) or getattr(preference, 'markdown_slim_requested', False) or getattr(preference, 'unittest_success_requested', False) or getattr(preference, 'cargo_artifacts_requested', False) or getattr(preference, 'untracked_status_requested', False))
                            and type(preference) is AdmittedGcfPreference
                            and type(_gcf_lease) is AgentWorkerLease and not _gcf_lease._released
                            and _gcf_lease.owner == owner and getattr(_gcf_lease.worker, '_bridge', None) is self
                            and _gcf_lease._pool.worker_for_owner(owner) is _gcf_lease.worker
                            and _gcf_lease.worker.is_alive() and getattr(_gcf_lease.worker, '_proc', None) is not None
                            and getattr(_gcf_lease.worker, '_client', None) is self._client
                            and getattr(_gcf_lease.worker, 'installed_generation', None) == _gcf_lease.generation
                            and getattr(getattr(_gcf_lease.worker, '_managed_callbacks', None), 'holder_id', None) == self._managed_provider_generation
                            and self._managed_provider_generation
                            and _gcf_lease._pool._owner_lifecycle_epoch(owner) == _gcf_lease._owner_epoch
                            and state.owner_account_id and not incognito and not auxiliary
                            and preference.provider_id in (managed_wire.get('providerID'), managed_wire.get('connectionID'))
                            and preference.model_id == managed_wire.get('modelID')):
                        from core.models import get_session_manager_instance
                        manager = get_session_manager_instance()
                        lifecycle = getattr(manager, 'lifecycle_context', None) if manager else None
                        auth = getattr(lifecycle, 'auth_manager', None)
                        route_key = json.dumps([route_context.provider_id, route_context.adapter_id,
                                                route_context.connection_kind, route_context.billing_lane],
                                               ensure_ascii=False, separators=(',', ':'))
                        fields = (state.owner_account_id, self._managed_provider_generation, route_key, route_context.adapter_id,
                                  route_context.connection_kind, route_context.billing_lane, managed_wire.get('connectionID'),
                                  managed_wire.get('modelRouteID'), managed_wire.get('providerID'), managed_wire.get('modelID'))
                        if (auth and auth.account_id(owner) == state.owner_account_id
                                and auth.username_for_account_id(state.owner_account_id) == owner
                                and all(type(x) is str and 0 < len(x) <= 256 for x in fields)):
                            view_admission = {
                                'version': 1, 'sessionID': mimo_session, 'rootOperationID': turn_id,
                                'hostAccountID': state.owner_account_id, 'generation': _gcf_lease.generation,
                                'holderID': self._managed_provider_generation,
                                'settingsRevision': preference.revision, 'requested': True,
                                'providerID': managed_wire['providerID'], 'modelID': managed_wire['modelID'],
                                'routeMethodKey': route_key, 'connectionID': managed_wire['connectionID'],
                                'modelRouteID': managed_wire['modelRouteID'], 'profile': 'json-whitespace-v1',
                            }
                            if preference.gcf_argument_decode_requested:
                                prompt_meta['openclankGcfArgumentAdmission'] = {**view_admission, 'profile':'gcf-argument-v1'}
                            if preference.json_whitespace_requested:
                                prompt_meta['openclankJsonWhitespaceAdmission'] = view_admission
                            if (preference.terminal_ansi_requested or preference.html_markdown_requested or preference.structured_search_requested
                                    or preference.repeated_lines_requested or preference.unified_diff_requested or preference.schema_annotations_requested or preference.toon_requested
                                    or preference.source_outlines_requested or preference.tap13_requested or preference.base64_lines_requested or preference.ndjson_logs_requested or preference.line_cap_requested or preference.json_shape_requested or preference.read_unnumbered_requested or preference.progress_redraw_requested or preference.lockfile_versions_requested or preference.markdown_slim_requested or getattr(preference, 'unittest_success_requested', False) or getattr(preference, 'cargo_artifacts_requested', False) or getattr(preference, 'untracked_status_requested', False)):
                                prompt_meta['openclankToolViewAdmission'] = {**{k:v for k,v in view_admission.items() if k != 'profile'},
                                    'version':2, 'profiles':{'jsonWhitespace':preference.json_whitespace_requested,
                                        'terminalAnsi':preference.terminal_ansi_requested, 'htmlMarkdown':preference.html_markdown_requested,
                                        'structuredSearch':preference.structured_search_requested, 'repeatedLines':preference.repeated_lines_requested,
                                        'unifiedDiff':preference.unified_diff_requested, 'schemaAnnotations':preference.schema_annotations_requested,
                                        'toon':preference.toon_requested, 'sourceOutlines':preference.source_outlines_requested, 'tap13':preference.tap13_requested, 'base64Lines':preference.base64_lines_requested, 'ndjsonLogs':preference.ndjson_logs_requested}}
                                if preference.line_cap_requested:
                                    prompt_meta['openclankToolViewAdmission']['version'] = 3
                                    prompt_meta['openclankToolViewAdmission']['profiles']['lineCap'] = True
                                    prompt_meta['openclankToolViewAdmission']['lineCapRows'] = preference.line_cap_rows
                                if preference.json_shape_requested:
                                    prompt_meta['openclankToolViewAdmission']['profiles']['jsonShape'] = True
                                if preference.read_unnumbered_requested:
                                    prompt_meta['openclankToolViewAdmission']['profiles']['readUnnumbered'] = True
                                if preference.progress_redraw_requested:
                                    prompt_meta['openclankToolViewAdmission']['profiles']['progressRedraw'] = True
                                if preference.lockfile_versions_requested:
                                    prompt_meta['openclankToolViewAdmission']['profiles']['lockfileVersions'] = True
                                if preference.markdown_slim_requested:
                                    prompt_meta['openclankToolViewAdmission']['profiles']['markdownSlim'] = True
                                if getattr(preference, 'unittest_success_requested', False) is True:
                                    prompt_meta['openclankToolViewAdmission']['profiles']['unittestSuccess'] = True
                                if getattr(preference, 'cargo_artifacts_requested', False) is True:
                                    prompt_meta['openclankToolViewAdmission']['profiles']['cargoArtifacts'] = True
                                if getattr(preference, 'untracked_status_requested', False) is True:
                                    prompt_meta['openclankToolViewAdmission']['profiles']['untrackedStatus'] = True
                except Exception:
                    pass  # Optional profile never changes ordinary dispatch.
            except Exception as exc:
                logger.warning(
                    "managed provider route resolution rejected model %r: %s",
                    selected_runtime_model,
                    exc,
                )
                if incognito or auxiliary:
                    await self._retire_provider_root(mimo_session, expected=state)
                    self._queues.pop(mimo_session, None)
                    await self._discard_private_session(odysseus_session, mimo_session)
                yield f'event: error\ndata: {json.dumps({"code": "MANAGED_PROVIDER_ROUTE_UNAVAILABLE", "error": str(exc), "status": 409, "retryable": False})}\n\n'
                yield "data: [DONE]\n\n"
                return

        logging_outcome = "unknown"
        logging_context = {}
        logging_callbacks = getattr(self, "_logging_callbacks", None)
        if logging_callbacks is not None:
            try:
                logging_context = await asyncio.to_thread(logging_callbacks.register_logging_operation, turn_id,
                    {"operation_type": "chat", "session_id": odysseus_session, "run_id": turn_id,
                     "actor_id": "main", "actor_kind": "foreground", "incognito": incognito or auxiliary,
                     "workspace_id": str(envelope.get("authority_workspace_id") or "") or None,
                     "connection_id": managed_wire.get("connectionID") if self._managed_provider_context is not None else None,
                     "route_id": managed_wire.get("modelRouteID") if self._managed_provider_context is not None else None,
                     "model_id": managed_wire.get("modelID") if self._managed_provider_context is not None else None,
                     "billing_lane": managed_wire.get("billingLane") if self._managed_provider_context is not None else None})
                if logging_context.get("operationID"):
                    managed_wire["operationID"] = logging_context["operationID"]
            except Exception as exc:
                logger.warning("chat logging admission unavailable: %s", type(exc).__name__)

        if not incognito and not auxiliary and owner and authority_workspace_id:
            try:
                from src.openclank.transcript_projection import get_managed_binding
                from src.openclank.browser_control_service import configured_browser_control_service
                binding = get_managed_binding(odysseus_session, owner=str(owner).strip().lower())
                browser_service = configured_browser_control_service()
                if (browser_service is not None and binding.get("owner") == str(owner).strip().lower()
                        and binding.get("stableChatID") == odysseus_session
                        and binding.get("authorityWorkspaceID") == authority_workspace_id
                        and binding.get("transition") is None
                        and type(binding.get("workspaceRevision")) is int):
                    browser_service.publish_turn_policy(
                        owner=str(owner).strip().lower(), host_session_id=odysseus_session,
                        workspace_id=authority_workspace_id,
                        binding_revision=int(binding["workspaceRevision"]), turn_id=turn_id,
                        envelope=envelope,
                    )
                    browser_policy_scope = (str(owner).strip().lower(), odysseus_session,
                                            authority_workspace_id, int(binding["workspaceRevision"]), turn_id)
            except Exception as exc:
                logger.info("Browser turn policy projection unavailable: %s", type(exc).__name__)

        # Issue inside the retained prompt task so refusal follows the same
        # cleanup path as every other prompt failure.
        async def _issue_prompt():
            if connector_guard is not None:
                from src.openclank.connector_turns import ConnectorTurnGuard
                if type(connector_guard) is not ConnectorTurnGuard:
                    raise ValueError('Connector guard unavailable')
                connector_guard.assert_dispatch(bridge=self, lease=_gcf_lease, state=state,
                    native_session=mimo_session, wire=prompt_meta.get('openclankProvider'))
            if type(_gcf_lease) is AgentWorkerLease:
                _gcf_lease.mark_directory_issued()
            return await self._client.prompt(mimo_session, prompt_parts, metadata=prompt_meta)

        host_stage_timing["preparation_ms"] = (time.monotonic_ns() - preparation_started_ns) / 1_000_000
        prompt_completed_ns = [None]
        prompt_task = asyncio.ensure_future(_issue_prompt())
        prompt_task.add_done_callback(lambda _task: prompt_completed_ns.__setitem__(0, time.monotonic_ns()))

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
                if type(_gcf_lease) is AgentWorkerLease:
                    _gcf_lease.mark_directory_settled()
            except Exception as e:
                logger.error("ACP prompt failed: %s", e)
                if type(_gcf_lease) is AgentWorkerLease:
                    await self._settle_failed_directory_lease(lease=_gcf_lease, state=state, owner=self._owner,
                        host_session=odysseus_session, native_session=mimo_session, root=turn_id)
                # A forwarded session.error already put the real provider
                # message on the stream — don't repeat it as a second card.
                if not state.error_streamed:
                    from src.openclank.connector_turns import ConnectorTurnConflict
                    if isinstance(e, ConnectorTurnConflict):
                        error = {"code": "CONNECTOR_AUTHORITY_STALE", "error": "Connector authority changed before prompt issue", "status": 409, "retryable": False}
                    else:
                        error = {"error": str(e), "status": 500}
                    yield f'event: error\ndata: {json.dumps(error)}\n\n'
                # Only a forwarded error with an independently verified final Native
                # witness can bind the saved Host error. Prompt failure itself is
                # not evidence of a Native terminal diagnostic.
                if execution_proof_callback is not None and state.error_streamed:
                    try:
                        if (self._turns.get(mimo_session) is state and state.root_operation_id == turn_id
                                and self._session_context.get(mimo_session, {}).get("root_operation_id") == turn_id):
                            execution_proof_callback({"kind":"managed_capture_privacy", "rootOperationID":turn_id, "captureVeto":state.privacy_capture_veto})
                            if not state.privacy_capture_veto and state.terminal_error_witness is not None:
                                execution_proof_callback({"kind":"managed_terminal_error", "rootOperationID":turn_id, "witness":copy.deepcopy(state.terminal_error_witness)})
                    except Exception:
                        logger.debug("terminal diagnostic proof unavailable", exc_info=True)
                if isinstance(state.foreground_attachment, dict) and state.foreground_attachment.get('provider_policy_selection') is not None:
                    yield f'data: {json.dumps({"type":"metrics", "data":{"model":None, "actual_model":None, "actual_model_state":"unknown"}})}\n\n'
                yield "data: [DONE]\n\n"
                return

            if execution_proof_callback is not None:
                try:
                    if self._turns.get(mimo_session) is state and state.root_operation_id == turn_id and self._session_context.get(mimo_session, {}).get("root_operation_id") == turn_id:
                        execution_proof_callback({"kind": "managed_capture_privacy", "rootOperationID": turn_id, "captureVeto": state.privacy_capture_veto})
                        if state.error_streamed and not state.privacy_capture_veto and state.terminal_error_witness is not None:
                            execution_proof_callback({'kind':'managed_terminal_error','rootOperationID':turn_id,'witness':copy.deepcopy(state.terminal_error_witness)})
                        if (result.get('stopReason') == 'end_turn' and not state.error_streamed
                                and not state.privacy_capture_veto and state.forward_pair_witness is not None):
                            execution_proof_callback({'kind':'managed_forward_pair', 'rootOperationID':turn_id,
                                'witness':copy.deepcopy(state.forward_pair_witness)})
                except Exception:
                    logger.warning("managed capture privacy unavailable")
            logging_outcome = "completed"
            state.stop_reason = result.get("stopReason", "end_turn")
            state.usage = result.get("usage")
            usage_meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
            primary_execution_verified = False
            alternative_attribution = None
            alternate_possible = (isinstance(state.foreground_attachment, dict)
                                  and state.foreground_attachment.get("provider_policy_selection") is not None)
            if (alternate_possible and result.get("stopReason") == "end_turn" and not state.error_streamed
                    and self._turns.get(mimo_session) is state and not state.provider_root_closing
                    and self._session_context.get(mimo_session, {}).get("root_operation_id") == turn_id):
                try:
                    from core.database import SessionLocal
                    from src.openclank.provider_alternative_proof import validate_alternative_execution
                    with SessionLocal() as proof_db:
                        alternative_attribution = validate_alternative_execution(proof_db, usage_meta.get("alternativeExecutionProof"),
                            owner=self._owner, account=state.owner_account_id, chat=odysseus_session, session=mimo_session,
                            root=turn_id, holder=state.provider_root_holder, generation=state.foreground_attachment.get("generation"),
                            nonce=state.provider_root_nonce)
                except Exception:
                    logger.info("alternative execution proof unavailable")
            if (execution_proof_callback is not None and result.get("stopReason") == "end_turn" and not state.error_streamed
                and not incognito and not auxiliary and not envelope.get("no_memory") and not envelope.get("compare_mode")):
                proof = usage_meta.get("mainExecutionProof")
                try:
                    from core.database import SessionLocal
                    from core.provider_models import ProviderOperationBinding
                    if not isinstance(proof, dict) or type(proof.get("version")) is not int or proof.get("version") != 1 or type(proof.get("executionRevision")) is not int or type(proof.get("bindingRevision")) is not int or proof.get("sessionID") != mimo_session or proof.get("actorID") != "main" or proof.get("rootOperationID") != turn_id:
                        raise ValueError("execution proof identity unavailable")
                    if self._turns.get(mimo_session) is not state or state.root_operation_id != turn_id or self._session_context.get(mimo_session, {}).get("root_operation_id") != turn_id:
                        raise ValueError("execution proof root changed")
                    with SessionLocal() as proof_db:
                        binding = proof_db.query(ProviderOperationBinding).filter_by(id=proof.get("bindingID"), owner=self._owner,
                            root_operation_id=turn_id, engine_session_id=mimo_session, actor_id="main", execution_revision=proof.get("executionRevision")).first()
                        if (binding is None or binding.state != "committed" or binding.revision != proof.get("bindingRevision")
                            or any(getattr(binding, attr) != proof.get(wire) for attr, wire in (
                                ("connection_id", "connectionID"), ("provider_id", "providerID"), ("billing_lane", "billingLane"),
                                ("model_id", "modelID"), ("model_route_id", "modelRouteID"), ("grant_id", "grantID")))
                            or (binding.policy_snapshot or {}).get("main_model_options") != proof.get("modelRequestOptions")):
                            raise ValueError("execution binding proof changed")
                    primary_execution_verified = True
                    execution_proof_callback(json.loads(json.dumps(proof)))
                except Exception:
                    logger.info("main execution proof unavailable")
            for field, key in (("normalizationProfile", "normalization_profile"), ("metricCoverage", "metric_coverage"),
                               ("coveredDispatchIDs", "covered_dispatch_ids"), ("lossReasons", "loss_reasons"),
                               ("identityCoverage", "identity_coverage")):
                if field in result or field in usage_meta:
                    state.metrics[key] = result[field] if field in result else usage_meta[field]

            # The result-processing boundary ends immediately before metrics construction.
            if prompt_completed_ns[0] is not None:
                host_stage_timing["result_processing_ms"] = (time.monotonic_ns() - prompt_completed_ns[0]) / 1_000_000
            state.metrics["host_stage_timing"] = host_stage_timing
            # Emit final metrics
            elapsed = time.time() - state.turn_start
            metrics = {
                "response_time": round(elapsed, 2),
                "model": model or "openclank-engine",
                "stop_reason": state.stop_reason,
                "root_turn_id": turn_id,
                "root_operation_id": turn_id,
                "operation_id": logging_context.get("operationID"),
                "identity_coverage": logging_context.get("identityCoverage", "partial"),
                **state.metrics,
            }
            if alternative_attribution is not None:
                metrics.update(model=alternative_attribution.model, actual_model=alternative_attribution.model,
                               actual_model_state="verified_alternative", actual_model_options=dict(alternative_attribution.thinking))
            elif alternate_possible and not primary_execution_verified:
                from src.openclank.provider_alternative_proof import unknown_alternative_attribution
                alternative_attribution = unknown_alternative_attribution()
                metrics.update(model=None, actual_model=None, actual_model_state="unknown")
            if state.usage:
                if "inputTokens" in state.usage:
                    metrics["input_tokens"] = state.usage["inputTokens"]
                if "outputTokens" in state.usage:
                    metrics["output_tokens"] = state.usage["outputTokens"]
                if "totalTokens" in state.usage:
                    metrics["total_tokens"] = state.usage["totalTokens"]
                if "thoughtTokens" in state.usage:
                    metrics["thinking_tokens"] = state.usage["thoughtTokens"]
                if "cachedReadTokens" in state.usage:
                    metrics["cache_read_tokens"] = state.usage["cachedReadTokens"]
                if "cachedWriteTokens" in state.usage:
                    metrics["cache_write_tokens"] = state.usage["cachedWriteTokens"]
                metrics["usage_source"] = "reported"
            else:
                metrics["usage_source"] = "estimated"

            # Ordinary numeric usage is independent of Advanced admission and
            # visible reply persistence. Preserve the actual aggregate coverage,
            # including missing categories and unresolved dispatch overlap.
            if logging_callbacks is not None:
                try:
                    await asyncio.to_thread(logging_callbacks.record_logging_metrics, turn_id, metrics, logging_outcome,
                                            alternative_attribution=alternative_attribution)
                except Exception as exc:
                    logger.warning("chat ordinary usage write unavailable: %s", type(exc).__name__)
                    metrics["loss_reasons"] = list(dict.fromkeys([*(metrics.get("loss_reasons") or []), "ordinary_usage_write_failed"]))

            yield f'data: {json.dumps({"type": "metrics", "data": metrics})}\n\n'

            # Non-end_turn stop reasons get a notice
            if state.stop_reason != "end_turn":
                notice = _stop_reason_notice(state.stop_reason)
                if notice:
                    yield f'data: {json.dumps({"delta": notice})}\n\n'

            yield "data: [DONE]\n\n"

        except (asyncio.CancelledError, GeneratorExit):
            logging_outcome = "cancelled"
            self._close_provider_root_admission(mimo_session, expected=state)
            # Client disconnected — cancel the turn
            try:
                await self._client.cancel(mimo_session)
            except Exception:
                pass
            try:
                from src.openclank.computer_control.linux import take_over_if_active
                await take_over_if_active()      # a cancelled turn must not leave a desktop-control session running
            except Exception:
                pass
            raise
        finally:
            if browser_policy_scope is not None:
                try:
                    from src.openclank.browser_control_service import configured_browser_control_service
                    browser_service = configured_browser_control_service()
                    if browser_service is not None:
                        browser_service.clear_turn_policy(
                            owner=browser_policy_scope[0], host_session_id=browser_policy_scope[1],
                            workspace_id=browser_policy_scope[2], binding_revision=browser_policy_scope[3],
                            turn_id=browser_policy_scope[4],
                        )
                except Exception as exc:
                    logger.info("Browser turn policy retirement unavailable: %s", type(exc).__name__)
            await self.question_handler.reject_session(mimo_session, turn_id=turn_id)
            await self._retire_provider_root(mimo_session, expected=state)
            self._queues.pop(mimo_session, None)
            if logging_callbacks is not None:
                retire_search_turn = getattr(logging_callbacks, "retire_search_turn", None)
                if callable(retire_search_turn):
                    try:
                        retire_search_turn(mimo_session, turn_id)
                    except Exception as exc:
                        logger.warning("managed search turn retirement unavailable: %s", type(exc).__name__)
            await self._cancel_code_context_requests(mimo_session)
            if not prompt_task.done():
                prompt_task.cancel()
                try:
                    await prompt_task
                except (asyncio.CancelledError, Exception):
                    pass
            if logging_callbacks is not None:
                try:
                    await asyncio.to_thread(logging_callbacks.finish_logging_operation, turn_id, logging_outcome)
                except Exception as exc:
                    logger.warning("chat logging finalization unavailable: %s", type(exc).__name__)
            if incognito or auxiliary:
                await self._discard_private_session(odysseus_session, mimo_session)

    def _mutate_canonical_plan(self, mimo_session_id: str, mutate) -> dict:
        from src.openclank.transcript_projection import mutate_plan_state
        context = self._session_context.get(mimo_session_id) or {}
        if context.get("incognito") or context.get("auxiliary"):
            raise RuntimeError("private canonical plan unavailable")
        owner = context.get("owner") or self._owner
        binding = context.get("managed_binding")
        if not owner or not isinstance(binding, dict) or not context.get("odysseus_session_id"):
            raise RuntimeError("canonical plan binding unavailable")
        from core.models import get_session_manager_instance
        turn = self._turns.get(mimo_session_id)
        account = getattr(turn, "owner_account_id", None)
        manager = get_session_manager_instance()
        auth = getattr(getattr(manager, "lifecycle_context", None), "auth_manager", None) if manager else None
        attachment = getattr(turn, "foreground_attachment", None)
        lifecycle = getattr(manager, "lifecycle_context", None) if manager else None
        supervisor = getattr(lifecycle, "mimo_supervisor", None)
        barrier = getattr(lifecycle, "account_request_barrier", None)
        holder = self._managed_provider_generation
        root = getattr(turn, "root_operation_id", None)
        pool = supervisor.owner_lifecycle_pool() if supervisor is not None else None
        worker = pool.worker_for_owner(owner) if pool is not None else None
        owner_state = pool._states.get(pool._key(owner)) if pool is not None else None
        process = getattr(worker, "_proc", None)
        generation = getattr(worker, "installed_generation", None)
        epoch = pool._owner_lifecycle_epoch(owner) if pool is not None else None
        matches = [(key, entry) for key, entry in self._managed_actor_turns.items()
                   if key[0] == mimo_session_id and key[1] == root and key[2] == "main"
                   and isinstance(entry, dict) and entry.get("state") == "active"
                   and entry.get("stableChatID") == context["odysseus_session_id"]]
        selected = matches[0] if len(matches) == 1 else None
        key, entry = selected if selected else (None, None)
        admission = entry.get("ownerAdmission") if entry else None
        def current():
            try:
                common = (account and auth and barrier and pool and worker and owner_state
                    and self._turns.get(mimo_session_id) is turn and not turn.provider_root_closing
                    and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                    and supervisor.owner_lifecycle_pool() is pool and pool.worker_for_owner(owner) is worker
                    and pool._states.get(pool._key(owner)) is owner_state and owner_state.active is worker
                    and owner_state.status == "ready" and owner_state.generation == generation
                    and worker._bridge is self and worker._proc is process and process is not None
                    and worker.is_alive() and worker.installed_generation == generation
                    and pool._owner_lifecycle_epoch(owner) == epoch and self._managed_provider_generation == holder
                    and getattr(getattr(worker, "_managed_callbacks", None), "holder_id", None) == holder
                    and not getattr(self, "_ownerAdmissionHolderStopped", False))
                if not common: return False
                if isinstance(attachment, dict):
                    return bool(attachment.get("account") == account and attachment.get("barrier") is barrier
                        and attachment.get("lease")._pool is pool and attachment.get("admission") is not None
                        and not attachment["admission"].released
                        and self._direct_foreground_current(attachment, owner=owner,
                            host_session=context["odysseus_session_id"], native_session=mimo_session_id,
                            root=root, holder=holder))
                return bool(entry and self._managed_actor_turns.get(key) is entry
                    and self._managed_actor_executions.get(key[:4]) is entry
                    and entry.get("state") == "active" and not entry.get("backgroundStartClosing")
                    and entry.get("owner") == owner and entry.get("rootSessionID") == mimo_session_id
                    and entry.get("stableChatID") == context["odysseus_session_id"]
                    and entry.get("holderID") == holder and entry.get("ownerAdmissionBarrier") is barrier
                    and entry.get("ownerAdmissionAccount") == account and entry.get("ownerAdmission") is admission
                    and admission is not None and not admission.released)
            except (AttributeError, KeyError, TypeError, RuntimeError):
                return False
        if not current():
            raise RuntimeError("original plan account unavailable")
        plan = mutate_plan_state(context["odysseus_session_id"], owner, mutate, expected_binding=binding, current_authority=current)
        self._session_state.setdefault(mimo_session_id, {})["plan_state"] = plan
        return plan

    @staticmethod
    def _combine_plan(previous: dict, payload: dict) -> dict:
        combined = {key: value for key, value in previous.items() if key not in ("digest", "revision", "approved_revision", "approved_digest")}
        combined.update(payload)
        material = json.dumps({key: value for key, value in combined.items() if key != "status"}, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        changed = previous.get("digest") != digest or previous.get("status") == "cleared"
        if changed and combined.get("status") == "approved": combined["status"] = "revised"
        return {**combined, "digest": digest, "revision": int(previous.get("revision") or 0) + changed,
                "approved_revision": None if changed else previous.get("approved_revision"),
                "approved_digest": None if changed else previous.get("approved_digest")}

    def _persist_plan_update(self, mimo_session_id: str, payload: dict) -> dict:
        return self._mutate_canonical_plan(mimo_session_id, lambda previous: self._combine_plan(previous, payload))

    def _persist_plan_diff(self, mimo_session_id: str, path: str, content: str) -> dict | None:
        from src.clanker_paths import canonical_plan_path, ClankerPathError
        def apply(previous):
            binding = previous.get("artifact_relpath")
            if not binding:
                return self._combine_plan(previous, {"plan": content, "path": path})
            try: root = canonical_plan_path(str(binding), allow_legacy=True)
            except ClankerPathError: return previous
            if path == root: return self._combine_plan(previous, {"plan": content, "path": root})
            if not root.endswith(".md") or not path.startswith(root[:-3] + "/"): return previous
            slices = dict(previous.get("slice_digests") or {})
            if path not in slices and len(slices) >= 128:
                return self._combine_plan(previous, {"slice_coverage": "unavailable", "slice_coverage_reason": "observed_slice_limit_exceeded"})
            slices[path] = hashlib.sha256(content.encode("utf-8")).hexdigest()
            return self._combine_plan(previous, {"slice_digests": slices})
        return self._mutate_canonical_plan(mimo_session_id, apply)

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
                    from src.clanker_paths import canonical_plan_path, ClankerPathError
                    canonical = None
                    try:
                        candidate = Path(path)
                        if candidate.is_absolute():
                            workspace = (self._session_context.get(mimo_session_id) or {}).get("workspace")
                            if workspace:
                                candidate = candidate.resolve().relative_to(Path(workspace).resolve())
                            else:
                                raise ValueError("workspace unavailable")
                        canonical = canonical_plan_path(candidate.as_posix(), allow_legacy=False)
                    except (ClankerPathError, ValueError, OSError):
                        pass
                    if canonical or "/plans/" in path.replace("\\", "/"):
                        try:
                            plan = self._persist_plan_diff(mimo_session_id, canonical or path, str(block.get("newText") or ""))
                        except (ValueError, KeyError, RuntimeError):
                            plan = None
                            logger.info("Native plan update unavailable")
                        if plan is not None:
                            sses.append(f'data: {json.dumps({"type": "plan_update", "data": plan})}\n\n')
            raw_output = update.get("rawOutput", {})
            activity = (raw_output.get('metadata') or {}).get('codeActivity') if isinstance(raw_output,dict) and isinstance(raw_output.get('metadata'),dict) else None
            if isinstance(raw_output, dict) and not output_text:
                output_text = str(raw_output.get("output") or raw_output.get("error") or "")
            event_type = "tool_progress" if status in ("pending", "in_progress") else "tool_output"
            clean_update = update
            if isinstance(raw_output,dict) and isinstance(raw_output.get('metadata'),dict) and 'codeActivity' in raw_output['metadata']:
                clean_update = dict(update); clean_raw = dict(raw_output); clean_meta = dict(raw_output['metadata'])
                clean_meta.pop('codeActivity',None); clean_raw['metadata']=clean_meta; clean_update['rawOutput']=clean_raw
            event: dict = {"type": event_type, "tool": title, "id": tool_call_id, "status": status, "output": output_text, "data": _sanitize_event_value(clean_update)}
            if event_type == 'tool_output' and status == 'completed' and isinstance(tool_call_id,str):
                matches=[]
                for identity,entry in self._managed_actor_turns.items():
                    if (identity[0] == mimo_session_id and identity[1] == state.root_operation_id and isinstance(entry,dict)
                        and entry.get('state') == 'active' and entry.get('ownerAccountID') == state.owner_account_id):
                        record=(entry.get('workflowResultEvents') or {}).get(tool_call_id)
                        if isinstance(record,dict): matches.append(record)
                if len(matches)==1:
                    try:
                        if isinstance(raw_output,dict) and json.loads(raw_output.get('output','')) == matches[0]['result']:
                            event['resourceResult']=matches[0]['selector']
                    except (TypeError,ValueError,KeyError): pass
            if event_type == "tool_output":
                # The UI derives ✓/✗ from exit_code (homegrown-loop shape);
                # ACP only reports status, so map it.
                event["exit_code"] = 0 if status == "completed" else 1
                if status == 'completed' and isinstance(activity,dict) and activity.get('toolCallID') == tool_call_id:
                    identity = (activity.get('engineSessionID'),activity.get('rootOperationID'),activity.get('actorID'),
                        activity.get('executionRevision'),activity.get('turnID'))
                    current = self._managed_actor_turns.get(identity) if all(isinstance(value,(str,int)) and not isinstance(value,bool) for value in identity) else None
                    if (activity.get('engineSessionID') == mimo_session_id and activity.get('rootOperationID') == state.root_operation_id
                        and isinstance(current,dict) and current.get('state') == 'active'
                        and current.get('codeActivityEnabled') is True
                        and current.get('codeActivityEvents',{}).get(tool_call_id) == activity):
                        emitted = current.setdefault('codeActivityEmitted',{})
                        if tool_call_id not in emitted:
                            emitted[tool_call_id] = activity['resultSHA256']
                            event['codeActivity'] = _project_code_activity_with_source_digest(activity, raw_output.get('output'))
            sses.append(f'data: {json.dumps(event)}\n\n')

        elif update_type == "usage_update":
            usage = _sanitize_event_value(update)
            state.metrics.update({key: value for key, value in usage.items() if key != "sessionUpdate"})
            state.metrics["usage_source"] = "reported"
            sses.append(f'data: {json.dumps({"type": "usage", "data": usage})}\n\n')

        elif update_type == "plan":
            try:
                plan = self._persist_plan_update(mimo_session_id, {
                    "todos": _sanitize_event_value(update.get("entries") or update.get("plan") or []),
                })
            except (ValueError, KeyError, RuntimeError):
                plan = None
                logger.info("Native plan update unavailable")
            if plan is not None:
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
                from src.openclank.operation_explanation import explain_operation
                explanation = explain_operation(request_id=req.request_id, session_id=req.odysseus_session_id,
                    turn_id=req.turn_id, revision=req.revision, permission_type=req.title, detail=detail)
                if explanation is not None:
                    payload["explanation"] = explanation
                sses.append(f'data: {json.dumps({"type": "permission_request", "data": payload})}\n\n')

        elif update_type == "_question_request":
            req = update.get("request")
            if req is not None:
                sses.append(f'data: {json.dumps({"type": "ask_user", "data": req.payload()})}\n\n')

        else:
            logger.error("unknown ACP session update: %s", update_type)
            sses.append(f'data: {json.dumps({"type": "protocol_error", "data": {"message": "Unknown ACP session update", "update_type": str(update_type)[:128]}})}\n\n')

        return sses


def _project_code_activity_with_source_digest(activity, original_output):
    """Attach a checksum only to the existing, admitted, unchanged full-file read."""
    resource=activity.get('resource')
    if type(resource) is not dict or type(original_output) is not str or len(original_output)>1_000_000:
        return activity
    try:
        encoded=original_output.encode('utf-8')
        if len(encoded)>1_000_000 or hashlib.sha256(encoded).hexdigest()!=activity.get('resultSHA256'):
            return activity
        wrapper=json.loads(original_output)
        if type(wrapper.get('exit_code')) is not int or wrapper['exit_code']!=0:
            return activity
        result=json.loads(wrapper['output'])
        window=result['source_window'];hits=result['hits']
        if (result.get('schema')!='open-clank.code-query/v1' or result.get('operation')!='read' or result.get('error') is not None
            or result.get('receipt')!=resource.get('receipt') or window.get('continuity')!='full_sha256'
            or type(hits) is not list or len(hits)!=1):
            return activity
        hit=hits[0];raw=hit['raw_source'];source=hit['source']
        digest=raw.get('hash')
        if (hit.get('id')!=resource.get('resourceID') or hit.get('evidence_state')!='current'
            or hit.get('current_source_verified') is not True or source.get('source_hash')!=resource.get('sourceHash')
            or raw.get('rel_path')!=window.get('rel_path') or raw.get('hash_algorithm')!='sha256'
            or raw.get('hash_scope')!='file' or raw.get('state')!='observed_file'
            or not isinstance(digest,str) or not re.fullmatch(r'[0-9a-f]{64}',digest)):
            return activity
        return {**activity,'resource':{**resource,'sourceSHA256':digest}}
    except (AttributeError, TypeError, ValueError, KeyError):
        return activity


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


def _data_uri(value: str, *, declared_mime: str = "") -> tuple[str, str] | None:
    if not value.startswith("data:") or "," not in value:
        return None
    header, payload = value[5:].split(",", 1)
    fields = header.split(";")
    mime = fields[0] or "application/octet-stream"
    # ACP native media crosses as an exact byte payload.  Plain/percent data
    # URLs are intentionally rejected so they cannot be mistaken for base64.
    if "base64" not in fields[1:] or not payload:
        return None
    if declared_mime and mime.lower() != declared_mime.lower():
        return None
    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        return None
    if len(raw) > _NATIVE_MEDIA_SOURCE_BYTES or len(payload.encode("ascii")) > _NATIVE_MEDIA_PAYLOAD_BYTES:
        return None
    return mime, payload


def _media_error_part(name: str, reason: str) -> dict:
    return {
        "type": "text",
        "text": f"[Attachment unavailable: {name} ({reason})]",
    }


def _bounded_blob(payload: Any) -> bool:
    if not isinstance(payload, str) or not payload:
        return False
    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        return False
    return len(raw) <= _NATIVE_MEDIA_SOURCE_BYTES and len(payload.encode("ascii")) <= _NATIVE_MEDIA_PAYLOAD_BYTES


def _is_native_media_mime(mime: str) -> bool:
    normalized = str(mime or "").split(";", 1)[0].strip().lower()
    return normalized.startswith(("image/", "audio/", "video/")) or normalized in {
        "application/pdf",
        "application/octet-stream",
    }


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
            name = next(names, "image")
            declared_mime = str((image or {}).get("mediaType") or block.get("mimeType") or "") if isinstance(image, dict) else ""
            data = _data_uri(uri or "", declared_mime=declared_mime)
            if data:
                result.append({
                    "type": "image",
                    "data": data[1],
                    "mimeType": data[0],
                    "uri": f"attachment://{urllib.parse.quote(name)}",
                })
            elif isinstance(uri, str) and uri.startswith("data:"):
                result.append(_media_error_part(name, "malformed or unsupported data URL"))
            elif isinstance(uri, str) and uri.startswith(("http://", "https://")):
                result.append(_media_error_part(name, "remote media bytes are unproven"))
            elif isinstance(uri, str) and _safe_resource_uri(uri, workspace):
                result.append({
                    "type": "image",
                    "uri": uri,
                    "mimeType": block.get("mimeType") or "image/*",
                })
            else:
                result.append(_media_error_part(name, "missing, unsafe, or unsupported image URI"))
            continue
        if kind in ("audio", "input_audio"):
            audio = block.get("audio") or block.get("input_audio") or {}
            uri = audio.get("url", "") if isinstance(audio, dict) else ""
            data = _data_uri(uri)
            if not data and isinstance(audio, dict) and audio.get("data"):
                raw_payload = str(audio["data"])
                try:
                    raw = base64.b64decode(raw_payload, validate=True)
                except (ValueError, binascii.Error):
                    raw = b""
                if raw and len(raw) <= _NATIVE_MEDIA_SOURCE_BYTES and len(raw_payload.encode("ascii")) <= _NATIVE_MEDIA_PAYLOAD_BYTES:
                    data = (f"audio/{audio.get('format') or 'mpeg'}", raw_payload)
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
            elif uri.startswith(("http://", "https://")):
                result.append(_media_error_part(next(names, "audio"), "remote media bytes are unproven"))
            elif uri.startswith("data:") or (isinstance(audio, dict) and audio.get("data")):
                result.append(_media_error_part(next(names, "audio"), "malformed or oversized audio payload"))
            elif isinstance(audio, dict) and audio:
                result.append(_media_error_part(next(names, "audio"), "missing, unsafe, or unsupported audio URI"))
            continue
        if kind == "resource_link":
            uri = str(block.get("uri") or "")
            mime = str(block.get("mimeType") or "application/octet-stream")
            remote_native = uri.startswith(("http://", "https://")) and _is_native_media_mime(mime)
            if remote_native:
                result.append(_media_error_part(str(block.get("name") or "resource"), "remote native media bytes are unproven"))
            elif _safe_resource_uri(uri, workspace):
                result.append({
                    "type": "resource_link",
                    "uri": uri,
                    "name": str(block.get("name") or "resource"),
                    "mimeType": str(block.get("mimeType") or "application/octet-stream"),
                    "size": block.get("size"),
                })
            else:
                result.append(_media_error_part(str(block.get("name") or "resource"), "unsupported or unsafe resource URI"))
            continue
        if kind == "resource" and isinstance(block.get("resource"), dict):
            resource = block["resource"]
            uri = str(resource.get("uri") or "")
            text_value = resource.get("text")
            blob_value = resource.get("blob")
            has_text = "text" in resource
            has_blob = "blob" in resource
            valid_text = has_text and not has_blob and isinstance(text_value, str) and len(text_value.encode("utf-8")) <= _NATIVE_MEDIA_SOURCE_BYTES
            valid_blob = has_blob and not has_text and _bounded_blob(blob_value)
            if _safe_resource_uri(uri, workspace) and (valid_text or valid_blob):
                result.append({"type": "resource", "resource": dict(resource)})
            else:
                result.append(_media_error_part(uri or "resource", "malformed, oversized, or unsafe resource"))
        elif any(key in block for key in ("url", "uri", "data", "image_url", "input_audio")):
            result.append(_media_error_part(str(kind or "attachment"), "unsupported media shape"))
    return result


def _literal_slash(messages: list) -> bool:
    current = next((item for item in reversed(messages) if item.get("role") == "user"), {})
    content = current.get("content")
    if isinstance(content, list):
        content = next((item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"), "")
    return isinstance(content, str) and bool(re.match(r"^/literal(?:\s|$)", content))


def _build_prompt_parts(
    messages: list,
    *,
    turn_id: str = "",
    workspace: str = "",
    authoritative_system: str = "",
    _source_layout: dict | None = None,
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

    source_position = len(parts)
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
        if re.match(r"^/literal(?:\s|$)", current):
            current = re.sub(r"^/literal(?:[ \t])?", "", current, count=1)
        elif current.startswith("/mimo:"):
            current = "/" + current[len("/mimo:"):]
        first_text["text"] = prefix + current
    parts.extend(current_parts)
    if type(_source_layout) is dict:
        # This classification belongs to the converter branches, not input flags.
        _source_layout['sourcePosition'] = source_position
        _source_layout['qualified'] = bool(
            current_index == len(messages)-1 and current_message.get('role') == 'user'
            and type(current_message.get('content')) is str and not metadata.get('attachments')
            and len(current_parts) == 1 and current_parts[0].get('type') == 'text'
            and current_parts[0].get('text') == prefix + current_message['content']
            and all(type(part) is dict and part.get('type') == 'text'
                    and type(part.get('text')) is str and part.get('annotations') == annotations
                    for part in parts[:source_position]))
    return parts


def _maintenance_layout_digest(layout, source_position):
    if type(layout) is not list or not 1 <= len(layout) <= 128 or type(source_position) is not int or not 0 <= source_position < len(layout):
        raise ValueError('maintenance layout unavailable')
    canonical=[]
    for index, part in enumerate(layout):
        if (type(part) is not dict or set(part) != {'position','sha256','synthetic'}
                or type(part['position']) is not int or part['position'] != index
                or type(part['synthetic']) is not bool or part['synthetic'] != (index != source_position)
                or type(part['sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',part['sha256']) is None):
            raise ValueError('maintenance layout unavailable')
        canonical.append({'position':index,'sha256':part['sha256'],'synthetic':part['synthetic']})
    return hashlib.sha256(json.dumps(canonical,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()


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
    # The private persistent wrapper has exactly the existing Python process
    # authority, including explicit allowlist and deny/hidden ceilings.
    python_denied = 'python' in set(map(str,envelope.get('disabled_tools') or ())) or 'python' in set(map(str,envelope.get('hidden_tools') or ()))
    if python_denied or allowed is not None:
        policy['lifetools_*_python_kernel'] = not python_denied and 'python' in set(map(str,allowed or ()))
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
    # Both canonical surfaces share the Native `memory` name. Keep the name
    # reachable when either surface is allowed; the actual operation is checked
    # again by its existing Host owner before any read or write.
    denied_memory = set(map(str, envelope.get("disabled_tools") or ())) | set(map(str, envelope.get("hidden_tools") or ()))
    allowed_memory = None if allowed is None else set(map(str, allowed))
    memory_surfaces = {"manage_memory", "recall_memory"} - denied_memory
    if allowed_memory is not None:
        memory_surfaces &= allowed_memory
    policy["memory"] = bool(memory_surfaces) and "memory" not in denied_memory
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
        "plan_revision", "plan_digest", "plan_account", "questions", "actor_id", "actor_session_id",
        "origin_turn_id", "question_revision", "blocking", "_future",
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
        plan_digest: str = "",
        plan_account: str = "",
        actor_id: str = "",
        actor_session_id: str = "",
        origin_turn_id: str = "",
        question_revision: int = 0,
        blocking: bool = True,
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
        self.plan_digest = plan_digest
        self.plan_account = plan_account
        self.questions = questions
        self.actor_id = actor_id
        self.actor_session_id = actor_session_id
        self.origin_turn_id = origin_turn_id
        self.question_revision = question_revision
        self.blocking = blocking
        self._future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()

    def payload(self) -> dict:
        first = self.questions[0] if self.questions else {}
        return {
            "request_id": self.request_id,
            "session_id": self.odysseus_session_id,
            "turn_id": self.turn_id,
            "revision": self.revision,
            "plan_revision": self.plan_revision,
            "plan_digest": self.plan_digest,
            "native_request_id": self.mimo_request_id,
            "actor_id": self.actor_id,
            "actor_session_id": self.actor_session_id or self.mimo_session_id,
            "origin_turn_id": self.origin_turn_id,
            "question_revision": self.question_revision,
            "blocking": self.blocking,
            "question_ref": f"{self.mimo_request_id}@{self.question_revision}" if self.question_revision else None,
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
        return self._future.result() if self._future.done() and not self._future.cancelled() else {}


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

    async def reject_session(self, mimo_session_id: str, *, turn_id: str | None = None) -> None:
        for req in list(self.pending_requests.values()):
            if req.mimo_session_id != mimo_session_id or (turn_id is not None and req.turn_id != turn_id):
                continue
            req.resolve({"rejected": True})
            if self._on_resolved:
                await self._on_resolved(req)

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
        native_request_id = str(params.get("requestId") or "")
        blocking_value = params.get("blocking", True)
        if not isinstance(blocking_value, bool):
            return {"rejected": True}
        blocking = blocking_value
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
        actor_id = str(params.get("actorID") or "")
        actor_session_id = str(params.get("actorSessionID") or mimo_session)
        origin_turn_id = str(params.get("originTurnID") or "")
        question_revision = params.get("questionRevision", 0)
        questions = self._questions(params.get("questions"))
        if not blocking:
            if (
                not actor_id
                or actor_session_id != mimo_session
                or not origin_turn_id
                or not isinstance(question_revision, int)
                or isinstance(question_revision, bool)
                or question_revision < 1
                or not native_request_id
                or any(question.get("key") == "plan_exit" for question in questions)
            ):
                return {"rejected": True}
        req = PendingQuestion(
            mimo_request_id=native_request_id,
            mimo_session_id=mimo_session,
            odysseus_session_id=odysseus_session,
            owner=owner,
            turn_id=str(projection.get("active_turn_id") or context.get("root_operation_id") or ""),
            revision=int(projection.get("transcript_revision") or 0),
            plan_revision=int(context.get("plan_revision") or 0),
            plan_digest=str(context.get("plan_digest") or ""),
            plan_account=str(context.get("plan_account") or ""),
            questions=questions,
            actor_id=actor_id,
            actor_session_id=actor_session_id,
            origin_turn_id=origin_turn_id,
            question_revision=question_revision if isinstance(question_revision, int) and not isinstance(question_revision, bool) else 0,
            blocking=blocking,
        )
        if not blocking:
            if self._on_request is None:
                return {"accepted": False, "deliveryState": "unavailable"}
            try:
                await self._on_request(req)
                return {"accepted": True, "deliveryState": "pending"}
            except Exception as exc:
                logger.warning("failed to surface optional Open Clank question: %s", exc)
                return {"accepted": False, "deliveryState": "unavailable"}
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
        return self.resolve_result(request_id, owner=owner, session_id=session_id, answers=answers, rejected=rejected) == "accepted"

    def resolve_result(
        self,
        request_id: str,
        *,
        owner: str,
        session_id: str,
        answers: Optional[list[list[str]]] = None,
        rejected: bool = False,
    ) -> str:
        req = self.pending_requests.get(request_id)
        if req is None or req._future.done() or req.owner != owner or req.odysseus_session_id != session_id:
            return "stale"
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
                or (any(q.get("key") == "plan_exit" for q in req.questions) and (not req.plan_digest or not req.plan_account or context.get("plan_digest") != req.plan_digest or context.get("plan_account") != req.plan_account))
            ):
                return "stale"
        if rejected:
            return "accepted" if req.resolve({"rejected": True}) else "stale"
        if not isinstance(answers, list) or len(answers) != len(req.questions):
            return "invalid_answer"
        clean: list[list[str]] = []
        for index, answer in enumerate(answers):
            if not isinstance(answer, list) or not answer:
                return "invalid_answer"
            if any(not isinstance(value, str) or len(value) > 2_048 for value in answer):
                return "invalid_answer"
            values = [value.strip() for value in answer if value.strip()]
            if not values:
                return "invalid_answer"
            question = req.questions[index]
            if not question["multiple"] and len(values) != 1:
                return "invalid_answer"
            allowed = {item["label"] for item in question["options"]}
            if not question["custom"] and any(value not in allowed for value in values):
                return "invalid_answer"
            clean.append(values)
        return "accepted" if req.resolve({"answers": clean}) else "stale"


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
        control = re.fullmatch(r"lifetools_.+_(control|inspect)_desktop", title) is not None
        inspect_tool = control and title.endswith("_inspect_desktop")
        forced_ask = control or title in {"bash_delete", "bash_destructive"}
        control_needs: tuple = ()
        raw_input = tool_call.get("rawInput", {})
        control_digest = ""
        if control:
            # Desktop input: the prompt shows exactly what will happen, and the approval is bound to these exact args.
            from src.openclank.computer_control import protocol as control_protocol
            try:
                control_args = raw_input.get("args") if isinstance(raw_input, dict) else None
                control_call = control_protocol.parse_inspect(control_args) if inspect_tool else control_protocol.parse_call(control_args)
                control_digest = control_protocol.args_digest(control_args)
                control_summary = control_protocol.inspect_summary(control_call) if inspect_tool else control_protocol.approval_summary(control_call)
                raw_input = {"summary": control_summary, "args": control_args}
                # Settings the call needs: reading and element actions need the accessibility opt-in, any acting needs the control opt-in.
                control_needs = (("desktop_accessibility_enabled",) if inspect_tool else
                                 ("desktop_control_enabled", "desktop_accessibility_enabled") if control_call["action"] in control_protocol.ELEMENT_ACTIONS or (control_call["action"] == "hand_over_file" and control_call.get("direction") == "upload" and "expected_revision" in control_call) else ("desktop_control_enabled",))
                params = {**params, "options": ["once", "reject"]}
            except control_protocol.ProtocolError:
                return self._approve("reject")
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
        from src.openclank.operation_approvals import canonical_workspace_id
        authority_workspace_id = canonical_workspace_id(owner, authority_workspace_id)
        incognito = bool(context.get("incognito"))
        is_admin = bool(context.get("is_admin"))

        if control and (not owner or incognito):
            return self._approve("reject")
        if control and inspect_tool and control_call["action"] == "handshake":
            return self._approve("once")      # readiness only: no window content, so no card; the tool answers with its typed reason
        if control and owner:
            from src.settings import get_user_setting
            if not all(get_user_setting(key, owner, False) for key in control_needs):
                return self._approve("reject")      # switched off in Settings: the human is not asked about something that cannot run
            # A call that is already refused (no fresh observation, lease of another owner/session, a file dialog we cannot do) must not show a card:
            # approve it WITHOUT recording a grant, so the tool runs, repeats the same check and answers the model with the typed reason.
            from src.openclank.computer_control.hoststate import STATE as _CONTROL_STATE
            upload = (control_call["action"] == "hand_over_file" and control_call.get("direction") == "upload"
                      and "expected_revision" in control_call and sys.platform.startswith("linux"))
            if (control_call["action"] == "hand_over_file" and not upload) or _CONTROL_STATE.precheck(owner, session_id, None, control_call,
                    {"multi_agent": bool(get_user_setting("desktop_multi_agent_same_app_enabled", owner, False))}):
                return self._approve("once")
        if context.get("interaction_policy") == "fail_on_interaction":
            return self._approve("reject")
        if owner and not is_admin:
            return self._approve("reject")

        # Installation safe-directory hints are not a second consent store.
        # Every reusable approval is checked in the canonical ledger below.
        # ── stored durable grants ──
        if not incognito and not forced_ask:
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
                    return self._approve("once")
            except Exception:
                pass
        # ── owner permission mode (yolo/auto) ──
        # Approve with once semantics — no durable grant, no UI wait. The
        # fail_on_interaction and non-admin rejects above stay authoritative.
        try:
            from src.permission_mode import auto_approves

            if not forced_ask and auto_approves(owner):
                logger.info("auto-approved %s: %s (permission mode)", title, filepath or "*")
                return self._approve("once")
        except Exception:
            pass

        selected_metadata = None
        async def selected_file_metadata():
            # This is metadata only.  The actual bytes are independently opened
            # later under the current actor's Agent Files grant.
            from core.models import get_session_manager_instance
            from src.openclank.files_facade import ProviderContext
            from src.openclank.files_host_provider import _path
            manager = get_session_manager_instance()
            state = getattr(manager, "lifecycle_context", None) if manager else None
            auth = getattr(state, "auth_manager", None)
            repository = getattr(state, "files_policy_repository", None)
            barrier = getattr(state, "account_request_barrier", None)
            factory = getattr(state, "files_facade_state_factory", None)
            if not auth or not repository or not barrier or barrier._fenced(owner) or not callable(factory) or not workspace or not authority_workspace_id:
                raise ValueError("selected file unavailable")
            account = auth.account_id(owner)
            if not account or auth.username_for_account_id(account) != owner:
                raise ValueError("selected file unavailable")
            generation = repository.generation()
            files_context = ProviderContext(account, owner, generation, bool(auth.is_admin(owner)), authority_workspace_id)
            facade = factory(workspace)
            provider, ref = facade._provider_for_ref(files_context, control_call["resource_ref"], capability="download")
            stat_provider, stat_ref = facade._provider_for_ref(files_context, control_call["resource_ref"], capability="stat")
            if provider is not stat_provider or ref != stat_ref or ref.provider != "host":
                raise ValueError("selected file unavailable")
            entry = await provider.stat_inspection(files_context, origin_id=ref.origin_id)
            revision = control_call["expected_revision"]["value"]
            size = int(revision.rsplit(":", 1)[1])
            path = _path(ref.origin_id)
            if (entry.origin_id != ref.origin_id or entry.kind != "file" or type(entry.size) is not int or entry.size != size
                or not isinstance(entry.revision, dict) or entry.revision.get("kind") != "hostObjectIdentity"
                or not isinstance(entry.revision.get("value"), str) or len(path) > 1024
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in path)
                or repository.generation() != generation or auth.account_id(owner) != account or barrier._fenced(owner)
                or auth.username_for_account_id(account) != owner or not facade._generation_current(files_context)):
                raise ValueError("selected file unavailable")
            facade._provider_for_ref(files_context, control_call["resource_ref"], capability="download")
            return (account, generation, path, entry.size, dict(entry.revision), revision)

        if control and control_call["action"] == "hand_over_file" and upload:
            try:
                selected_metadata = await selected_file_metadata()
                selected_path = selected_metadata[2]
                control_summary = (f"Enter the selected file {os.path.basename(selected_path)} from {os.path.dirname(selected_path)} "
                    f"(expected revision {selected_metadata[5]}) into the observed chooser in "
                    f"{control_call['target']['app']} / {control_call['target']['window']}. "
                    "Entry does not prove selection or opening.")
                raw_input = {"summary": control_summary, "args": control_args}
            except Exception:
                return self._approve("reject")

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
            if control:
                # Only a plain allow-once counts, and it is recorded for the Host to consume once.
                if option_id != "once" or incognito:
                    return self._approve("reject")
                if selected_metadata is not None:
                    try:
                        latest = self._context_resolver(session_id) if self._context_resolver else {}
                        latest_projection = get_projection(odysseus_session, owner=owner)
                        if (not isinstance(latest_projection, dict) or latest_projection.get("mimo_session_id") != session_id
                            or latest_projection.get("active_turn_id") != projection.get("active_turn_id")
                            or latest_projection.get("transcript_revision") != projection.get("transcript_revision")
                            or not isinstance(latest, dict) or latest.get("owner") != owner
                            or latest.get("workspace") != workspace or latest.get("authority_workspace_id") != context.get("authority_workspace_id")
                            or await selected_file_metadata() != selected_metadata
                            or _CONTROL_STATE.precheck(owner, session_id, None, control_call,
                                {"multi_agent": bool(get_user_setting("desktop_multi_agent_same_app_enabled", owner, False))})):
                            return self._approve("reject")
                    except Exception:
                        return self._approve("reject")
                control_protocol.REGISTRY.grant(owner, session_id, control_digest)
                return self._approve("once")
            if (incognito or forced_ask) and option_id in {"chat", "workspace", "always"}:
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
                        option_id = "once"
                        logger.info("approval persistence unavailable; using Once")
                    else:
                        logger.info("stored canonical %s grant: (%s, %s)", option_id, title, pattern)
                else:
                    # There is no stable scope to persist against; preserve
                    # the safe Once semantics instead of minting an account-
                    # wide approval under a misleading label.
                    option_id = "once"

            # Keep the managed engine from caching a second approval ledger.
            # Reusable consent is resolved afresh by this bridge on each ask.
            return self._approve("once" if option_id in {"chat", "workspace", "always"} else option_id)
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
