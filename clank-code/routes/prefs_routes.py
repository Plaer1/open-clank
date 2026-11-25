"""User preferences API — per-user key/value store backed by a JSON file."""
import asyncio
import json
import os
import threading
import tempfile
import copy

from typing import Iterable, Optional
from fastapi import APIRouter, HTTPException, Request
from src.auth_helpers import get_current_user
from src.constants import USER_PREFS_FILE

PREFS_FILE = USER_PREFS_FILE

CLANKSHOP_DEFAULT = {'version':1,'mode':'workshop','motion':'system','intensity':'normal',
    'drawn_actor_limit':8,'followed_chat_limit':8,'effects_budget':12,'audio_enabled':False,
    'music_enabled':True,'hits_enabled':True,'master_volume':0.15,'music_volume':0.55,'effects_volume':0.8,
    'background_audio':'silent','pause_in_background':False,'completion_cue':True,'drift_meter':'adaptive','drift_pickup_scale':'major','drift_style':'rally-house',
    'activity_volume':1,'pickup_volume':1,'drift_volume':1,'dodge_volume':1,'tool_volume':1,'alert_volume':1}

def _clankshop_pref(value):
    volume_fields = ('activity_volume','pickup_volume','drift_volume','dodge_volume','tool_volume','alert_volume')
    optional_fields = {'music_volume','effects_volume','drift_meter','drift_pickup_scale','drift_style','pause_in_background',*volume_fields}
    if (type(value) is not dict or not set(CLANKSHOP_DEFAULT).difference(optional_fields).issubset(value)
            or not set(value).issubset(CLANKSHOP_DEFAULT)
            or type(value.get('version')) is not int or value['version'] != 1):
        raise ValueError('Invalid Clankshop preference fields')
    for key, choices in {'mode':{'workshop','drift','blocks','idle'}, 'motion':{'system','reduced','full'},
                         'intensity':{'quiet','normal','busy'}, 'background_audio':{'full','quarter','silent'}}.items():
        if value.get(key) not in choices: raise ValueError('Invalid Clankshop preference')
    for key, bounds in {'drawn_actor_limit':(1,16), 'followed_chat_limit':(1,8), 'effects_budget':(0,32)}.items():
        if type(value.get(key)) is not int or not bounds[0] <= value[key] <= bounds[1]: raise ValueError('Invalid Clankshop preference')
    value = {**CLANKSHOP_DEFAULT, **value}
    for key in ('audio_enabled','music_enabled','hits_enabled','completion_cue','pause_in_background'):
        if type(value[key]) is not bool: raise ValueError('Invalid Clankshop preference')
    for key, choices in {'drift_meter':('adaptive','3/4','4/4','5/4','6/8','7/8','9/8','12/8'),
                         'drift_pickup_scale':('major','pentatonic','minor-pentatonic','natural-minor',
                                               'dorian','phrygian','lydian','mixolydian','harmonic-minor','melodic-minor'),
                         'drift_style':('rally-house','garage','phonk')}.items():
        if value[key] not in choices:
            raise ValueError('Invalid Drift music preference')
    for key in ('master_volume', 'music_volume', 'effects_volume', *volume_fields):
        volume = value[key]
        if type(volume) not in (int, float) or not 0 <= volume <= 1:
            raise ValueError('Invalid Clankshop volume')
    return value


class _PreferenceLock:
    """Reentrant process and file lock shared by every preferences writer."""
    def __init__(self):
        self._thread = threading.RLock()
        self._local = threading.local()

    def try_enter(self):
        """Acquire the same process/file authority without waiting."""
        if not self._thread.acquire(blocking=False):
            return False
        try:
            depth = getattr(self._local, 'depth', 0)
            if depth == 0:
                directory = os.path.dirname(PREFS_FILE) or '.'
                os.makedirs(directory, exist_ok=True)
                fd = os.open(PREFS_FILE + '.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                try:
                    import stat
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise OSError('preference lock is not a regular file')
                    if os.name == 'nt':
                        import msvcrt
                        if os.fstat(fd).st_size == 0: os.write(fd, b'\0')
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BaseException:
                    os.close(fd)
                    raise
                self._local.fd = fd
            self._local.depth = depth + 1
            return True
        except Exception:
            self._thread.release()
            return False
        except BaseException:
            self._thread.release()
            raise

    def __enter__(self):
        self._thread.acquire()
        try:
            depth = getattr(self._local, "depth", 0)
            if depth == 0:
                directory = os.path.dirname(PREFS_FILE) or "."
                os.makedirs(directory, exist_ok=True)
                fd = os.open(PREFS_FILE + ".lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
                try:
                    import stat
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise OSError("preference lock is not a regular file")
                    if os.name == "nt":
                        import msvcrt
                        if os.fstat(fd).st_size == 0: os.write(fd, b"\0")
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(fd)
                    raise
                self._local.fd = fd
            self._local.depth = depth + 1
            return self
        except BaseException:
            self._thread.release()
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            self._local.depth -= 1
            if self._local.depth == 0:
                fd = self._local.fd
                try:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
                    del self._local.fd
        finally:
            self._thread.release()


PREFERENCE_LOCK = _PreferenceLock()
_MEMORY_POLICY_KEY = "memory_policy"
_PROVIDER_METHOD_POLICY_KEY = "provider_method_policy"
_BINDER_CATALOG_KEY = "binder_catalog"
_HEX_EVOLVE_KEY = "workspace_hex_evolve"
_MEMORY_MODES = frozenset({"off", "automatic", "manual"})
_PERMISSION_MODES = frozenset({"manual", "yolo", "auto"})
_TRUST_PREF_MASTER = "memory_trust_auto"
_TRUST_PREF_KINDS = "memory_trust_auto_kinds"
_TRUST_KINDS = frozenset({"instruction", "persona", "fact", "episodic", "fabric", "wiki"})
_MOBILE_CONTROL_SIDE_PREF = "mobile_control_side"
_MOBILE_CONTROL_SIDES = frozenset({"left", "right", "system"})


def _load():
    """Load the raw prefs file (internal use only)."""
    try:
        with open(PREFS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("Invalid preference store")
            return data
    except FileNotFoundError:
        return {}


def _save(prefs):
    os.makedirs(os.path.dirname(PREFS_FILE) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".prefs-", dir=os.path.dirname(PREFS_FILE) or ".")
    os.close(fd)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prefs, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PREFS_FILE)


def _load_for_user(user: Optional[str] = None) -> dict:
    """Load preferences for a specific user."""
    all_prefs = _load()
    if "_users" in all_prefs:
        if user is None:
            # Auth disabled — return first user's prefs for backward compat
            users = all_prefs["_users"]
            return dict(next(iter(users.values()), {}))
        return dict(all_prefs["_users"].get(user, {}))
    if user is not None and all_prefs:
        raise ValueError("Legacy preferences require .clanker/tools/migrations/python/secondary.py prefs")
    return dict(all_prefs)


def _save_for_user_unlocked(user: Optional[str], prefs: dict):
    """Save preferences for a specific user."""
    all_prefs = _load()
    if user is None:
        # Auth disabled. If the store is already multi-user (e.g. auth was
        # turned off on a deployment that previously ran multi-user), writing
        # `prefs` flat would overwrite the whole `_users` map and destroy every
        # other user's preferences. Instead write back into the same (first)
        # slot _load_for_user(None) reads from, preserving the others.
        if "_users" in all_prefs:
            users = all_prefs["_users"]
            first_key = next(iter(users), None)
            if first_key is not None:
                users[first_key] = prefs
                _save(all_prefs)
                return
        _save(prefs)
        return
    if "_users" not in all_prefs:
        if all_prefs:
            raise ValueError("Legacy preferences require .clanker/tools/migrations/python/secondary.py prefs")
        all_prefs = {"_users": {}}
    all_prefs["_users"][user] = prefs
    _save(all_prefs)


def _protected_preferences(current, proposed, *, allow_logging=False, allow_memory_policy=False, allow_provider_method_policy=False, allow_binder_catalog=False, allow_legacy_memory=False, allow_skill_draft_policy=False, allow_document_vector_policy=False):
    from src.openclank.memery_preferences import DEFAULTS
    updated = dict(proposed)
    protected = [_HEX_EVOLVE_KEY, "code_vector_search_consent"]
    if not allow_skill_draft_policy: protected.append('_skill_draft_policy_version')
    if not allow_document_vector_policy: protected.extend(('_document_vector_profile_version','document_vector_profile'))
    if not allow_binder_catalog: protected.append(_BINDER_CATALOG_KEY)
    if not allow_provider_method_policy: protected.append(_PROVIDER_METHOD_POLICY_KEY)
    if not allow_memory_policy: protected.append(_MEMORY_POLICY_KEY)
    if not allow_legacy_memory: protected.extend(('memory_mode', 'auto_memory', 'memory_enabled', 'memory_question_seeds', 'memory_assistant_interpretation', 'memory_policy_importance_enabled', 'memory_policy_connections_enabled', 'memory_policy_consolidation_enabled', 'memory_policy_consolidation_interval_days', 'memory_shadow_ranking_enabled', 'memory_policy_connections_predicate'))
    if not allow_legacy_memory: protected.extend(DEFAULTS)
    if not allow_logging: protected.append('logging_preferences')
    for key in protected:
        if key in current: updated[key] = current[key]
        else: updated.pop(key, None)
    return updated


def _save_for_user(user: Optional[str], prefs: dict, *, allow_logging=False, allow_memory_policy=False, allow_provider_method_policy=False, allow_binder_catalog=False, allow_legacy_memory=False, allow_skill_draft_policy=False, allow_document_vector_policy=False, _policy_token=None):
    with PREFERENCE_LOCK:
        current = _load_for_user(user)
        updated = _protected_preferences(current, prefs, allow_logging=allow_logging,
            allow_memory_policy=allow_memory_policy, allow_provider_method_policy=allow_provider_method_policy,
            allow_binder_catalog=allow_binder_catalog, allow_legacy_memory=allow_legacy_memory,
            allow_skill_draft_policy=allow_skill_draft_policy, allow_document_vector_policy=allow_document_vector_policy)
        from src.openclank.memory_policy_projection import exact_json, policy_coordinator, preference_inputs
        coordinator = policy_coordinator()
        if coordinator is not None and exact_json(preference_inputs(current)) != exact_json(preference_inputs(updated)):
            coordinator.validate(_policy_token, user, current)
        return _save_for_user_unlocked(user, updated)


def _update_for_user(user, patch):
    def prepare(current):
        current.update(patch)
        return current, None
    result, _ = _mutate_user(user, prepare)
    return result


def _mutate_user(user, prepare, *, allow_memory_policy=False, allow_skill_draft_policy=False, allow_document_vector_policy=False, before_save=None):
    """Compute under the prefs lock, fence outside it, then compare and save."""
    from src.openclank.memory_policy_projection import exact_json, policy_coordinator, preference_inputs
    with PREFERENCE_LOCK:
        before = copy.deepcopy(_load_for_user(user))
        updated, result = prepare(copy.deepcopy(before))
        if type(updated) is not dict:
            raise ValueError('preferences unavailable')
        updated = _protected_preferences(before, updated, allow_memory_policy=allow_memory_policy,
                                         allow_legacy_memory=True, allow_skill_draft_policy=allow_skill_draft_policy, allow_document_vector_policy=allow_document_vector_policy)
        if before_save is not None and before_save() is not True:
            raise ValueError('preference authority changed')
        if exact_json(updated) == exact_json(before):
            return before, result
        changes_policy = exact_json(preference_inputs(before)) != exact_json(preference_inputs(updated))
        coordinator = policy_coordinator() if changes_policy else None
        if coordinator is None:
            _save_for_user(user, updated, allow_memory_policy=allow_memory_policy, allow_legacy_memory=True,
                           allow_skill_draft_policy=allow_skill_draft_policy, allow_document_vector_policy=allow_document_vector_policy)
            return _load_for_user(user), result
        combined = coordinator.active_combined_account_token(user, before)
        if combined is not None:
            if before_save is not None and before_save() is not True:
                raise ValueError('preference authority changed')
            _save_for_user(user, updated, allow_memory_policy=allow_memory_policy, allow_legacy_memory=True,
                           allow_skill_draft_policy=allow_skill_draft_policy, allow_document_vector_policy=allow_document_vector_policy,
                           _policy_token=combined)
            return _load_for_user(user), result
    # Callers must not hold a reentrant outer preference lock here.
    if getattr(PREFERENCE_LOCK._local, 'depth', 0):
        raise ValueError('policy transition requires outer preference lock release')
    token = coordinator.begin(user, before)
    try:
        with PREFERENCE_LOCK:
            if exact_json(_load_for_user(user)) != exact_json(before):
                raise ValueError('preferences changed during policy transition')
            if before_save is not None and before_save() is not True:
                raise ValueError('preference authority changed')
            _save_for_user(user, updated, allow_memory_policy=allow_memory_policy,
                           allow_legacy_memory=True, allow_skill_draft_policy=allow_skill_draft_policy, allow_document_vector_policy=allow_document_vector_policy, _policy_token=token)
            after = copy.deepcopy(_load_for_user(user))
        coordinator.finish(token, user, after)
        return after, result
    finally:
        coordinator.close(token)


def workspace_hex_mode(owner: str, account_id: str, project_id: str) -> dict:
    """Read an account-bound mode; an absent entry is normal revision zero."""
    with PREFERENCE_LOCK:
        entries = _load_for_user(owner).get(_HEX_EVOLVE_KEY, {})
        entry = entries.get(account_id, {}).get(project_id) if isinstance(entries, dict) and isinstance(entries.get(account_id, {}), dict) else None
        if entry is None:
            return {"mode": "normal", "revision": 0}
        if not isinstance(entry, dict) or entry.get("mode") not in {"normal", "evolve"} or type(entry.get("revision")) is not int or entry["revision"] < 0:
            raise ValueError("Workspace mode metadata unavailable")
        return {"mode": entry["mode"], "revision": entry["revision"]}


def set_workspace_hex_mode(owner: str, account_id: str, project_id: str, mode: str, expected_revision: int, current) -> dict:
    if mode not in {"normal", "evolve"} or type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("Invalid workspace mode request")
    with PREFERENCE_LOCK:
        prior = workspace_hex_mode(owner, account_id, project_id)
        if prior["revision"] != expected_revision or not current():
            raise ValueError("Workspace mode or credential changed")
        if prior["mode"] == mode:
            return prior
        prefs = _load_for_user(owner)
        entries = copy.deepcopy(prefs.get(_HEX_EVOLVE_KEY, {}))
        if not isinstance(entries, dict) or not isinstance(entries.get(account_id, {}), dict):
            raise ValueError("Workspace mode metadata unavailable")
        previous_operation = workspace_hex_operation(owner, account_id, project_id)
        entries.setdefault(account_id, {})[project_id] = {"mode": mode, "revision": prior["revision"] + 1, **({'operation': previous_operation} if previous_operation is not None else {})}
        prefs[_HEX_EVOLVE_KEY] = entries
        if not current():
            raise ValueError("Credential changed")
        _save_for_user_unlocked(owner, prefs)
        return {"mode": mode, "revision": prior["revision"] + 1}


def workspace_hex_operation(owner: str, account_id: str, project_id: str) -> dict | None:
    with PREFERENCE_LOCK:
        entries = _load_for_user(owner).get(_HEX_EVOLVE_KEY, {})
        project = entries.get(account_id, {}).get(project_id, {}) if isinstance(entries, dict) and isinstance(entries.get(account_id, {}), dict) else {}
        if not isinstance(project, dict): raise ValueError('Workspace mode metadata unavailable')
        operation = project.get('operation')
        if operation is not None and not isinstance(operation, dict): raise ValueError('Workspace operation unavailable')
        return copy.deepcopy(operation)


def set_workspace_hex_operation(owner: str, account_id: str, project_id: str, *, mode_revision: int,
                                expected_operation_id: str | None, operation: dict, current):
    """Store a bounded current-operation receipt while holding mode authority."""
    if not isinstance(operation, dict) or len(json.dumps(operation, separators=(',', ':')).encode()) > 16000:
        raise ValueError('Workspace operation metadata unavailable')
    with PREFERENCE_LOCK:
        mode = workspace_hex_mode(owner, account_id, project_id)
        prior = workspace_hex_operation(owner, account_id, project_id)
        if (mode != {'mode':'evolve', 'revision':mode_revision} or not current()
                or (prior or {}).get('operation_id') != expected_operation_id):
            raise ValueError('Workspace mode, operation or credential changed')
        prefs = _load_for_user(owner)
        entries = copy.deepcopy(prefs.get(_HEX_EVOLVE_KEY, {}))
        project = entries[account_id][project_id]
        project['operation'] = copy.deepcopy(operation)
        if not current(): raise ValueError('Credential changed')
        prefs[_HEX_EVOLVE_KEY] = entries
        _save_for_user_unlocked(owner, prefs)


def _mobile_control_side(value) -> str:
    """Normalize the mobile layout preference without mutating stored prefs."""
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _MOBILE_CONTROL_SIDES:
            return normalized
    return "system"




def load_binder_catalog(username: str, account_id: str):
    """Give a new account a renameable view of its existing journal pages."""
    from src.openclank.binder_catalog import empty_catalog, public_catalog, store_catalog

    with PREFERENCE_LOCK:
        prefs = _load_for_user(username)
        stored = prefs.get(_BINDER_CATALOG_KEY)
        if stored is not None:
            return public_catalog(stored, account_id)
        binder_id = "default-journal"
        catalog = empty_catalog(account_id)
        catalog.update(revision=1, defaultId=binder_id, configuredIds=[binder_id])
        catalog["descriptors"][binder_id] = {
            "id": binder_id, "displayName": "Journal", "provider": "copal",
            "source": {"workspaceId": "default", "corpus": "notes", "root": []},
            "enabled": True, "revision": 1,
        }
        prefs[_BINDER_CATALOG_KEY] = store_catalog(None, catalog, account_id)
        _save_for_user(username, prefs, allow_binder_catalog=True)
        return catalog


def binder_owner(request: Request) -> tuple[str, str]:
    from core.auth import normalize_known_username
    from src.auth_helpers import is_internal_tool_identity, require_user
    user = require_user(request)
    if is_internal_tool_identity(user):
        raise HTTPException(403, "Binder catalog requires a user session")
    manager = getattr(request.app.state, "auth_manager", None)
    username = normalize_known_username(getattr(manager, "users", {}), user)
    account = manager.account_id(username) if username and callable(getattr(manager, "account_id", None)) else None
    if not isinstance(account, str) or not account.strip():
        raise HTTPException(401, "Durable account identity is unavailable")
    return username, account


def setup_prefs_routes():
    router = APIRouter(prefix="/api/prefs", tags=["preferences"])

    @router.get("")
    async def get_all_prefs(request: Request):
        user = get_current_user(request)
        prefs = _load_for_user(user)
        prefs.pop(_BINDER_CATALOG_KEY, None)
        prefs.pop(_HEX_EVOLVE_KEY, None)
        prefs.pop("code_vector_search_consent", None)
        marker=prefs.pop('_document_vector_profile_version',None)
        if type(marker) is not int or marker!=1:prefs.pop('document_vector_profile',None)
        if _MOBILE_CONTROL_SIDE_PREF in prefs:
            prefs[_MOBILE_CONTROL_SIDE_PREF] = _mobile_control_side(
                prefs[_MOBILE_CONTROL_SIDE_PREF]
            )
        if _MEMORY_POLICY_KEY in prefs:
            prefs[_MEMORY_POLICY_KEY] = _public_memory_policy(request, prefs[_MEMORY_POLICY_KEY])
        return prefs

    def _policy_owner(request: Request) -> str:
        from src.auth_helpers import require_privilege, effective_user
        from src.owner_identity import provider_owner_for, is_internal_tool_identity
        user = require_privilege(request, "can_manage_memory")
        if is_internal_tool_identity(user):
            raise HTTPException(403, "Memory policy requires an authenticated owner")
        owner = effective_user(request)
        if not owner:
            raise HTTPException(401, "Memory policy requires an authenticated owner")
        return provider_owner_for(owner)

    def _policy_allowed_models(request: Request, owner: str):
        # Match the existing normalized /api/models visibility projection.
        auth = getattr(getattr(request.app, "state", None), "auth_manager", None)
        get_privileges = getattr(auth, "get_privileges", None)
        if not callable(get_privileges):
            return None
        privileges = get_privileges(owner) or {}
        if privileges.get("block_all_models"):
            return frozenset()
        raw = privileges.get("allowed_models")
        allowed = frozenset(str(item) for item in raw) if isinstance(raw, list) else frozenset()
        return allowed if privileges.get("allowed_models_restricted") or allowed else None

    def _public_memory_policy(request: Request, value):
        from src.openclank.memory_policy import public_policy_response
        try:
            owner = _policy_owner(request)
            return public_policy_response(owner, value, allowed_models=_policy_allowed_models(request, owner))
        except Exception:
            # A generic preference read must never expose unprojected route IDs.
            return None

    def _document_vector_owner(request: Request):
        from src.auth_helpers import require_privilege, effective_user
        from src.owner_identity import is_internal_tool_identity
        user=require_privilege(request,'can_use_documents')
        if is_internal_tool_identity(user) or effective_user(request)!=user:
            raise HTTPException(403,'Named document owner required')
        state=request.app.state;auth=getattr(state,'auth_manager',None);capture=getattr(state,'capture_original_credential',None)
        if auth is None or not callable(capture):raise HTTPException(503,'Document authority unavailable')
        account=auth.account_id(user)
        if not account or auth.username_for_account_id(account)!=user:raise HTTPException(503,'Document authority unavailable')
        credential=capture(request,user,account)
        def current():
            try:
                return (getattr(state,'auth_manager',None) is auth and getattr(state,'capture_original_credential',None) is capture
                    and credential() is True and require_privilege(request,'can_use_documents')==user
                    and auth.account_id(user)==account and auth.username_for_account_id(account)==user)
            except Exception:return False
        if not callable(credential) or current() is not True:raise HTTPException(403,'Document authority unavailable')
        return user,account,current

    @router.get('/code-vector-search')
    async def code_vector_consent(request: Request):
        from src.openclank.code_vector_retrieval import ROUTE_FIELDS, consent_preview, disclosure, read_consent
        owner, account, current = _document_vector_owner(request)
        try:
            configured = await asyncio.to_thread(consent_preview, owner)
        except Exception:
            configured = None
        with PREFERENCE_LOCK:
            if current() is not True:
                raise HTTPException(409, "Code vector consent authority changed")
            consent = read_consent(_load_for_user(owner), account)
        exposed = {key: consent[key] for key in ROUTE_FIELDS} if consent else None
        return {"enabled": consent is not None, "configured": configured, "consent": exposed,
                "disclosure": disclosure()}

    @router.put('/code-vector-search')
    async def set_code_vector_consent(request: Request, body: dict):
        from uuid import uuid4
        from src.openclank.code_vector_retrieval import CONSENT_KEY, clear_account_vectors, consent_preview
        owner, account, current = _document_vector_owner(request)
        if set(body) != {"enabled", "expected_route"} or type(body["enabled"]) is not bool:
            raise HTTPException(422, "Choose whether to enable Code vector search")
        selected = None
        if body["enabled"]:
            try:
                selected = await asyncio.to_thread(consent_preview, owner)
            except Exception:
                raise HTTPException(409, "The managed embedding route is unavailable") from None
            if selected != body["expected_route"]:
                raise HTTPException(409, "Review the current Code vector route before enabling")
        elif body["expected_route"] is not None:
            raise HTTPException(422, "Disabling requires no embedding route")
        with PREFERENCE_LOCK:
            if current() is not True:
                raise HTTPException(409, "Code vector consent authority changed")
            prefs = _load_for_user(owner)
            if selected is None:
                prefs.pop(CONSENT_KEY, None)
            else:
                prefs[CONSENT_KEY] = {"version": 1, "account": account, "consent_epoch": uuid4().hex, **selected}
            _save_for_user_unlocked(owner, prefs)
            clear_account_vectors(account)
        return await code_vector_consent(request)

    @router.post('/code-vector-index')
    async def code_vector_index(request: Request):
        from fastapi.responses import JSONResponse
        from src.openclank.code_vector_build import manage_code_vectors, valid_request

        owner, account, current = _document_vector_owner(request)
        if (getattr(request.state, "authenticated", False) is not True
                or getattr(request.state, "api_token", False)
                or getattr(request.state, "internal_tool_authenticated", False)):
            raise HTTPException(403, "Named Code workspace owner required")
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > 4096:
                raise HTTPException(413, "Code vector index request exceeds its byte limit")
            payload.extend(chunk)
        try:
            body = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, "Invalid Code vector index request") from None
        if not valid_request(body):
            raise HTTPException(422, "Invalid Code vector index request")
        result = await manage_code_vectors(state=request.app.state, owner=owner, account=account,
                                           original_current=current, body=body)
        return JSONResponse(result, headers={"Cache-Control": "private, no-store"})

    @router.get('/document-vector-profile')
    async def document_vector_profile(request: Request):
        from src.openclank.document_vector_settings import selected
        from src.openclank.chat_source_bridge import canonical_source_provider
        from src.openclank.memory_policy_projection import policy_coordinator
        from src.memery_provider import NATIVE_VECTOR_PROFILE_OPERATION
        owner,account,current=_document_vector_owner(request)
        with PREFERENCE_LOCK:
            try:requested=selected(_load_for_user(owner))
            except ValueError as exc:raise HTTPException(409,'Document vector preference unavailable') from exc
        supported=False
        try:
            coordinator=policy_coordinator();provider=canonical_source_provider(request.app.state)
            if coordinator is not None and provider is not None and current() is True:
                resolved,target=coordinator.resolve(owner)
                if resolved==account and provider._scope(owner).workspace_id==target:
                    refs=coordinator.checked_refs(owner,account,target)
                    ready=await provider._host_negotiate_native_memory_ops(owner=owner,workspace_id=target)
                    supported=(ready is True and NATIVE_VECTOR_PROFILE_OPERATION in provider._native_memory_ops_support()
                        and current() is True and coordinator.checked_refs(owner,account,target)==refs
                        and coordinator.resolve(owner)==(account,target))
        except Exception:supported=False
        if current() is not True:raise HTTPException(409,'Document authority changed')
        with PREFERENCE_LOCK:
            if selected(_load_for_user(owner))!=requested:raise HTTPException(409,'Document preference changed')
        return {'version':1,'requested':requested,'profile_supported':supported,
            'effective':'exact' if requested['family']=='exact' or not supported else 'not_yet_verified'}

    @router.put('/document-vector-profile')
    async def set_document_vector_profile(request: Request,body: dict):
        from src.openclank.document_vector_settings import KEY,MARKER,selected,validate
        owner,account,current=_document_vector_owner(request)
        if type(body) is not dict or set(body)!={'expected','selection'}:raise HTTPException(422,'Expected current and complete selection')
        try:expected=validate(body['expected']);desired=validate(body['selection'])
        except ValueError as exc:raise HTTPException(422,'Invalid document vector selection') from exc
        def update(prefs):
            if selected(prefs)!=expected:raise ValueError('Document preference changed')
            if expected!=desired:prefs[KEY]=desired;prefs[MARKER]=1
            return prefs,None
        try:
            saved,_=_mutate_user(owner,update,allow_document_vector_policy=True,before_save=current)
            if current() is not True:raise ValueError('Document authority changed')
            return {'version':1,'requested':selected(saved)}
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc

    @router.get("/memory-policy/preview")
    async def preview_memory_policy(request: Request):
        from src.openclank.memory_policy import STORE, MemoryPolicyError, defaults, public_policy_response
        owner = _policy_owner(request)
        # Qualify optional rank support on this live scoped provider before
        # projecting settings; missing custody leaves the normal preview usable.
        rank_current=False
        try:
            from src.auth_helpers import effective_user
            from src.openclank.chat_source_bridge import canonical_source_provider
            from src.openclank.memory_policy_projection import policy_coordinator
            coordinator=policy_coordinator(); provider=canonical_source_provider(request.app.state)
            user=effective_user(request)
            if coordinator is not None and provider is not None and user:
                account,workspace=coordinator.resolve(user)
                refs=coordinator.checked_refs(user,account,workspace)
                if provider._scope(owner).workspace_id==workspace:
                    ready=await provider._host_negotiate_native_memory_ops(owner=owner,workspace_id=workspace)
                    rank_current=(ready is True and coordinator.checked_refs(user,account,workspace)==refs
                                  and coordinator.resolve(user)==(account,workspace))
        except Exception:
            pass
        try:
            snapshot=STORE.snapshot(owner)
            if not rank_current:
                from src.openclank.memory_policy import _fingerprint
                snapshot['capabilities']['consumers']['rank_learning']=False
                snapshot['fingerprint']=_fingerprint({key:value for key,value in snapshot.items() if key!='fingerprint'})
            return public_policy_response(owner, {**snapshot, "proposed_defaults":defaults()}, allowed_models=_policy_allowed_models(request, owner))
        except MemoryPolicyError as exc:
            raise HTTPException(409, {"code":exc.code, "message":str(exc)}) from exc

    @router.put("/memory-policy")
    async def accept_memory_policy(request: Request, body: dict):
        from src.openclank.memory_policy import STORE, MemoryPolicyError, public_policy_response
        owner = _policy_owner(request)
        if type(body) is not dict or set(body) != {"expected_revision", "policy"}:
            raise HTTPException(422, "Expected revision and complete policy are required")
        try:
            return public_policy_response(owner, STORE.accept(owner, body["policy"], expected_revision=body["expected_revision"]), allowed_models=_policy_allowed_models(request, owner))
        except MemoryPolicyError as exc:
            raise HTTPException(409 if exc.code == "revision_conflict" else 422, {"code":exc.code, "message":str(exc)}) from exc

    @router.put("/memory-policy/ratings")
    async def set_memory_ratings(request: Request, body: dict):
        from src.openclank.memory_policy import STORE, MemoryPolicyError, public_policy_response
        owner = _policy_owner(request)
        if type(body) is not dict or set(body) != {"expected_revision", "expected_owner", "ratings"}:
            raise HTTPException(422, "Expected owner, revision and ratings are required")
        if type(body["expected_owner"]) is not str or body["expected_owner"] != owner:
            raise HTTPException(409, "Authenticated account changed; refresh settings")
        try:
            return public_policy_response(owner, STORE.set_ratings(owner, body["ratings"], expected_revision=body["expected_revision"]), allowed_models=_policy_allowed_models(request, owner))
        except MemoryPolicyError as exc:
            raise HTTPException(409 if exc.code == "revision_conflict" else 422, {"code":exc.code, "message":str(exc)}) from exc

    @router.put("/memory-policy/work-kind/{work_kind}")
    async def set_memory_work_kind(request: Request, work_kind: str, body: dict):
        from src.openclank.memory_policy import STORE, MemoryPolicyError, normalize_public_assignment, public_policy_response
        owner = _policy_owner(request)
        if type(body) is not dict or set(body) != {"expected_revision", "expected_owner", "selection"}:
            raise HTTPException(422, "Expected owner, revision and selection are required")
        if type(body["expected_owner"]) is not str or body["expected_owner"] != owner:
            raise HTTPException(409, "Authenticated account changed; refresh settings")
        try:
            allowed = _policy_allowed_models(request, owner)
            selection = normalize_public_assignment(owner, body["selection"], allowed_models=allowed)
            return public_policy_response(owner, STORE.set_work_kind(owner, work_kind, selection, expected_revision=body["expected_revision"]), allowed_models=allowed)
        except MemoryPolicyError as exc:
            raise HTTPException(409 if exc.code == "revision_conflict" else 422, {"code":exc.code, "message":str(exc)}) from exc

    @router.get("/binder-catalog")
    async def get_binder_catalog(request: Request):
        from src.openclank.binder_catalog import BinderCatalogError
        username, account = binder_owner(request)
        try:
            return load_binder_catalog(username, account)
        except BinderCatalogError as exc:
            raise HTTPException(409, {"code": exc.code, "message": str(exc)}) from exc

    @router.put("/binder-catalog")
    async def put_binder_catalog(request: Request, body: dict):
        from src.openclank.binder_catalog import BinderCatalogError, prepare_storage_update, store_catalog
        username, account = binder_owner(request)
        if type(body) is not dict or set(body) != {"expectedAccountId", "expectedRevision", "catalog"}:
            raise HTTPException(422, "Expected account, revision and complete catalog are required")
        if body["expectedAccountId"] != account:
            raise HTTPException(409, "Authenticated account changed; refresh Binder settings")
        try:
            with PREFERENCE_LOCK:
                prefs = _load_for_user(username)
                stored = prefs.get(_BINDER_CATALOG_KEY)
                catalog = prepare_storage_update(stored, body["catalog"], account, body["expectedRevision"])
                prefs[_BINDER_CATALOG_KEY] = store_catalog(stored, catalog, account)
                _save_for_user(username, prefs, allow_binder_catalog=True)
                return catalog
        except BinderCatalogError as exc:
            raise HTTPException(409 if exc.code in {"revision_conflict", "account_mismatch", "provider_unavailable"} else 422, {"code": exc.code, "message": str(exc)}) from exc

    def memery_preferences(prefs):
        from src.memory_gate import memory_mode
        mode = memory_mode(prefs)
        from src.openclank.memery_preferences import normalize_preferences
        return {"enabled": prefs.get("memory_enabled") is not False, "mode": mode, **normalize_preferences(prefs)}

    @router.get("/memery-settings")
    async def get_memery_settings(request: Request):
        user, account = binder_owner(request)
        return {"account_id": account, **memery_preferences(_load_for_user(user))}

    @router.put("/memery-settings")
    async def put_memery_settings(request: Request, body: dict):
        from src.memory_gate import VALID_MODES
        user, account = binder_owner(request)
        if body.get("expected_account_id") != account:
            raise HTTPException(409, "Authenticated account changed")
        if type(body.get("enabled")) is not bool or body.get("mode") not in tuple(VALID_MODES):
            raise HTTPException(422, "Invalid Memery preferences")

        from src.openclank.memery_preferences import DEFAULTS, validate_preference
        extra = set(body) - {"enabled", "mode", "expected", "expected_account_id"}
        if extra and extra != set(DEFAULTS):
            raise HTTPException(422, "Incomplete or unknown recall settings")
        try:
            options = {key: validate_preference(key, body[key]) for key in extra}
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

        def prepare(current):
            previous = memery_preferences(current)
            expected = body.get("expected")
            keys = {"enabled", "mode"} | extra
            if (not isinstance(expected, dict) or set(expected) != keys
                    or any(type(expected[k]) is not type(previous[k]) or expected[k] != previous[k] for k in keys)):
                raise HTTPException(409, "Memery settings changed. Reopen the page to load them.")
            current.update(memory_enabled=body["enabled"], memory_mode=body["mode"], **options)
            return current, None

        saved, _ = _mutate_user(user, prepare, before_save=lambda: binder_owner(request) == (user, account))
        return {"account_id": account, **memery_preferences(saved)}

    @router.get("/{key}")
    async def get_pref(request: Request, key: str):
        if key in {_BINDER_CATALOG_KEY, _HEX_EVOLVE_KEY}:
            raise HTTPException(404, "Preference unavailable")
        if key == 'memory_policy_consolidation_interval_days':
            user, account = binder_owner(request)
            return {'key':key,'value':_load_for_user(user).get(key),'account_id':account}
        if key == 'clankshop':
            user, account = binder_owner(request)
            try: value = _clankshop_pref(_load_for_user(user).get(key, CLANKSHOP_DEFAULT))
            except (ValueError, TypeError): value = dict(CLANKSHOP_DEFAULT)
            return {'key':key, 'value':value, 'account_id':account}
        if key in {"deliverable_reduced_motion", "companion_enabled", "input_queue_enabled"}:
            user, account = binder_owner(request)
            return {"key":key, "value":_load_for_user(user).get(key) is True, "account_id":account}
        user = get_current_user(request)
        prefs = _load_for_user(user)
        value = prefs.get(key, False) if key == "code_context_enabled" else prefs.get(key)
        if key == "code_context_prompt_budget":
            from src.openclank.code_context import MAX_GRAPH_PROMPT_BUDGET, MIN_GRAPH_PROMPT_BUDGET
            if type(value) is not int or not MIN_GRAPH_PROMPT_BUDGET <= value <= MAX_GRAPH_PROMPT_BUDGET:
                value = MAX_GRAPH_PROMPT_BUDGET
        elif key == "memory_mode":
            from src.memory_gate import memory_mode
            value = memory_mode(prefs)
        elif key == 'memory_question_seeds':
            value = prefs.get(key, True)
        elif key in ('memory_assistant_interpretation', 'memory_policy_importance_enabled', 'memory_policy_connections_enabled', 'memory_policy_consolidation_enabled', 'memory_shadow_ranking_enabled'):
            value = prefs.get(key) is True
        elif key == _MEMORY_POLICY_KEY:
            value = _public_memory_policy(request, value)
        elif key == "permission_mode":
            value = value if value in _PERMISSION_MODES else "manual"
        elif key == _MOBILE_CONTROL_SIDE_PREF:
            value = _mobile_control_side(value)
        return {"key": key, "value": value}

    @router.put("/{key}")
    async def set_pref(request: Request, key: str, body: dict):
        from src.openclank.memery_preferences import DEFAULTS
        if key in DEFAULTS:
            raise HTTPException(422, "Use Memery settings to update recall preferences")
        if key in {_BINDER_CATALOG_KEY, _HEX_EVOLVE_KEY}:
            raise HTTPException(422, "Preference unavailable")
        if key == 'memory_policy_consolidation_interval_days':
            user, account = binder_owner(request)
            if type(body) is not dict or set(body)!={'value','expected_account_id'} or body['expected_account_id']!=account:
                raise HTTPException(409, 'Authenticated account changed or request invalid')
            value=body['value']
            if value is not None and (type(value) is not int or not 1<=value<=365):
                raise HTTPException(422, 'Interval must be 1–365 days or null to inherit')
            def prepare(current):
                if value is None: current.pop(key,None)
                else: current[key]=value
                return current,None
            try:
                saved,_=_mutate_user(user,prepare,before_save=lambda:binder_owner(request)==(user,account))
            except Exception as exc:
                raise HTTPException(503,'Memory policy is temporarily unavailable') from exc
            return {'key':key,'value':saved.get(key),'account_id':account}
        if key == 'clankshop':
            user, account = binder_owner(request)
            if type(body) is not dict or set(body) != {'value','expected_account_id'}:
                raise HTTPException(422, 'Expected account and Clankshop preferences are required')
            if body.get('expected_account_id') != account: raise HTTPException(409, 'The authenticated account changed')
            try: value = _clankshop_pref(body.get('value'))
            except (ValueError, TypeError): raise HTTPException(422, 'Invalid Clankshop preferences')
            _update_for_user(user, {key:value})
            return {'key':key, 'value':value, 'account_id':account}
        if key in {"deliverable_reduced_motion", "companion_enabled", "input_queue_enabled"}:
            user, account = binder_owner(request)
            if type(body) is not dict or set(body) != {"value", "expected_account_id"} or type(body.get("value")) is not bool:
                raise HTTPException(422, "Expected account and boolean value are required")
            if body.get("expected_account_id") != account:
                raise HTTPException(409, "The authenticated account changed")
            value = _update_for_user(user, {key:body["value"]})[key]
            return {"key":key, "value":value, "account_id":account}
        user = get_current_user(request)
        prefs = _load_for_user(user)
        value = body.get("value") if isinstance(body, dict) else None
        if key in ("memory_question_seeds", "memory_assistant_interpretation", 'memory_policy_importance_enabled', 'memory_policy_connections_enabled', 'memory_policy_consolidation_enabled', 'memory_shadow_ranking_enabled'):
            if type(value) is not bool:
                raise HTTPException(status_code=422, detail=f"{key} must be a boolean")
        elif key == "permission_mode":
            if not isinstance(value, str) or value.strip().lower() not in _PERMISSION_MODES:
                raise HTTPException(status_code=422, detail="permission_mode must be manual, yolo, or auto")
            value = value.strip().lower()
        elif key == _TRUST_PREF_MASTER:
            if not isinstance(value, bool):
                raise HTTPException(status_code=422, detail="memory_trust_auto must be a boolean")
        elif key == "code_context_prompt_budget":
            from src.openclank.code_context import MAX_GRAPH_PROMPT_BUDGET, MIN_GRAPH_PROMPT_BUDGET
            if type(value) is not int or not MIN_GRAPH_PROMPT_BUDGET <= value <= MAX_GRAPH_PROMPT_BUDGET:
                raise HTTPException(422, "Graph prompt budget must be between 768 and 24,000")
        elif key == "code_context_enabled":
            if type(value) is not bool:
                raise HTTPException(status_code=422, detail="code_context_enabled must be a boolean")
        elif key == _TRUST_PREF_KINDS:
            if not isinstance(value, dict) or any(
                kind not in _TRUST_KINDS or not isinstance(enabled, bool)
                for kind, enabled in value.items()
            ):
                raise HTTPException(
                    status_code=422,
                    detail="memory_trust_auto_kinds must map known kinds to booleans",
                )
            value = {kind: value[kind] for kind in sorted(value)}
        elif key == _MOBILE_CONTROL_SIDE_PREF:
            if not isinstance(value, str) or value.strip().lower() not in _MOBILE_CONTROL_SIDES:
                raise HTTPException(
                    status_code=422,
                    detail="mobile_control_side must be left, right, or system",
                )
            value = value.strip().lower()
        if key == "logging_preferences":
            raise HTTPException(422, "Use the validated Logging policy endpoint")
        if key == 'memory_policy_connections_predicate':
            raise HTTPException(422, "Use the validated connection predicate endpoint")
        if key == _MEMORY_POLICY_KEY:
            raise HTTPException(422, "Use the validated Memory policy endpoint")
        if key == _PROVIDER_METHOD_POLICY_KEY:
            raise HTTPException(422, "Use the validated Provider method policy endpoint")
        from src.openclank.memory_policy_projection import PolicyTransitionUnavailable
        try:
            prefs = _update_for_user(user, {key: value})
        except PolicyTransitionUnavailable as exc:
            raise HTTPException(status_code=503, detail="Memory policy is temporarily unavailable. Please try again later.") from exc
        return {"key": key, "value": prefs[key]}

    return router
