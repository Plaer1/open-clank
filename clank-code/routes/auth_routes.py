"""Authentication routes — login, logout, signup, status, user management."""

from fastapi import APIRouter, Request, Response, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool
from typing import Optional
import asyncio
import copy
from contextvars import ContextVar
from functools import wraps
import hashlib
import inspect
import ipaddress
import logging
import os

import json
import re
import uuid
from pathlib import Path

from core.atomic_io import atomic_write_json, atomic_write_text
from core.auth import AuthManager, SetAdminResult, TOKEN_TTL, is_reserved_username
from src.auth_helpers import copal_owner_for_user
from src.constants import DEEP_RESEARCH_DIR, MEMORY_FILE, PASSWORD_MIN_LENGTH, SKILLS_DIR
from src.rate_limiter import RateLimiter
from src.settings_scrub import scrub_settings
from src.settings import (
    load_settings as _load_settings,
    save_settings as _save_settings,
    load_features as _load_features,
    save_features as _save_features,
    DEFAULT_SETTINGS,
    PER_USER_MODEL_SETTING_KEYS,
    SETTINGS_LOCK,
)
from src.openclank.code_index_settings import (
    REVISION_KEY as CODE_INDEX_REVISION_KEY,
    MANAGED_KEYS as CODE_INDEX_MANAGED_KEYS,
    prepare_settings as prepare_code_index_settings,
    readback as code_index_settings_readback,
)
from src.agent_settings import validate_agent_settings, patch_agent_settings, agent_settings_schema, agent_settings_revision
from routes.prefs_routes import (
    PREFERENCE_LOCK,
    _load as _load_prefs,
    _load_for_user,
    _mutate_user,
    _save as _save_prefs,
    _save_for_user,
)
from src.integrations import (
    load_integrations,
    add_integration,
    update_integration,
    delete_integration,
    get_integration,
    mask_integration_secret,
    execute_api_call,
    INTEGRATION_PRESETS,
)
from src.reminder_endpoints import ReminderEndpointError, normalize_endpoints

logger = logging.getLogger(__name__)

_account_claim_cleanup = ContextVar("account_claim_cleanup", default=None)


def _guard_account_claim(endpoint):
    """Release only this invocation's claim on every exceptional exit.

    Cancellation can interrupt staging after an effect but before its receipt.
    Keep that operation fenced and resumable; never infer a rollback from a
    cancelled request. Existing domain compensation still runs for ordinary
    failures, and the guard leaves its already-released claim alone.
    """
    @wraps(endpoint)
    async def guarded(*args, **kwargs):
        token = _account_claim_cleanup.set(None)
        try:
            return await endpoint(*args, **kwargs)
        except BaseException as exc:
            cleanup = _account_claim_cleanup.get()
            if cleanup is not None:
                try:
                    cleanup(exc)
                except Exception:
                    logger.exception("Could not release interrupted account operation claim")
            raise
        finally:
            _account_claim_cleanup.reset(token)
    return guarded


def _register_account_claim_cleanup(coordinator, operation_id, claim_token, request):
    def cleanup(exc):
        operation = coordinator.get_operation(operation_id)
        claim = (operation.get("receipt") or {}).get("claim") or {}
        if operation.get("state") == "aborted":
            release = getattr(
                getattr(request.app.state, "research_handler", None),
                "release_owner_fence", None,
            )
            if callable(release):
                release(operation["source_owner"])
            return
        if operation.get("state") == "complete" or claim.get("token") != claim_token:
            return
        if operation.get("steps"):
            coordinator.fail_delete_operation(operation_id, exc, claim_token=claim_token)
        else:
            coordinator.abort_operation(operation_id, exc, claim_token=claim_token)
            release = getattr(
                getattr(request.app.state, "research_handler", None),
                "release_owner_fence", None,
            )
            if callable(release):
                release(operation["source_owner"])
    _account_claim_cleanup.set(cleanup)

_ACCOUNT_REQUEST_DRAIN_SECONDS = 30.0

_MODEL_SETTING_PAIRS = (
    ("default_endpoint_id", "default_model"),
    ("utility_endpoint_id", "utility_model"),
    ("memory_endpoint_id", "memory_model"),
    ("research_endpoint_id", "research_model"),
    ("task_endpoint_id", "task_model"),
)


def _validate_model_settings_update(body: dict, current: dict, request: Request, owner: str) -> None:
    del request
    from src.openclank.chat_routing import (
        ChatRouteUnavailable,
        normalized_provider_owner,
        resolve_chat_route,
        share_id_from_endpoint,
    )
    from src.openclank.provider_store import ProviderStore, ProviderStoreError

    normalized_owner = normalized_provider_owner(owner)
    store = ProviderStore()

    def _route(
        *,
        selector: str,
        endpoint_id: str = "",
        operations: set[str],
        label: str,
    ):
        route_selector = str(selector or "").strip()
        connection_selector = str(endpoint_id or "").strip()
        if not route_selector:
            raise HTTPException(400, f"{label} must select a stable model route")

        # Shared routes intentionally expose only the grant-backed public
        # endpoint and provider model ID. Resolve that pair through the same
        # normalized authorization path used by chat dispatch; source route,
        # connection, account, and credential identities remain private.
        if share_id_from_endpoint(connection_selector) is not None:
            try:
                shared = resolve_chat_route(
                    owner=normalized_owner,
                    endpoint_id=connection_selector,
                    model_id=route_selector,
                    provider_store=store,
                )
            except ChatRouteUnavailable as exc:
                raise HTTPException(
                    400,
                    f"{label} is not an available normalized model route",
                ) from exc
            if not operations.intersection(set(shared.operations or ())):
                raise HTTPException(
                    400,
                    f"{label} is not an available normalized model route",
                )
            return shared

        candidates = []
        try:
            exact = store.get_model_route(
                owner=normalized_owner,
                model_route_id=route_selector,
            )

            candidates.append(exact)
        except ProviderStoreError:
            # Compatibility with historical settings that stored the provider
            # model ID instead of the stable route ID. It is accepted only when
            # it resolves to one exact owned route.
            connections = (
                [store.get_connection(owner=normalized_owner, connection_id=connection_selector)]
                if connection_selector
                else store.list_connections(owner=normalized_owner)
            )
            for connection in connections:
                candidates.extend(
                    route
                    for route in store.list_model_routes(
                        owner=normalized_owner,
                        connection_id=connection.id,
                    )
                    if route.provider_model_id == route_selector
                )

        eligible = []
        for candidate in candidates:
            if connection_selector and candidate.connection_id != connection_selector:
                continue
            if (
                not candidate.enabled
                or candidate.deleted_at is not None
                or str(candidate.visibility or "visible") != "visible"
                or not operations.intersection(set(candidate.operations or ()))
            ):
                continue
            try:
                connection = store.get_connection(
                    owner=normalized_owner,
                    connection_id=candidate.connection_id,
                )
            except ProviderStoreError:
                continue
            if connection.enabled and connection.deleted_at is None:
                eligible.append(candidate)
        unique = {candidate.id: candidate for candidate in eligible}
        if len(unique) != 1:
            raise HTTPException(400, f"{label} is not an available normalized model route")
        return next(iter(unique.values()))

    for endpoint_key, model_key in _MODEL_SETTING_PAIRS:
        if endpoint_key not in body and model_key not in body:
            continue
        endpoint_id = str(body.get(endpoint_key, current.get(endpoint_key, "")) or "").strip()
        model_id = str(body.get(model_key, current.get(model_key, "")) or "").strip()
        if endpoint_id or model_id:
            if not endpoint_id or not model_id:
                raise HTTPException(400, f"{endpoint_key} and {model_key} must be selected together")
            _route(
                selector=model_id,
                endpoint_id=endpoint_id,
                operations={"chat.complete", "chat.stream"},
                label=model_key,
            )

    for fallback_key in (
        "default_model_fallbacks",
        "utility_model_fallbacks",
        "memory_model_fallbacks",
        "vision_model_fallbacks",
    ):
        if fallback_key not in body:
            continue
        fallbacks = body[fallback_key]
        if not isinstance(fallbacks, list):
            raise HTTPException(400, f"{fallback_key} must be a list")
        for fallback in fallbacks:
            if not isinstance(fallback, dict):
                raise HTTPException(400, f"{fallback_key} entries must be objects")
            endpoint_id = str(fallback.get("endpoint_id") or "").strip()
            model_id = str(fallback.get("model") or "").strip()
            if not endpoint_id or not model_id:
                raise HTTPException(400, f"Incomplete model in {fallback_key}")
            _route(
                selector=model_id,
                endpoint_id=endpoint_id,
                operations=(
                    {"vision.describe"}
                    if fallback_key == "vision_model_fallbacks"
                    else {"chat.complete", "chat.stream"}
                ),
                label=fallback_key,
            )

    for provider_key, model_key in (("tts_provider", "tts_model"), ("stt_provider", "stt_model")):
        provider = str(body.get(provider_key, current.get(provider_key, "")) or "")
        if provider.startswith("endpoint:"):
            endpoint_id = provider.split(":", 1)[1]
            model_id = str(body.get(model_key, current.get(model_key, "")) or "")
            _route(
                selector=model_id,
                endpoint_id=endpoint_id,
                operations={
                    "audio.synthesize" if provider_key == "tts_provider" else "audio.transcribe"
                },
                label=model_key,
            )

    if "vision_model" in body and body.get("vision_model"):
        _route(
            selector=str(body["vision_model"]),
            operations={"vision.describe"},
            label="vision_model",
        )

    if "image_model" in body and body.get("image_model"):
        _route(
            selector=str(body["image_model"]),
            operations={"image.generate"},
            label="image_model",
        )


def _validate_reminder_endpoints(value: dict, owner: str) -> None:
    """Validate endpoint references before replacing a saved preference."""
    rows = value.get("endpoints", []) if isinstance(value, dict) else []
    from core.database import EmailAccount, SessionLocal
    for index, endpoint in enumerate(rows):
        if not isinstance(endpoint, dict):
            continue
        account_id = str(endpoint.get("email_account_id") or "").strip()
        if account_id:
            db = SessionLocal()
            try:
                account = db.query(EmailAccount).filter(EmailAccount.id == account_id).first()
                if not account or (owner and account.owner not in (None, "", owner)):
                    raise HTTPException(400, f"Reminder endpoint {index + 1} references an inaccessible email account")
            finally:
                db.close()
        integration_id = str(endpoint.get("webhook_integration_id") or "").strip()
        if integration_id:
            integration = get_integration(integration_id)
            if not integration or integration.get("enabled", True) is False or not integration.get("base_url"):
                raise HTTPException(400, f"Reminder endpoint {index + 1} references an unavailable integration")
        ntfy_integration_id = str(endpoint.get("ntfy_integration_id") or "").strip()
        if ntfy_integration_id:
            integration = get_integration(ntfy_integration_id)
            if (
                not integration
                or str(integration.get("preset") or integration.get("name") or "").lower() != "ntfy"
                or integration.get("enabled", True) is False
                or not integration.get("base_url")
            ):
                raise HTTPException(400, f"Reminder endpoint {index + 1} references an unavailable ntfy integration")


def _settings_for_user(settings: dict, user: str) -> dict:
    """Return global policy plus only this user's model selections."""
    prefs = _load_for_user(user) if user else {}
    scoped = dict(settings)
    for key in PER_USER_MODEL_SETTING_KEYS:
        scoped[key] = prefs[key] if key in prefs else DEFAULT_SETTINGS[key]
    scoped["agent_settings_override"] = bool(user and "agent_settings" in prefs and prefs["agent_settings"] is not None)
    scoped["agent_settings_saved"] = prefs.get("agent_settings") or {}
    saved_agent = prefs.get("agent_settings") if isinstance(prefs.get("agent_settings"), dict) else {}
    saved_distill = saved_agent.get("distill") if isinstance(saved_agent.get("distill"), dict) else None
    activated = type(prefs.get("_skill_draft_policy_version")) is int and prefs["_skill_draft_policy_version"] == 1
    scoped["agent_settings_distill_intent"] = {
        "version": 1,
        "state": ("active" if saved_distill and saved_distill.get("auto") is True else "off") if activated
                 else ("legacy_unactivated" if saved_distill is not None else "off"),
        "eligibility": "not_evaluated",
    }
    scoped["agent_settings_revision"] = agent_settings_revision(prefs.get("agent_settings"))
    scoped["agent_settings_schema"] = agent_settings_schema()
    return scoped


def _detach_user_prefs(username: str):
    """Remove one account's JSON prefs, returning a rollback snapshot."""
    owner = str(username or "").strip().lower()
    prefs = _load_prefs()
    users = prefs.get("_users") if isinstance(prefs, dict) else None
    if not isinstance(users, dict):
        return None
    key = next(
        (stored for stored in users if str(stored).strip().lower() == owner),
        None,
    )
    if key is None:
        return None
    snapshot = (str(key), users.pop(key))
    _save_prefs(prefs)
    return snapshot


def _restore_user_prefs(snapshot) -> None:
    if snapshot is None:
        return
    key, value = snapshot
    prefs = _load_prefs()
    users = prefs.setdefault("_users", {})
    users.setdefault(key, value)
    _save_prefs(prefs)


def _rename_user_prefs(old_owner: str, new_owner: str) -> bool:
    """Move one preference record without exposing its content to a saga."""
    old_owner = str(old_owner or "").strip().lower()
    new_owner = str(new_owner or "").strip().lower()
    prefs = _load_prefs()
    users = prefs.get("_users") if isinstance(prefs, dict) else None
    if not isinstance(users, dict):
        return False
    old_key = next(
        (key for key in users if str(key).strip().lower() == old_owner),
        None,
    )
    new_key = next(
        (key for key in users if str(key).strip().lower() == new_owner),
        None,
    )
    if old_key is not None and new_key is not None:
        raise RuntimeError("source and target preference owners both exist")
    if old_key is None:
        return bool(new_key is not None)
    users[new_owner] = users.pop(old_key)
    _save_prefs(prefs)
    return True


def _inventory_present(payload) -> bool:
    if isinstance(payload, dict):
        return any(
            _inventory_present(value)
            for key, value in payload.items()
            if key not in {"owner", "workspace_id", "fingerprint"}
        )
    if isinstance(payload, (list, tuple, set)):
        return any(_inventory_present(value) for value in payload)
    if isinstance(payload, bool):
        return payload
    if isinstance(payload, (int, float)):
        return payload != 0
    return False


def _semantic_inventory_present(payload) -> bool:
    if type(payload) is not dict or type(payload.get('count')) is not int or type(payload.get('tables')) is not dict:
        raise RuntimeError('memory semantic inventory unavailable')
    return bool(payload['count'] or _inventory_present(payload['tables']))


def _account_memory_domain_stores(memory_provider):
    from services.memory.import_batch import MemoryImportBatchStore
    from services.memory.media_assets import MemoryMediaStore
    from src.constants import DATA_DIR, FM_DB_PATH

    db_path = getattr(memory_provider, "_fm_db_path", None) or FM_DB_PATH
    return (
        MemoryMediaStore.for_provider(
            memory_provider,
            default_db_path=FM_DB_PATH,
            default_data_dir=DATA_DIR,
        ),
        MemoryImportBatchStore(
            db_path=str(db_path),
            data_dir=os.path.dirname(str(db_path)) or DATA_DIR,
        ),
    )


def _account_file_domain_store():
    from routes import prefs_routes
    from src.openclank.account_file_lifecycle import (
        AccountFileLifecyclePaths,
        AccountFileOwnerLifecycle,
    )
    return AccountFileOwnerLifecycle(
        AccountFileLifecyclePaths(
            preferences_file=Path(prefs_routes.PREFS_FILE),
            completed_research_dir=Path(DEEP_RESEARCH_DIR),
            legacy_memory_file=Path(MEMORY_FILE),
            skills_dir=Path(SKILLS_DIR),
            include_skills=False,
        )
    )


def _account_sql_domain_store():
    from core.database import SessionLocal
    from src.openclank.sql_owner_lifecycle import CommonSqlOwnerLifecycle

    return CommonSqlOwnerLifecycle(SessionLocal)


def _account_ancillary_domain_store():
    from core.database import SessionLocal
    from src.openclank.account_ancillary_lifecycle import (
        build_account_ancillary_lifecycle,
    )

    return build_account_ancillary_lifecycle(SessionLocal)


def _account_skills_domain_store(request: Request):
    manager = getattr(request.app.state, "skills_manager", None)
    if manager is not None:
        return manager
    from services.memory.skills import SkillsManager
    from src.constants import DATA_DIR

    return SkillsManager(DATA_DIR)


def _account_shell_audit_store():
    from src.openclank.shell_audit_lifecycle import (
        build_shell_audit_owner_lifecycle,
    )

    return build_shell_audit_owner_lifecycle()


def _account_personal_rag_store(request: Request):
    from routes.personal_routes import PERSONAL_UPLOADS_DIR, PersonalRagLifecycle

    manager = getattr(request.app.state, "personal_docs_manager", None)
    rag_manager = getattr(manager, "rag_manager", None) if manager is not None else None
    return PersonalRagLifecycle(
        upload_root=str(PERSONAL_UPLOADS_DIR),
        personal_docs_manager=manager,
        rag_manager=rag_manager,
        canonical_rag_managed_externally=True,
    )


async def _drain_owner_provider_flows(request: Request, owner: str) -> None:
    """Fence native-provider login tasks before an account ownership mutation."""
    from routes.provider_v1_routes import purge_owner_provider_flows

    supervisor = getattr(request.app.state, "mimo_supervisor", None)
    await purge_owner_provider_flows(supervisor, owner)


async def _memory_owner_transition(provider, *, action, owner, new_owner=None, lifecycle, operation_id, claim_token, state, _joined=False):
    """Carry the exact claimed source-fence capability to guarded FM only."""
    from src.memery_provider import MemeryProvider
    from src.openclank.chat_source_bridge import canonical_source_provider
    canonical = canonical_source_provider(state)
    if canonical is not None and canonical is not provider and not _joined:
        raise RuntimeError('canonical memory owner transition requires reconciliation')
    kwargs = {'owner': owner}
    if isinstance(provider, MemeryProvider) and provider._guarded_v17:
        from src.openclank.chat_source_bridge import issue_account_source_transition
        kwargs['_source_capability'] = issue_account_source_transition(lifecycle=lifecycle,
            operation_id=operation_id, claim_token=claim_token, provider=provider,
            action=action, owner=owner, new_owner=new_owner)
    if action == 'rename':
        return await provider.rename_owner(new_owner, **kwargs)
    if action == 'purge':
        return await provider.purge_owner(**kwargs)
    raise ValueError('unsupported memory owner transition')


def _claimed_memory_assessment_guard(*,state,lifecycle,operation_id,claim_token,account_id,host_owner,provider):
    """Current named account, existing claim and exact prepared FM lifetime."""
    from src.openclank.chat_source_bridge import canonical_source_provider
    from src.memery_database import GuardedStorePreparation
    preparation=getattr(provider,'_guarded_preparation',None);auth=getattr(state,'auth_manager',None)
    def current():
        try:
            if (type(preparation) is not GuardedStorePreparation or provider._guarded_preparation is not preparation
                or canonical_source_provider(state) is not provider or getattr(state,'auth_manager',None) is not auth
                or getattr(state,'account_owner_lifecycle',None) is not lifecycle or auth is None
                or auth.account_id(host_owner)!=account_id or auth.username_for_account_id(account_id)!=host_owner
                or os.path.realpath(provider._local_db_path() or '')!=preparation.database_path):return False
            operation=lifecycle.get_operation(operation_id);claim=(operation.get('receipt') or {}).get('claim') or {}
            if (operation.get('operation_id')!=operation_id or operation.get('subject_id')!=account_id
                or operation.get('source_owner')!=host_owner or operation.get('kind') not in ('rename','delete')
                or claim.get('token')!=claim_token or not claim_token or not lifecycle.owner_has_active_operation(host_owner)):return False
            preparation.assert_valid()
            return (provider._guarded_preparation is preparation and canonical_source_provider(state) is provider
                and getattr(state,'auth_manager',None) is auth and auth.account_id(host_owner)==account_id
                and auth.username_for_account_id(account_id)==host_owner)
        except Exception:return False
    return current


async def _assess_claimed_memory_source(provider,inventory,*,state,lifecycle,operation_id,claim_token,account_id,host_owner,source_owner,phase=None):
    from src.memery_provider import MemeryProvider
    if not isinstance(provider,MemeryProvider) or not getattr(provider,'_guarded_v17',False):
        _require_s02_rename_safe(provider,inventory);return
    if (type(inventory) is not dict or type(inventory.get('s02_rename_blocked')) is not bool
        or type(inventory.get('selective_rename_blocked')) is not bool or type(inventory.get('fingerprint')) is not str):
        raise RuntimeError('memory source assessment unavailable')
    if phase is None:
        if source_owner!=host_owner:raise RuntimeError('memory source assessment unavailable')
        guard=_claimed_memory_assessment_guard(state=state,lifecycle=lifecycle,operation_id=operation_id,claim_token=claim_token,
            account_id=account_id,host_owner=host_owner,provider=provider)
    else:
        from src.openclank.chat_source_bridge import issue_account_source_prefence_transition
        if type(phase) is not dict or phase.get('owner')!=source_owner or phase.get('account_id')!=account_id:
            raise RuntimeError('memory source assessment unavailable')
        capability=issue_account_source_prefence_transition(state=state,lifecycle=lifecycle,operation_id=operation_id,
            claim_token=claim_token,provider=provider,phase=phase,host_owner=host_owner)
        def guard():
            try:
                capability.assert_prefence_current(provider=provider,phase=phase,account_id=account_id,sql_owner=source_owner)
                return True
            except Exception:return False
    request={'version':1,'owner':source_owner,'workspace_id':provider._workspace_id,'account_id':account_id,
        'expected_source_fingerprint':inventory['fingerprint'],'phase':phase}
    await provider._host_s02_rename_assessment(request,current_guard=guard)


async def _verify_completed_fm_move(state,provider,operation,*,lifecycle,operation_id,claim_token,account_id,source,target,current_phase):
    """Read the exact already-applied original; this never issues a move."""
    from src.memery_provider import _HostOwnerTransition
    from src.openclank.chat_source_bridge import issue_account_source_transition
    manifest=operation.get('manifest') or {};participants=manifest.get('memory_participants') or {};fm=participants.get('fm') or {}
    snapshot=manifest.get('memory_fm');step=(operation.get('steps') or {}).get('memory_fm') or {}
    if (type(snapshot) is not dict or set(snapshot)!={'source','target','generation','prior_receipt_id','prior_receipt_hash','rag_relocation'}
        or type(fm) is not dict or type(fm.get('database_id')) is not str or step.get('state') not in ('applying','applied')
        or (step.get('state')=='applied' and type(step.get('receipt')) is not dict)):
        raise RuntimeError('memory transition original unavailable')
    request=_HostOwnerTransition(database_id=fm['database_id'],account_id=account_id,operation_id=operation_id,step='memory_fm',
        generation=snapshot['generation'],action='forward',owner=source,new_owner=target,
        expected_source_fingerprint=snapshot['source']['fingerprint'],expected_target_fingerprint=snapshot['target']['fingerprint'],
        prior_receipt_id=snapshot['prior_receipt_id'],prior_receipt_hash=snapshot['prior_receipt_hash'],rag_relocation=snapshot['rag_relocation'])
    phase_name='memory_fm_source_phase_'+str(request.generation);phase=manifest.get(phase_name);phase_step=(operation.get('steps') or {}).get(phase_name) or {}
    expected={key:getattr(request,key) for key in ('database_id','account_id','operation_id','step','generation','action','owner','new_owner')}
    if phase!=expected or current_phase!=expected or phase_step.get('state')!='applied' or phase_step.get('receipt')!=phase:
        raise RuntimeError('memory transition original phase unavailable')
    capability=issue_account_source_transition(lifecycle=lifecycle,operation_id=operation_id,claim_token=claim_token,
        provider=provider,action='rename',owner=source,new_owner=target)
    result=await provider._host_owner_transition(request,capability=capability,status=True)
    current=lifecycle.get_operation(operation_id);claim=(current.get('receipt') or {}).get('claim') or {}
    current_step=(current.get('steps') or {}).get('memory_fm') or {}
    if (result.get('state')!='applied' or (step.get('state')=='applied' and result!=step['receipt']) or claim.get('token')!=claim_token
        or current.get('subject_id')!=account_id or current_step!=step or (current.get('manifest') or {}).get('memory_fm')!=snapshot):
        raise RuntimeError('memory transition original changed')
    return result


def _require_s02_rename_safe(provider, inventory):
    from src.memery_provider import MemeryProvider
    if isinstance(provider, MemeryProvider):
        if type(inventory) is not dict or type(inventory.get('s02_rename_blocked')) is not bool:
            raise RuntimeError('S02 rename preflight unavailable')
        if inventory['s02_rename_blocked']:
            raise RuntimeError('S02 owner rename requires a reviewed provenance transition')
        _require_selective_rename_safe(provider, inventory)


def _require_selective_rename_safe(provider, inventory):
    from src.memery_provider import MemeryProvider
    if isinstance(provider, MemeryProvider) and getattr(provider, '_guarded_v17', False):
        if type(inventory) is not dict or type(inventory.get('selective_rename_blocked')) is not bool:
            raise RuntimeError('selective Memory rename preflight unavailable')
        if inventory['selective_rename_blocked']:
            raise RuntimeError('Memory owner rename requires a pending provenance transition')


async def _freeze_memory_participants(state, source, target, *, lifecycle, operation_id, claim_token, account_id, host_owner):
    """Freeze only the supported Native plus retained guarded-FM pair."""
    from src.memory_provider import NativeMemoryProvider
    from src.memery_provider import MemeryProvider
    from src.openclank.chat_source_bridge import canonical_source_provider
    selected = getattr(state, 'memory_provider', None)
    canonical = canonical_source_provider(state)
    if type(selected) is not NativeMemoryProvider or not isinstance(canonical, MemeryProvider) or canonical is selected:
        raise RuntimeError('retained memory participants unavailable')
    _require_owner_transition_capability(canonical)
    native_source = selected.lifecycle_snapshot(source)
    native_target = selected.lifecycle_snapshot(target)
    if native_target['count']:
        raise RuntimeError('native memory target already contains state')
    identity = canonical.persisted_database_identity()
    fm_source = await canonical.owner_stats(owner=source)
    fm_target = await canonical.owner_stats(owner=target)
    await _assess_claimed_memory_source(canonical,fm_source,state=state,lifecycle=lifecycle,operation_id=operation_id,claim_token=claim_token,account_id=account_id,host_owner=host_owner,source_owner=source)
    _require_s02_rename_safe(canonical, fm_target)
    if any(type(value.get('code_epoch_count')) is not int or value['code_epoch_count'] < 0 for value in (fm_source, fm_target)):
        raise RuntimeError('retained memory epoch inventory unavailable')
    if _inventory_present(fm_target):
        raise RuntimeError('retained memory target already contains state')
    return {'version': 1, 'native': {'source': native_source, 'target': native_target},
            'fm': {'database_id': identity['database_id'], 'source': fm_source, 'target': fm_target}}


async def _freeze_primary_guarded_memory(state, source, target, *, lifecycle, operation_id, claim_token, account_id, host_owner):
    """Freeze the existing primary FM owner for a typed guarded move."""
    from src.memery_provider import MemeryProvider
    from src.openclank.chat_source_bridge import canonical_source_provider
    selected=getattr(state,'memory_provider',None)
    if not isinstance(selected,MemeryProvider) or canonical_source_provider(state) is not selected:
        raise RuntimeError('guarded primary memory unavailable')
    _require_owner_transition_capability(selected)
    identity=selected.persisted_database_identity()
    before=await selected.owner_stats(owner=source); after=await selected.owner_stats(owner=target)
    await _assess_claimed_memory_source(selected,before,state=state,lifecycle=lifecycle,operation_id=operation_id,claim_token=claim_token,account_id=account_id,host_owner=host_owner,source_owner=source)
    _require_s02_rename_safe(selected,after)
    if _inventory_present(after): raise RuntimeError('guarded memory target already contains state')
    return {'version':1,'native':None,'fm':{'database_id':identity['database_id'],'source':before,'target':after}}


def _require_owner_transition_capability(provider):
    from src.memery_database import GuardedStorePreparation
    preparation = getattr(provider, '_guarded_preparation', None)
    ready = getattr(provider, '_ready', None)
    task = getattr(provider, '_owner_task', None)
    if (not getattr(provider, '_guarded_v17', False) or type(preparation) is not GuardedStorePreparation
            or preparation.capabilities.get('owner_transition') is not True or task is None or task.done()
            or ready is None or not ready.done() or ready.cancelled() or ready.exception() is not None):
        raise RuntimeError('retained memory owner capability unavailable')
    preparation.assert_valid()


def _memory_participants_current(state, manifest, *, status_only=False):
    from src.memory_provider import NativeMemoryProvider
    from src.memery_provider import MemeryProvider
    from src.openclank.chat_source_bridge import canonical_source_provider
    selected = getattr(state, 'memory_provider', None)
    canonical = canonical_source_provider(state)
    primary = type(manifest) is dict and manifest.get('native') is None
    if (type(manifest) is not dict or set(manifest) != {'version', 'native', 'fm'} or manifest['version'] != 1
            or not isinstance(canonical, MemeryProvider)
            or (primary and selected is not canonical)
            or (not primary and (type(selected) is not NativeMemoryProvider or canonical is selected
                or type(manifest['native']) is not dict or set(manifest['native']) != {'source', 'target'}))
            or type(manifest['fm']) is not dict or set(manifest['fm']) != {'database_id', 'source', 'target'}):
        raise RuntimeError('retained memory manifest changed')
    if status_only:
        from src.memery_database import GuardedStorePreparation
        preparation=getattr(canonical,'_guarded_preparation',None)
        if type(preparation) is not GuardedStorePreparation or preparation.database_id!=manifest['fm']['database_id']:
            raise RuntimeError('retained memory original identity unavailable')
        preparation._owned_move_schema_cookie()
    else:
        if canonical.persisted_database_identity()['database_id']!=manifest['fm']['database_id']:
            raise RuntimeError('retained memory manifest changed')
        _require_owner_transition_capability(canonical)
    if primary:
        return selected, canonical
    original = manifest['native']['source']
    if type(original) is not dict or type(original.get('owner')) is not str:
        raise RuntimeError('native memory manifest unavailable')
    current = selected.lifecycle_identity(original['owner'])
    if any(current.get(key) != original.get(key) for key in ('version', 'kind', 'root', 'device', 'inode')):
        raise RuntimeError('native memory domain changed')
    return selected, canonical


def _verify_guarded_delete_staging(*, store, provider, lifecycle, operation_id, claim_token,
                                   account_id, source, target, phase):
    """Check current typed custody and existing deletion staging checkpoints."""
    from src.openclank.chat_source_bridge import issue_account_source_transition
    from src.openclank.import_staging_custody import verified_staging_relocations
    if phase not in ('before_move','before_purge'):
        raise RuntimeError('staging delete phase unavailable')
    action='rename' if phase=='before_move' else 'purge'
    capability=issue_account_source_transition(lifecycle=lifecycle,operation_id=operation_id,
        claim_token=claim_token,provider=provider,action=action,
        owner=source if phase=='before_move' else target,new_owner=target if phase=='before_move' else None)
    def current():
        try:
            capability.assert_current(provider=provider,action=action,owner=source if phase=='before_move' else target,
                new_owner=target if phase=='before_move' else None)
            operation=lifecycle.get_operation(operation_id)
            return operation.get('subject_id')==account_id
        except Exception:
            return False
    prepared=getattr(provider,'_guarded_preparation',None)
    if (prepared is None or os.path.realpath(store.db_path)!=prepared.database_path
        or os.path.realpath(os.fspath(store.data_dir))!=os.path.dirname(prepared.database_path)
        or current() is not True):
        raise RuntimeError('staging delete authority unavailable')
    prepared.assert_valid()
    with store._connect() as db:
        db.execute('BEGIN')
        verified_staging_relocations(db,database_id=prepared.database_id,account_id=account_id,
            current_owner=target,lifecycle=lifecycle,current=current,delete_capability=capability,delete_phase=phase)
        db.commit()
    prepared.assert_valid()
    if current() is not True: raise RuntimeError('staging delete authority changed')


def _next_fm_generation(operation, step):
    """Advance only after a durable exact reverse and completed compensation."""
    manifests, steps = operation.get('manifest') or {}, operation.get('steps') or {}
    previous = manifests.get(step)
    if previous is None:
        return 1, None, None
    if type(previous) is not dict or type(previous.get('generation')) is not int or previous['generation'] < 1:
        raise RuntimeError('retained memory generation unavailable')
    generation = previous['generation']
    forward = (steps.get(f'memory_fm_forward_{generation}') or {}).get('receipt')
    reverse_step = steps.get(f'memory_fm_reverse_{generation}') or {}
    reverse = reverse_step.get('receipt')
    reverse_manifest = manifests.get(f'memory_fm_reverse_{generation}') or {}
    compensation = steps.get(f'memory_compensation_complete_{generation}') or {}
    if (type(forward) is not dict or forward.get('state') != 'applied' or type(reverse) is not dict
            or reverse_step.get('state') != 'applied' or reverse.get('state') != 'applied'
            or reverse_manifest.get('prior_receipt_id') != forward.get('receipt_id')
            or reverse_manifest.get('prior_receipt_hash') != forward.get('receipt_hash')
            or compensation.get('state') != 'applied' or (compensation.get('receipt') or {}).get('state') != 'complete'
            or ((compensation.get('receipt') or {}).get('results') or {}).get('memory_fm_tombstoned') != reverse):
        raise RuntimeError('retained memory compensation proof unavailable')
    return generation + 1, reverse.get('receipt_id'), reverse.get('receipt_hash')


def _claimed_fm_source_phase(*, operation, claim_token, database_id, account_id, operation_id,
                             step, generation, owner, new_owner, action='forward'):
    """Freeze only existing claimed pre-fence phase data; no future receipt."""
    claim=(operation.get('receipt') or {}).get('claim') if type(operation) is dict else None
    if (type(operation) is not dict or operation.get('operation_id')!=operation_id
        or operation.get('subject_id')!=account_id or type(claim) is not dict or claim.get('token')!=claim_token
        or type(generation) is not int or generation<1 or owner==new_owner or action not in ('forward','reverse')
        or any(type(v) is not str or not v or v!=v.strip() or len(v)>512
               for v in (database_id,account_id,operation_id,step,owner,new_owner))):
        raise RuntimeError('memory source phase unavailable')
    return {'database_id':database_id,'account_id':account_id,'operation_id':operation_id,
            'step':step,'generation':generation,'action':action,'owner':owner,'new_owner':new_owner}


async def _move_memory_participant(state, manifest, kind, source, target, *, lifecycle, operation_id, claim_token, started=False, reverse=False, fm_snapshot=None, step=None, cleanup_receipt=None):
    selected, canonical = _memory_participants_current(state, manifest, status_only=(kind=='fm' and started))
    if kind == 'native':
        frozen = manifest['native']
        if reverse:
            now_source, now_target = selected.lifecycle_snapshot(source), selected.lifecycle_snapshot(target)
            original = frozen['source']
            if now_source == frozen['target'] and now_target == original:
                return {'state': 'not_applied'}
            if (now_source['ids'] != original['ids'] or now_source['content_fingerprint'] != original['content_fingerprint']
                    or now_target['count'] != 0):
                raise RuntimeError('native memory compensation changed')
            return selected.lifecycle_rename(source, target, original=now_source, target_before=now_target)
        return selected.lifecycle_rename(source, target, original=frozen['source'], target_before=frozen['target'])
    if kind != 'fm':
        raise RuntimeError('unknown memory participant')
    before, destination = (None,None) if started else (await canonical.owner_stats(owner=source), await canonical.owner_stats(owner=target))
    frozen = manifest['fm']
    if reverse:
        if started:
            from src.memery_provider import _HostOwnerTransition
            from src.openclank.chat_source_bridge import issue_account_source_transition
            operation=lifecycle.get_operation(operation_id); manifests=operation.get('manifest') or {}; steps=operation.get('steps') or {}
            if type(fm_snapshot) is not dict or type(fm_snapshot.get('generation')) is not int:
                raise RuntimeError('retained memory reverse snapshot unavailable')
            generation=fm_snapshot['generation']; reverse_step='memory_fm_reverse_'+str(generation)
            snapshot=manifests.get(reverse_step); saved=steps.get(reverse_step) or {}
            if (type(snapshot) is not dict or saved.get('state') not in ('applying','applied')
                or type(step) is not str or not step):
                raise RuntimeError('retained memory reverse original unavailable')
            relocation=fm_snapshot.get('rag_relocation')
            inverse={'from':relocation['to'],'to':relocation['from']} if relocation is not None else None
            request=_HostOwnerTransition(database_id=manifest['fm']['database_id'],account_id=operation['subject_id'],
                operation_id=operation_id,step=step,generation=generation,action='reverse',owner=source,new_owner=target,
                expected_source_fingerprint=snapshot['source_fingerprint'],expected_target_fingerprint=snapshot['target_fingerprint'],
                prior_receipt_id=snapshot['prior_receipt_id'],prior_receipt_hash=snapshot['prior_receipt_hash'],rag_relocation=inverse)
            capability=issue_account_source_transition(lifecycle=lifecycle,operation_id=operation_id,claim_token=claim_token,
                provider=canonical,action='rename',owner=source,new_owner=target)
            receipt=await canonical._host_owner_transition(request,capability=capability,status=True)
            if receipt.get('state')!='applied' or (saved.get('state')=='applied' and saved.get('receipt')!=receipt):
                raise RuntimeError('retained memory reverse original unavailable')
            if cleanup_receipt is not None:
                if saved.get('state')!='applied' or saved.get('receipt')!=cleanup_receipt or receipt!=cleanup_receipt:
                    raise RuntimeError('retained memory reverse cleanup receipt changed')
                await canonical._host_owner_transition_cache_cleanup(request,capability=capability,receipt=receipt)
            return receipt
        baseline = fm_snapshot if type(fm_snapshot) is dict and {'source', 'target'} <= set(fm_snapshot) else frozen
        if before == baseline['target'] and destination == baseline['source']:
            return {'state': 'not_applied'}
        raise RuntimeError('retained memory compensation receipt unavailable')
    elif type(fm_snapshot) is not dict or set(fm_snapshot) != {'source', 'target', 'generation', 'prior_receipt_id', 'prior_receipt_hash', 'rag_relocation'}:
        raise RuntimeError('retained memory transition snapshot unavailable')
    elif not started and not (before == fm_snapshot['source'] and destination == fm_snapshot['target']):
        raise RuntimeError('retained memory transition state changed')
    from src.memery_provider import _HostOwnerTransition
    from src.openclank.chat_source_bridge import issue_account_source_transition
    if type(step) is not str or not step:
        raise RuntimeError('retained memory transition step unavailable')
    operation = lifecycle.get_operation(operation_id)
    request = _HostOwnerTransition(database_id=manifest['fm']['database_id'], account_id=operation['subject_id'],
        operation_id=operation_id, step=step, generation=fm_snapshot['generation'], action='forward', owner=source, new_owner=target,
        expected_source_fingerprint=fm_snapshot['source']['fingerprint'], expected_target_fingerprint=fm_snapshot['target']['fingerprint'],
        prior_receipt_id=fm_snapshot['prior_receipt_id'], prior_receipt_hash=fm_snapshot['prior_receipt_hash'],
        rag_relocation=fm_snapshot['rag_relocation'])
    phase_name='memory_fm_source_phase_'+str(request.generation)
    phase=(operation.get('manifest') or {}).get(phase_name)
    phase_step=(operation.get('steps') or {}).get(phase_name) or {}
    expected_phase={key:getattr(request,key) for key in ('database_id','account_id','operation_id','step','generation','action','owner','new_owner')}
    if type(phase) is not dict or phase!=expected_phase or phase_step.get('state')!='applied' or phase_step.get('receipt')!=phase:
        raise RuntimeError('original memory source phase unavailable')
    capability = issue_account_source_transition(lifecycle=lifecycle, operation_id=operation_id, claim_token=claim_token,
        provider=canonical, action='rename', owner=source, new_owner=target)
    receipt = await canonical._host_owner_transition(request, capability=capability, status=started)
    if receipt.get('state') != 'applied':
        # A started original has no positive not-issued proof. Never send it again.
        raise RuntimeError('retained memory transition original unavailable')
    if cleanup_receipt is not None:
        saved=(lifecycle.get_operation(operation_id).get('steps') or {}).get(step) or {}
        if saved.get('state')!='applied' or saved.get('receipt')!=cleanup_receipt or receipt!=cleanup_receipt:
            raise RuntimeError('retained memory transition cleanup receipt changed')
        await canonical._host_owner_transition_cache_cleanup(request,capability=capability,receipt=receipt)
    return receipt


async def _verify_fm_move_after(provider, source, target, receipt):
    """Check current inventory only after the original receipt is durable."""
    after_source,after_target=await provider.owner_stats(owner=source),await provider.owner_stats(owner=target)
    if (after_source.get('fingerprint')!=receipt['result']['source_after_fingerprint']
        or after_target.get('fingerprint')!=receipt['result']['target_after_fingerprint']):
        raise RuntimeError('retained memory transition receipt changed')
    return {'receipt_id':receipt['receipt_id'],'receipt_hash':receipt['receipt_hash']}


async def _claimed_fm_purge_stats(provider, owner, *, lifecycle, operation_id, claim_token):
    """Read the exact account-bound inventory under the active delete claim."""
    from src.openclank.chat_source_bridge import issue_account_source_transition
    capability=issue_account_source_transition(lifecycle=lifecycle,operation_id=operation_id,claim_token=claim_token,
        provider=provider,action='purge',owner=owner)
    return await provider._host_owner_purge_stats(owner=owner,capability=capability)


async def _purge_memory_participant(state, manifest, kind, owner, snapshot, *, lifecycle, operation_id, claim_token, started=False, step=None, cleanup_receipt=None):
    selected, canonical = _memory_participants_current(state, manifest, status_only=(kind=='fm' and started))
    if kind == 'native':
        receipt = selected.lifecycle_clear(owner, original=snapshot)
        if selected.lifecycle_snapshot(owner)['count']:
            raise RuntimeError('native memory purge remains incomplete')
        return receipt
    if kind != 'fm' or type(snapshot) is not dict:
        raise RuntimeError('retained memory purge snapshot unavailable')
    current = None if started else await _claimed_fm_purge_stats(canonical,owner,lifecycle=lifecycle,operation_id=operation_id,claim_token=claim_token)
    if not started and current != snapshot:
        raise RuntimeError('retained memory purge receipt unavailable')
    from src.memery_provider import _HostOwnerPurge
    from src.openclank.chat_source_bridge import issue_account_source_transition
    if type(step) is not str or not step:
        raise RuntimeError('retained memory purge step unavailable')
    operation = lifecycle.get_operation(operation_id)
    request = _HostOwnerPurge(database_id=manifest['fm']['database_id'], account_id=operation['subject_id'],
        operation_id=operation_id, step=step, generation=1, action='purge', owner=owner,
        expected_source_fingerprint=snapshot['fingerprint'])
    capability = issue_account_source_transition(lifecycle=lifecycle, operation_id=operation_id, claim_token=claim_token,
        provider=canonical, action='purge', owner=owner)
    receipt = await canonical._host_owner_purge(request, capability=capability, status=started)
    if receipt.get('state') != 'applied':
        raise RuntimeError('retained memory purge receipt unavailable')
    if cleanup_receipt is not None:
        saved=(lifecycle.get_operation(operation_id).get('steps') or {}).get(step) or {}
        if saved.get('state')!='applied' or saved.get('receipt')!=cleanup_receipt or receipt!=cleanup_receipt:
            raise RuntimeError('retained memory purge cleanup receipt changed')
        await canonical._host_owner_purge_cache_cleanup(request,capability=capability,receipt=receipt)
    return receipt


async def _personal_native_relocation_proof(state, *, lifecycle, operation_id, claim_token, step, personal_manifest, source, target):
    operation = lifecycle.get_operation(operation_id)
    manifests, steps = operation.get('manifest') or {}, operation.get('steps') or {}
    snapshot = manifests.get(step)
    saved = steps.get(step) or {}
    if (type(snapshot) is not dict or saved.get('state') != 'applied'
            or type(personal_manifest) is not dict or 'rag_relocation' not in personal_manifest
            or snapshot.get('rag_relocation') != personal_manifest['rag_relocation']):
        raise RuntimeError('personal native relocation receipt unavailable')
    receipt = await _move_memory_participant(state, manifests['memory_participants'], 'fm', source, target,
        lifecycle=lifecycle, operation_id=operation_id, claim_token=claim_token, started=True, fm_snapshot=snapshot, step=step)
    if receipt != saved.get('receipt'):
        raise RuntimeError('personal native relocation receipt changed')
    _, provider = _memory_participants_current(state, manifests['memory_participants'])
    from src.openclank.chat_source_bridge import issue_account_source_transition
    from routes.personal_routes import _issue_native_relocation_proof
    capability = issue_account_source_transition(lifecycle=lifecycle, operation_id=operation_id, claim_token=claim_token,
        provider=provider, action='rename', owner=source, new_owner=target)
    return _issue_native_relocation_proof(capability=capability, provider=provider, step=step, receipt=receipt,
        relocation=snapshot['rag_relocation'], source=source, target=target, manifest=personal_manifest)


def _require_memory_participant_join(state):
    """Admit only the supported two-store profile to the joined workflow."""
    from src.memory_provider import NativeMemoryProvider
    from src.memery_provider import MemeryProvider
    from src.openclank.chat_source_bridge import canonical_source_provider
    selected = getattr(state, 'memory_provider', None)
    canonical = canonical_source_provider(state)
    if canonical is not None and canonical is not selected:
        try:
            if type(selected) is not NativeMemoryProvider or selected.memory_vector is not None or not isinstance(canonical, MemeryProvider):
                raise ValueError('unsupported memory participants')
            _require_owner_transition_capability(canonical)
            return True
        except Exception as exc:
            raise HTTPException(503, 'Retained memory account lifecycle is unavailable') from exc
    return False


def _account_domain_provider(state, selected, joined):
    if not joined:
        return selected
    from src.openclank.chat_source_bridge import canonical_source_provider
    canonical = canonical_source_provider(state)
    if canonical is None:
        raise RuntimeError('canonical memory domain unavailable')
    return canonical


def _sync_account_skill_runtime(*owners):
    from routes.skills_routes import synchronize_owner_skill_job_runtime

    return synchronize_owner_skill_job_runtime(*owners)


def _transition_account_skills(request, source, target, manifest, *, compensate=False):
    store = _account_skills_domain_store(request)
    transition = store.compensate_owner_rename if compensate else store.reconcile_owner_rename
    try:
        return transition(source, target, manifest)
    finally:
        # Refresh even after a partially applied file effect. The next job
        # completion must serialize the durable owner keys, never stale ones.
        _sync_account_skill_runtime(source, target)


def _invalidate_account_runtime(
    request: Request,
    *owners: str,
    invalidate_tokens: bool = True,
    invalidate_session_cache: bool = True,
) -> dict[str, object]:
    """Fence process-local owner state without flushing unrelated owners."""
    from routes.model_routes import invalidate_model_catalogue_revision
    from services.memory.principal_context import invalidate_principal_cache
    from src.agent_loop import invalidate_cached_base_prompt
    from src.openclank.files_service_client import close_clients_for_owner

    email_invalidator = getattr(
        request.app.state,
        "invalidate_email_owner_runtime",
        None,
    )
    tui_invalidator = getattr(request.app.state, "invalidate_tui_owner_runtime", None)
    session_manager = getattr(request.app.state, "session_manager", None)
    invalidate_sessions = getattr(session_manager, "invalidate_owner_cache", None)
    receipt: dict[str, object] = {}
    for raw_owner in owners:
        owner = str(raw_owner or "").strip().lower()
        if not owner or owner in receipt:
            continue
        invalidate_model_catalogue_revision(owner)
        invalidate_principal_cache(owner)
        owner_receipt = {
            "prompt": invalidate_cached_base_prompt(owner),
            "files_clients": close_clients_for_owner(owner),
            "email": email_invalidator(owner) if callable(email_invalidator) else None,
            "tui": tui_invalidator(owner) if callable(tui_invalidator) else None,
            "sessions": (
                invalidate_sessions(owner)
                if invalidate_session_cache and callable(invalidate_sessions)
                else {"available": False}
            ),
        }
        receipt[owner] = owner_receipt
    token_invalidator = getattr(request.app.state, "invalidate_token_cache", None)
    if invalidate_tokens and callable(token_invalidator):
        token_invalidator()
    return receipt


class LoginRequest(BaseModel):
    username: str
    password: str
    remember: bool = True
    totp_code: Optional[str] = None


class SetupRequest(BaseModel):
    username: str
    password: str


class SignupRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class DeleteUserRequest(BaseModel):
    username: str
    operation_id: Optional[str] = None
    force_foreign_takeover: bool = False


class RenameUserRequest(BaseModel):
    username: str
    operation_id: Optional[str] = None
    force_foreign_takeover: bool = False


class SetAdminRequest(BaseModel):
    is_admin: bool


class FullAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


class SetOpenRegistrationRequest(BaseModel):
    enabled: bool

SESSION_COOKIE = "open_clank_session"


def _is_loopback_address(value: object) -> bool:
    candidate = str(value or "").strip().strip("[]")
    if not candidate:
        return False
    if candidate.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def _session_cookie_is_secure(request: Request) -> bool:
    """Choose a secure session cookie from explicit policy or request scheme.

    Explicit ``SECURE_COOKIES`` remains an operator override.  When it is
    unset, the ASGI request's HTTPS scheme receives a Secure cookie while
    direct HTTP stays usable. Proxy middleware must set that scheme only after
    applying its configured trust policy; a caller-supplied forwarding header
    is not cookie-security authority. A forced Secure policy
    still permits only a direct loopback HTTP login to support the native app.
    """
    configured = os.getenv("SECURE_COOKIES")
    if configured is None or not configured.strip():
        url = getattr(request, "url", None)
        scheme = str(getattr(url, "scheme", "") or "").lower()
        return scheme == "https"
    if configured.lower() != "true":
        return False
    url = getattr(request, "url", None)
    scheme = str(getattr(url, "scheme", "") or "").lower()
    hostname = str(getattr(url, "hostname", "") or "")
    peer = str(getattr(getattr(request, "client", None), "host", "") or "")
    if scheme == "http" and _is_loopback_address(hostname) and _is_loopback_address(peer):
        return False
    return True


def _copal_lifecycle_bridge(request: Request):
    state = getattr(getattr(request, "app", None), "state", None)
    if state is None or not hasattr(state, "copal_bridge"):
        return None
    bridge = state.copal_bridge
    if bridge is None:
        raise HTTPException(503, "Copal database bridge is unavailable")
    return bridge


def _mimo_lifecycle_supervisor(request: Request):
    """Return the production agent lifecycle authority, failing closed.

    Lightweight route-unit harnesses historically omit the state attribute and
    therefore retain the compatibility no-supervisor lane.  The real app sets
    the attribute explicitly; ``None`` means startup failed and must not be
    mistaken for proof that the owner has no runtime or grant state.
    """

    state = getattr(getattr(request, "app", None), "state", None)
    if state is None or not hasattr(state, "mimo_supervisor"):
        return None
    supervisor = state.mimo_supervisor
    required = (
        "preview_owner_rename",
        "reconcile_owner_rename",
        "compensate_owner_rename",
        "purge_owner_lifecycle",
        "owner_lifecycle_inventory",
    )
    if supervisor is None or any(
        not callable(getattr(supervisor, name, None)) for name in required
    ):
        raise HTTPException(503, "Open Clank agent lifecycle is unavailable")
    return supervisor


def _tombstone_inventory(steps, step, field, owner):
    """Bind a destructive purge to the partition actually staged by this operation."""
    checkpoint = steps.get(step)
    receipt = checkpoint.get("receipt") if isinstance(checkpoint, dict) else None
    inventory = receipt.get(field) if isinstance(receipt, dict) else None
    if (not isinstance(inventory, dict) or checkpoint.get("state") != "applied"
            or inventory.get("owner") != owner):
        raise RuntimeError("Staged tombstone inventory unavailable")
    return inventory


async def _call_copal_lifecycle(bridge, operation: str, args: dict):
    if bridge is None:
        return None
    try:
        is_alive = getattr(bridge, "is_alive", None)
        if callable(is_alive) and not is_alive():
            await bridge.start()
        return await bridge.call(operation, args, timeout=20)
    except Exception as exc:
        raise HTTPException(503, "Copal owner migration failed") from exc


def _drop_copal_calendar_projections_for_owner(db, owner: str) -> int:
    from core.database import CalendarCal, CalendarEvent

    calendar_ids = [
        row[0]
        for row in db.query(CalendarCal.id).filter(CalendarCal.owner == owner).all()
    ]
    if not calendar_ids:
        return 0
    return (
        db.query(CalendarEvent)
        .filter(
            CalendarEvent.calendar_id.in_(calendar_ids),
            CalendarEvent.origin == "copal",
        )
        .delete(synchronize_session=False)
    )


def _move_common_owner_rows(source_owner: str, target_owner: str) -> dict[str, int]:
    """Atomically stage legacy SQL ownership under a stable tombstone."""
    from sqlalchemy import func
    from core.database import Base, MimoProjectionState, ModelShareSubscription, SessionLocal

    counts: dict[str, int] = {}
    db = SessionLocal()
    try:
        from core.database import ScheduledTask
        if db.query(ScheduledTask.id).filter(func.lower(ScheduledTask.owner).in_((source_owner, target_owner)), ScheduledTask.host_account_id.isnot(None)).first():
            raise RuntimeError('bound task requires account-scoped SQL lifecycle')
        for mapper in Base.registry.mappers:
            model = mapper.class_
            if not hasattr(model, "owner"):
                continue
            source_count = db.query(model).filter(
                func.lower(model.owner) == source_owner
            ).count()
            target_count = db.query(model).filter(
                func.lower(model.owner) == target_owner
            ).count()
            if source_count and target_count:
                raise RuntimeError(f"split SQL owner state in {model.__name__}")
            if source_count:
                counts[model.__name__] = db.query(model).filter(
                    func.lower(model.owner) == source_owner
                ).update({"owner": target_owner}, synchronize_session=False)
        special = (
            (MimoProjectionState, MimoProjectionState.owner_id, "owner_id"),
            (ModelShareSubscription, ModelShareSubscription.subscriber, "subscriber"),
        )
        for model, column, attribute in special:
            source_count = db.query(model).filter(func.lower(column) == source_owner).count()
            target_count = db.query(model).filter(func.lower(column) == target_owner).count()
            if source_count and target_count:
                raise RuntimeError(f"split SQL owner state in {model.__name__}.{attribute}")
            if source_count:
                counts[f"{model.__name__}.{attribute}"] = db.query(model).filter(
                    func.lower(column) == source_owner
                ).update({attribute: target_owner}, synchronize_session=False)
        db.commit()
        return counts
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _purge_common_owner_rows(owner: str) -> dict[str, int]:
    from sqlalchemy import func
    from core.database import Base, MimoProjectionState, ModelShareSubscription, SessionLocal

    counts: dict[str, int] = {}
    db = SessionLocal()
    try:
        from core.database import ScheduledTask
        if db.query(ScheduledTask.id).filter(func.lower(ScheduledTask.owner) == owner, ScheduledTask.host_account_id.isnot(None)).first():
            raise RuntimeError('bound task requires account-scoped SQL lifecycle')
        for mapper in Base.registry.mappers:
            model = mapper.class_
            if hasattr(model, "owner"):
                removed = db.query(model).filter(
                    func.lower(model.owner) == owner
                ).delete(synchronize_session=False)
                if removed:
                    counts[model.__name__] = removed
        for model, column, label in (
            (MimoProjectionState, MimoProjectionState.owner_id, "owner_id"),
            (ModelShareSubscription, ModelShareSubscription.subscriber, "subscriber"),
        ):
            removed = db.query(model).filter(
                func.lower(column) == owner
            ).delete(synchronize_session=False)
            if removed:
                counts[f"{model.__name__}.{label}"] = removed
        db.commit()
        return counts
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def setup_auth_routes(
    auth_manager: AuthManager,
    *,
    account_lifecycle=None,
) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["auth"])
    ancillary_lifecycle = (
        _account_ancillary_domain_store()
        if account_lifecycle is not None
        and callable(getattr(type(account_lifecycle), "begin_delete_operation", None))
        else None
    )
    first_run_setup_lock = asyncio.Lock()
    full_access_lock = asyncio.Lock()

    _login_limiter = RateLimiter(max_requests=15, window_seconds=60)
    _signup_limiter = RateLimiter(max_requests=3, window_seconds=300)
    _setup_limiter = RateLimiter(max_requests=3, window_seconds=300)

    def _get_current_user(request: Request) -> Optional[str]:
        token = request.cookies.get(SESSION_COOKIE)
        return auth_manager.get_username_for_token(token)

    async def _sync_history_accounts(request: Request) -> None:
        # Auth commits remain the identity authority. Project current aliases
        # after lifecycle/account mutations, including zero-binding accounts.
        from src.openclank.file_policy import FilePolicyRepository
        from core.auth import normalize_known_username
        current_users = {name: record for name, record in auth_manager.users.items() if normalize_known_username(auth_manager.users, name)}
        repository = getattr(request.app.state, "files_policy_repository", None) or FilePolicyRepository()
        repository.sync_subjects(current_users)
        supervisor = getattr(request.app.state, "history_supervisor", None)
        sync = getattr(supervisor, "sync_accounts", None)
        if callable(sync):
            try:
                await sync(current_users)
            except Exception as exc:
                # Account changes remain authoritative in AuthManager; the
                # worker reports a paused state until its scoped credential
                # rotation succeeds on the next lifecycle retry.
                logger.error("History credential rotation failed closed: %s", exc)
                try:
                    await supervisor.stop()
                except Exception:
                    logger.exception("Failed to stop history worker after credential rotation failure")
                raise HTTPException(503, "History credential rotation unavailable") from exc

    def _wake_memory_provisioning(request: Request) -> None:
        # No-op on the legacy profile (request_completed_turn_wake is itself a no-op until a guarded registry is
        # configured); must never fail account creation. New accounts get their policy projection sooner this way,
        # but provisioning still completes asynchronously (G-DEF decision 4).
        try:
            scheduler = getattr(request.app.state, "task_scheduler", None)
            wake = getattr(scheduler, "request_completed_turn_wake", None)
            if callable(wake):
                wake()
        except Exception:
            logger.warning("Memory provisioning wake unavailable after account creation")

    def _reject_active_lifecycle_owner(username: str) -> None:
        checker = getattr(account_lifecycle, "owner_has_active_operation", None)
        if callable(checker) and checker(username):
            raise HTTPException(
                409,
                "Username is reserved by an unfinished account lifecycle operation",
            )

    @router.post("/setup")
    async def first_run_setup(body: SetupRequest, request: Request):
        """Create initial admin account. Only works if no accounts exist."""
        if not _setup_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        if auth_manager.is_configured:
            raise HTTPException(400, "Already configured")
        if len(body.password) < PASSWORD_MIN_LENGTH:
            raise HTTPException(400, f"Password must be at least {PASSWORD_MIN_LENGTH} characters")
        if len(body.username.strip()) < 1:
            raise HTTPException(400, "Username is required")
        if is_reserved_username(body.username):
            raise HTTPException(403, "Username is reserved")
        username = body.username.strip().lower()
        async with first_run_setup_lock:
            if auth_manager.is_configured:
                raise HTTPException(400, "Already configured")
            # Account bootstrap does not adopt or rename legacy local data.
            ok = await asyncio.to_thread(auth_manager.setup, username, body.password)
            if ok:
                await _sync_history_accounts(request)
                _wake_memory_provisioning(request)
                return {"ok": True, "message": "Admin account created"}
            raise HTTPException(500, "Setup failed")

    @router.post("/signup")
    async def signup(body: SignupRequest, request: Request):
        """Create a new user account. Only works if signup is enabled by admin."""
        if not _signup_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        if not auth_manager.is_configured:
            raise HTTPException(400, "Run setup first")
        if not auth_manager.signup_enabled:
            raise HTTPException(403, "Registration is disabled. Ask an admin for an account.")
        if len(body.password) < PASSWORD_MIN_LENGTH:
            raise HTTPException(400, f"Password must be at least {PASSWORD_MIN_LENGTH} characters")
        if len(body.username.strip()) < 1:
            raise HTTPException(400, "Username is required")
        if is_reserved_username(body.username):
            raise HTTPException(403, "Username is reserved")
        _reject_active_lifecycle_owner(body.username)
        ok = await asyncio.to_thread(auth_manager.create_user, body.username, body.password, is_admin=False)
        if not ok:
            raise HTTPException(409, "Username already taken")
        await _sync_history_accounts(request)
        _wake_memory_provisioning(request)
        return {"ok": True, "message": "Account created"}

    @router.post("/login")
    async def login(body: LoginRequest, request: Request, response: Response):
        if not _login_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        # Verify password first
        username = body.username.strip().lower()
        if not await asyncio.to_thread(auth_manager.verify_password, username, body.password):
            raise HTTPException(401, "Invalid credentials")
        # Check 2FA if enabled
        if auth_manager.totp_enabled(username):
            if not body.totp_code:
                # Password OK but need TOTP — tell client to show code input
                return {"ok": False, "requires_totp": True, "username": username}
            if not auth_manager.totp_verify(username, body.totp_code):
                raise HTTPException(401, "Invalid 2FA code")
        # All checks passed — create session (password already verified above)
        token = await asyncio.to_thread(auth_manager.create_session_trusted, username, remember=bool(body.remember))
        if not token:
            raise HTTPException(401, "Invalid credentials")
        cookie_kwargs = dict(
            key=SESSION_COOKIE,
            value=token,
            httponly=True,
            samesite="lax",
            secure=_session_cookie_is_secure(request),
            path="/",
        )
        if body.remember:
            cookie_kwargs.update(auth_manager.session_cookie_lifetime(token))
        response.set_cookie(**cookie_kwargs)
        return {"ok": True, "username": username}

    @router.post("/logout")
    async def logout(request: Request, response: Response):
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            auth_manager.revoke_token(token)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @router.get("/status")
    async def auth_status(request: Request):
        token = request.cookies.get(SESSION_COOKIE)
        result = auth_manager.status(token)
        result["signup_enabled"] = auth_manager.signup_enabled
        if result["authenticated"]:
            result["account_id"] = auth_manager.account_id(result["username"])
        return result

    @router.put("/full-access")
    async def update_full_access(body: FullAccessRequest, request: Request):
        username = _get_current_user(request)
        if not username or getattr(request.state, "api_token", False) or getattr(request.state, "internal_tool_authenticated", False):
            raise HTTPException(401, "Browser session required")
        account_id = auth_manager.account_id(username)
        if not account_id or request.headers.get("X-Openclank-Account") != account_id:
            raise HTTPException(409, "The active account changed")

        async with full_access_lock:
            if not auth_manager.full_access_status(username)["eligible"]:
                raise HTTPException(403, "Full access is unavailable for this account")
            if _get_current_user(request) != username or auth_manager.account_id(username) != account_id:
                raise HTTPException(409, "The active account changed")
            repository = getattr(request.app.state, "files_policy_repository", None)
            supervisor = getattr(request.app.state, "mimo_supervisor", None)
            invalidate_worker = getattr(supervisor, "invalidate_owner_projection", None)
            if repository is None or not callable(invalidate_worker):
                raise HTTPException(503, "Access transition is unavailable")

            async def persist():
                if not await asyncio.to_thread(auth_manager.set_owner_full_access, username, account_id, body.enabled):
                    raise HTTPException(409, "The active account changed")

            try:
                if not body.enabled:
                    # New admissions see the narrower authority before draining older work.
                    await persist()
                await asyncio.to_thread(repository.invalidate_account_access, account_id)
                from src.agent_tools.filesystem_tools import reject_file_approval_scope
                from src.shell_policy import reject_shell_approval_scope
                from src.openclank.files_service_client import close_clients_for_owner
                close_clients_for_owner(username)
                handler = supervisor.permission_handler_for(username)
                if handler is not None:
                    handler.reject_scope(all_pending=True)
                reject_file_approval_scope(owner=username, all_pending=True)
                reject_shell_approval_scope(owner=username, all_pending=True)
                await invalidate_worker(username)
                if body.enabled:
                    if _get_current_user(request) != username or auth_manager.account_id(username) != account_id:
                        raise HTTPException(409, "The active account changed")
                    await persist()
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception("Could not confirm owner access transition")
                raise HTTPException(503, "Access transition could not be confirmed. Check the current mode before retrying.") from exc

            return {
                "account_id": account_id,
                "full_access": auth_manager.full_access_status(username),
                "privileges": auth_manager.get_privileges(username),
            }

    @router.get("/policy")
    async def auth_policy():
        """Return public auth policy constants for the frontend."""
        return auth_manager.policy()

    @router.post("/change-password")
    async def change_password(body: ChangePasswordRequest, request: Request):
        user = _get_current_user(request)
        if not user:
            raise HTTPException(401, "Not authenticated")
        admin_empty_password = body.new_password == "" and auth_manager.is_admin(user)
        if len(body.new_password) < PASSWORD_MIN_LENGTH and not admin_empty_password:
            raise HTTPException(400, f"Password must be at least {PASSWORD_MIN_LENGTH} characters")
        current_token = request.cookies.get(SESSION_COOKIE)
        ok = await asyncio.to_thread(auth_manager.change_password, user, body.current_password, body.new_password)
        if not ok:
            raise HTTPException(400, "Current password is incorrect")
        await asyncio.to_thread(auth_manager.revoke_user_sessions, user, current_token)
        return {"ok": True}

    # ------------------------------------------------------------------
    # Two-factor authentication
    # ------------------------------------------------------------------

    @router.post("/2fa/setup")
    async def totp_setup(request: Request):
        """Generate a TOTP secret and return the QR code URI."""
        user = _get_current_user(request)
        if not user:
            raise HTTPException(401, "Not authenticated")
        if auth_manager.totp_enabled(user):
            raise HTTPException(400, "2FA is already enabled")
        secret = auth_manager.totp_generate_secret(user)
        if not secret:
            raise HTTPException(500, "Failed to generate secret")
        uri = auth_manager.totp_get_provisioning_uri(user, secret)
        # Generate QR code as base64 PNG
        import qrcode, io, base64
        qr = qrcode.make(uri, box_size=6, border=2)
        buf = io.BytesIO()
        qr.save(buf, format="PNG")
        qr_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return {"secret": secret, "uri": uri, "qr_code": f"data:image/png;base64,{qr_b64}"}

    class TotpVerifyRequest(BaseModel):
        code: str

    @router.post("/2fa/confirm")
    async def totp_confirm(body: TotpVerifyRequest, request: Request):
        """Verify a TOTP code to confirm 2FA setup. Returns backup codes."""
        user = _get_current_user(request)
        if not user:
            raise HTTPException(401, "Not authenticated")
        if not auth_manager.totp_confirm_enable(user, body.code):
            raise HTTPException(400, "Invalid code — try again")
        backup = auth_manager.users.get(user, {}).get("totp_backup_codes", [])
        return {"ok": True, "backup_codes": backup}

    class TotpDisableRequest(BaseModel):
        password: str

    @router.post("/2fa/disable")
    async def totp_disable(body: TotpDisableRequest, request: Request):
        """Disable 2FA. Requires password confirmation."""
        user = _get_current_user(request)
        if not user:
            raise HTTPException(401, "Not authenticated")
        if not auth_manager.totp_disable(user, body.password):
            raise HTTPException(400, "Invalid password")
        return {"ok": True}

    @router.get("/2fa/status")
    async def totp_status(request: Request):
        """Check if 2FA is enabled for the current user."""
        user = _get_current_user(request)
        if not user:
            raise HTTPException(401, "Not authenticated")
        return {"enabled": auth_manager.totp_enabled(user)}

    # Admin-only routes
    @router.get("/users")
    async def list_users(request: Request):
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        return {"users": auth_manager.list_users()}

    @router.post("/users")
    async def admin_create_user(body: CreateUserRequest, request: Request):
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        if len(body.password) < PASSWORD_MIN_LENGTH:
            raise HTTPException(400, f"Password must be at least {PASSWORD_MIN_LENGTH} characters")
        if len(body.username.strip()) < 1:
            raise HTTPException(400, "Username is required")
        if is_reserved_username(body.username):
            raise HTTPException(403, "Username is reserved")
        _reject_active_lifecycle_owner(body.username)
        ok = auth_manager.create_user(body.username, body.password, body.is_admin)
        if not ok:
            raise HTTPException(409, "Username already taken")
        await _sync_history_accounts(request)
        _wake_memory_provisioning(request)
        return {"ok": True}

    @router.put("/users/{username}/privileges")
    async def update_user_privileges(username: str, request: Request):
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        body = await request.json()
        from src.openclank.memory_policy_projection import PolicyTransitionUnavailable
        try:
            ok = auth_manager.set_privileges(username, body)
        except PolicyTransitionUnavailable as exc:
            raise HTTPException(503, "Memory policy is temporarily unavailable") from exc
        if not ok:
            raise HTTPException(404, "User not found or is admin")
        return {"ok": True, "privileges": auth_manager.get_privileges(username)}

    @router.put("/users/{username}/rename")
    @_guard_account_claim
    async def rename_user(username: str, body: RenameUserRequest, request: Request):
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        joined_memory = _require_memory_participant_join(request.app.state)
        old_username = (username or "").strip().lower()
        new_username = (body.username or "").strip().lower()
        durable_rename = account_lifecycle is not None and callable(
            getattr(type(account_lifecycle), "begin_rename_operation", None)
        )
        from src.memery_provider import MemeryProvider
        selected_memory = getattr(request.app.state,'memory_provider',None)
        guarded_primary = (not joined_memory and isinstance(selected_memory,MemeryProvider)
            and getattr(selected_memory,'_guarded_v17',False) is True)
        if guarded_primary and not durable_rename:
            raise HTTPException(503, 'Guarded memory account lifecycle is unavailable')
        typed_primary = guarded_primary and durable_rename
        typed_memory = joined_memory or typed_primary
        rename_operation = None
        requested_operation_id = getattr(body, "operation_id", None)
        if durable_rename and requested_operation_id:
            try:
                rename_operation = account_lifecycle.get_operation(requested_operation_id)
            except Exception as exc:
                raise HTTPException(404, "Account rename operation not found") from exc
            if (
                rename_operation.get("kind") != "rename"
                or rename_operation.get("source_owner") != old_username
                or rename_operation.get("target_owner") != new_username
            ):
                raise HTTPException(409, "Account rename operation target does not match")
        if not new_username:
            raise HTTPException(400, "Username required")
        if is_reserved_username(new_username):
            raise HTTPException(403, "Username is reserved")
        if old_username == new_username:
            return {"ok": True, "username": new_username, "renamed_self": old_username == user}
        if durable_rename and rename_operation is None and old_username == user:
            raise HTTPException(
                409,
                "Durable self-renames require another administrator to avoid recovery lockout",
            )
        if old_username not in auth_manager.users and rename_operation is None:
            raise HTTPException(404, "User not found")
        if new_username in auth_manager.users and rename_operation is None:
            raise HTTPException(409, "Username already taken")
        rename_subject_id = (
            rename_operation.get("subject_id")
            if rename_operation
            else auth_manager.account_id(old_username)
        )
        actor_account_id = auth_manager.account_id(user) or str(user)
        if durable_rename and rename_operation is None:
            try:
                rename_operation = account_lifecycle.begin_rename_operation(
                    actor_account_id=actor_account_id,
                    source_owner=old_username,
                    target_owner=new_username,
                    subject_id=rename_subject_id,
                )
            except Exception as exc:
                raise HTTPException(409, str(exc)) from exc
        rename_operation_id = str(
            (rename_operation or {}).get("operation_id") or ""
        )
        rename_claim_token = ""
        rename_steps = dict((rename_operation or {}).get("steps") or {})
        if durable_rename:
            try:
                rename_operation = account_lifecycle.claim_operation(
                    rename_operation_id,
                    expected_kind="rename",
                    force_foreign_takeover=bool(body.force_foreign_takeover),
                )
                rename_claim_token = str(
                    ((rename_operation.get("receipt") or {}).get("claim") or {}).get(
                        "token"
                    )
                    or ""
                )
                rename_steps = dict(rename_operation.get("steps") or {})
                _register_account_claim_cleanup(
                    account_lifecycle, rename_operation_id, rename_claim_token, request,
                )
            except Exception as exc:
                raise HTTPException(409, str(exc)) from exc

        def _rename_applied(step: str) -> bool:
            value = rename_steps.get(step)
            return value is True or (
                isinstance(value, dict) and value.get("state") == "applied"
            )

        def _rename_start(step: str, *, manifest=None) -> None:
            nonlocal rename_operation, rename_steps
            if not durable_rename:
                return
            rename_operation = account_lifecycle.start_operation_step(
                rename_operation_id,
                step,
                manifest=manifest,
                claim_token=rename_claim_token,
            )
            rename_steps = dict(rename_operation.get("steps") or {})

        def _rename_checkpoint(step: str, receipt=True, *, complete=False) -> None:
            nonlocal rename_operation, rename_steps
            if not durable_rename:
                return
            rename_operation = account_lifecycle.checkpoint_delete_operation(
                rename_operation_id,
                step,
                receipt,
                complete=complete,
                claim_token=rename_claim_token,
            )
            rename_steps = dict(rename_operation.get("steps") or {})

        def _rename_fail(exc: BaseException) -> None:
            if durable_rename:
                account_lifecycle.fail_delete_operation(
                    rename_operation_id,
                    exc,
                    claim_token=rename_claim_token,
                )

        def _rename_fail_or_rewind(exc: BaseException) -> None:
            if not durable_rename:
                return
            current_auth_owner = auth_manager.username_for_account_id(
                rename_subject_id
            )
            if current_auth_owner == new_username or _rename_applied("auth_renamed"):
                _rename_fail(exc)
                return
            release = getattr(research_handler, "release_owner_fence", None)
            if callable(release):
                release(old_username)
            account_lifecycle.rewind_operation_steps(
                rename_operation_id,
                (
                    "research_fenced",
                    "runtime_caches_fenced",
                    "auth_renamed",
                ),
                exc,
                claim_token=rename_claim_token,
            )

        def _rename_domain_error(label: str, exc: BaseException) -> None:
            _rename_fail_or_rewind(exc)
            if durable_rename:
                raise HTTPException(500, f"Account rename convergence failed in {label}") from exc

        class _RenameDomainApplied(Exception):
            pass

        if durable_rename:
            drain_owner_requests = getattr(
                request.app.state,
                "drain_account_owner_requests",
                None,
            )
            if not callable(drain_owner_requests):
                exc = RuntimeError("account request drain is unavailable")
                if rename_steps:
                    _rename_fail(exc)
                else:
                    account_lifecycle.abort_operation(
                        rename_operation_id,
                        exc,
                        claim_token=rename_claim_token,
                    )
                raise HTTPException(
                    503,
                    "Account rename cannot quiesce active requests",
                ) from exc
            try:
                # A timeout cancels only this waiter, not admitted work. Retry
                # the durable operation rather than treating timeout as drain.
                await asyncio.wait_for(drain_owner_requests(old_username), timeout=_ACCOUNT_REQUEST_DRAIN_SECONDS)
            except Exception as exc:
                if rename_steps:
                    _rename_fail(exc)
                else:
                    account_lifecycle.abort_operation(
                        rename_operation_id,
                        exc,
                        claim_token=rename_claim_token,
                    )
                raise HTTPException(
                    503,
                    "Account rename could not drain active requests",
                ) from exc
            quiesce_owner_writers = getattr(
                request.app.state,
                "quiesce_account_owner_writers",
                None,
            )
            if not callable(quiesce_owner_writers):
                exc = RuntimeError("account owner writer drain is unavailable")
                if rename_steps:
                    _rename_fail(exc)
                else:
                    account_lifecycle.abort_operation(
                        rename_operation_id,
                        exc,
                        claim_token=rename_claim_token,
                    )
                raise HTTPException(
                    503,
                    "Account rename cannot quiesce background writers",
                ) from exc
            try:
                await quiesce_owner_writers(old_username, subject_id=(rename_operation or {}).get('subject_id'))
            except Exception as exc:
                if rename_steps:
                    _rename_fail(exc)
                else:
                    account_lifecycle.abort_operation(
                        rename_operation_id,
                        exc,
                        claim_token=rename_claim_token,
                    )
                raise HTTPException(
                    503,
                    "Account rename could not quiesce background writers",
                ) from exc
            # Validate these again on every recovery attempt.  An old manifest
            # cannot prove that a failed current-process authority has no
            # retained state.  Release the request claim on refusal so retry is
            # possible after the service is repaired.
            try:
                _mimo_lifecycle_supervisor(request)
                _copal_lifecycle_bridge(request)
            except Exception as exc:
                if rename_steps:
                    _rename_fail(exc)
                else:
                    account_lifecycle.abort_operation(
                        rename_operation_id,
                        exc,
                        claim_token=rename_claim_token,
                    )
                if isinstance(exc, HTTPException):
                    raise
                raise HTTPException(
                    503,
                    "Account rename lifecycle authority is unavailable",
                ) from exc

        research_handler = getattr(request.app.state, "research_handler", None)
        if durable_rename and not (rename_operation.get("manifest") or {}).get(
            "preflight_version"
        ):
            try:
                memory_preflight = getattr(request.app.state, "memory_provider", None)
                normalized_source = account_lifecycle.owner_inventory(old_username)
                normalized_target = account_lifecycle.owner_inventory(new_username)
                if normalized_source["count"] and normalized_target["count"]:
                    raise RuntimeError("normalized rename target already contains state")
                sql_preflight = _account_sql_domain_store()
                sql_source = sql_preflight.owner_inventory(old_username, account_id=rename_subject_id)
                sql_target = sql_preflight.owner_inventory(new_username, account_id=rename_subject_id)
                if sql_source["count"] and sql_target["count"]:
                    raise RuntimeError("SQL rename target already contains state")
                file_preflight = _account_file_domain_store().preview_rename(
                    old_username,
                    new_username,
                )
                skills_preflight = _account_skills_domain_store(
                    request
                ).preview_owner_rename(old_username, new_username)
                shell_audit_preflight = _account_shell_audit_store().preview_owner_rename(
                    old_username,
                    new_username,
                )
                ancillary_preflight = ancillary_lifecycle.preview_owner_rename(
                    old_username,
                    new_username,
                    account_id=(rename_operation or {}).get('subject_id'),
                )
                upload_preflight_store = getattr(request.app.state, "upload_handler", None)
                uploads_preflight = (
                    upload_preflight_store.preview_owner_rename(
                        old_username,
                        new_username,
                    )
                    if upload_preflight_store is not None
                    else None
                )
                personal_preflight = _account_personal_rag_store(
                    request
                ).preview_owner_rename(old_username, new_username)
                from src.openclank.filesystem_registry import FilesystemRootRegistry
                filesystem_preflight = FilesystemRootRegistry().preview_owner_rename(
                    old_username, new_username
                )
                mimo_preflight_store = getattr(
                    request.app.state,
                    "mimo_supervisor",
                    None,
                )
                mimo_preflight = (
                    await mimo_preflight_store.preview_owner_rename(
                        old_username,
                        new_username,
                    )
                    if callable(
                        getattr(mimo_preflight_store, "preview_owner_rename", None)
                    )
                    else {"available": False}
                )
                research_preview = getattr(
                    research_handler,
                    "preview_owner_rename",
                    None,
                )
                research_preflight = (
                    research_preview(old_username, new_username)
                    if callable(research_preview)
                    else {"available": False}
                )
                media_preflight = staging_preflight = None
                memory_source = memory_target = None
                if memory_preflight is not None:
                    memory_source = await memory_preflight.owner_stats(owner=old_username)
                    memory_target = await memory_preflight.owner_stats(owner=new_username)
                    await _assess_claimed_memory_source(memory_preflight,memory_source,state=request.app.state,lifecycle=account_lifecycle,operation_id=rename_operation_id,claim_token=rename_claim_token,account_id=rename_subject_id,host_owner=old_username,source_owner=old_username)
                    _require_s02_rename_safe(memory_preflight, memory_target)
                    if _inventory_present(memory_source) and _inventory_present(memory_target):
                        raise RuntimeError("Memory rename target already contains state")
                    media_preflight_store, staging_preflight_store = (
                        _account_memory_domain_stores(_account_domain_provider(request.app.state, memory_preflight, joined_memory))
                    )
                    media_preflight = {
                        "source": media_preflight_store.preview_owner_purge(old_username),
                        "target": media_preflight_store.preview_owner_purge(new_username),
                    }
                    staging_preflight = {
                        "source": staging_preflight_store.preview_owner_staging(old_username,guarded=typed_memory),
                        "target": staging_preflight_store.preview_owner_staging(new_username,guarded=typed_memory),
                    }
                    if media_preflight["source"]["count"] and media_preflight["target"]["count"]:
                        raise RuntimeError("Memory media rename target already contains state")
                    if staging_preflight["source"]["count"] and staging_preflight["target"]["count"]:
                        raise RuntimeError("import staging rename target already contains state")
                copal_preflight = await _call_copal_lifecycle(
                    _copal_lifecycle_bridge(request),
                    "preflight_rename_owner",
                    {
                        "old_owner": copal_owner_for_user(old_username),
                        "new_owner": copal_owner_for_user(new_username),
                    },
                )
                manifest = {
                    "preflight_version": 1,
                    "task_runtime": (
                        request.app.state.task_scheduler.preview_owner_runtime_rename(
                            old_username, new_username,
                        )
                        if getattr(request.app.state, "task_scheduler", None) is not None
                        else {"available": False}
                    ),
                    "normalized": {"source": normalized_source, "target": normalized_target},
                    "common_sql": sql_source,
                    "common_sql_target": sql_target,
                    "file_domains": file_preflight,
                    "skills": skills_preflight,
                    "shell_audit": shell_audit_preflight,
                    "ancillary": ancillary_preflight,
                    "uploads": uploads_preflight,
                    "personal_rag": personal_preflight,
                    "filesystem_registry": filesystem_preflight,
                    "mimo": mimo_preflight,
                    "active_research": research_preflight,
                    "memory": {"source": memory_source, "target": memory_target},
                    "memory_media": media_preflight,
                    "import_staging": staging_preflight,
                    "copal": copal_preflight or {"available": False},
                }
                from src.openclank.chat_source_bridge import canonical_source_provider
                if canonical_source_provider(request.app.state) is not None and canonical_source_provider(request.app.state) is not memory_preflight:
                    manifest['memory_participants'] = await _freeze_memory_participants(request.app.state, old_username, new_username,lifecycle=account_lifecycle,operation_id=rename_operation_id,claim_token=rename_claim_token,account_id=rename_subject_id,host_owner=old_username)
                elif typed_primary:
                    manifest['memory_participants'] = await _freeze_primary_guarded_memory(request.app.state, old_username, new_username,lifecycle=account_lifecycle,operation_id=rename_operation_id,claim_token=rename_claim_token,account_id=rename_subject_id,host_owner=old_username)
                rename_operation = account_lifecycle.freeze_operation_manifest(
                    rename_operation_id,
                    manifest,
                    claim_token=rename_claim_token,
                )
                rename_steps = dict(rename_operation.get("steps") or {})
            except Exception as exc:
                account_lifecycle.abort_operation(
                    rename_operation_id,
                    exc,
                    claim_token=rename_claim_token,
                )
                release = getattr(research_handler, "release_owner_fence", None)
                if callable(release):
                    release(old_username)
                raise HTTPException(409, f"Account rename preflight failed: {exc}") from exc
        # Recheck the originally frozen inventory even on resumed operations.
        # Old manifests without the selective proof cannot authorize a move.
        if durable_rename:
            try:
                from src.openclank.chat_source_bridge import canonical_source_provider
                saved_manifest = rename_operation.get('manifest') or {}
                selected_provider = getattr(request.app.state, 'memory_provider', None)
                saved_memory = saved_manifest.get('memory') or {}
                _require_s02_rename_safe(selected_provider, saved_memory.get('target'))
                canonical = canonical_source_provider(request.app.state)
                if canonical is not None and canonical is not selected_provider:
                    saved_fm = (saved_manifest.get('memory_participants') or {}).get('fm') or {}
                    _require_s02_rename_safe(canonical, saved_fm.get('target'))
                # A saved preflight is not current evidence on recovery.
                from src.memery_provider import MemeryProvider
                from src.openclank.chat_source_bridge import _account_source_saved_phase
                phase=_account_source_saved_phase(rename_operation)
                source_owner=phase['owner'] if phase is not None else old_username
                target_owner=phase['new_owner'] if phase is not None else new_username
                current_host=auth_manager.username_for_account_id(rename_subject_id)
                for participant in (selected_provider, canonical if canonical is not selected_provider else None):
                    if isinstance(participant, MemeryProvider) and getattr(participant, '_guarded_v17', False):
                        fm_step=(rename_steps.get('memory_fm') or {})
                        if type(fm_step) is dict and fm_step.get('state') in ('applying','applied'):
                            original=await _verify_completed_fm_move(request.app.state,participant,rename_operation,lifecycle=account_lifecycle,
                                operation_id=rename_operation_id,claim_token=rename_claim_token,account_id=rename_subject_id,
                                source=old_username,target=new_username,current_phase=phase)
                            if fm_step.get('state')=='applying': _rename_checkpoint('memory_fm',original)
                            if not _rename_applied('memory_fm_after_verified'):
                                _rename_start('memory_fm_after_verified')
                                await _move_memory_participant(request.app.state,saved_manifest['memory_participants'],'fm',old_username,new_username,
                                    lifecycle=account_lifecycle,operation_id=rename_operation_id,claim_token=rename_claim_token,
                                    started=True,fm_snapshot=saved_manifest['memory_fm'],step='memory_fm',cleanup_receipt=original)
                                verified=await _verify_fm_move_after(participant,old_username,new_username,original)
                                _rename_checkpoint('memory_fm_after_verified',verified)
                            continue
                        source_inventory=await participant.owner_stats(owner=source_owner)
                        await _assess_claimed_memory_source(participant,source_inventory,state=request.app.state,lifecycle=account_lifecycle,
                            operation_id=rename_operation_id,claim_token=rename_claim_token,account_id=rename_subject_id,
                            host_owner=current_host,source_owner=source_owner,phase=phase)
                        target_inventory=await participant.owner_stats(owner=target_owner)
                        _require_s02_rename_safe(participant,target_inventory)
            except Exception as exc:
                _rename_fail_or_rewind(exc)
                raise HTTPException(409, f'Account rename preflight failed: {exc}') from exc
        if typed_memory and (not durable_rename or not (rename_operation.get('manifest') or {}).get('memory_participants') or rename_steps.get('memory')):
            exc = RuntimeError('ambiguous legacy memory checkpoint')
            if durable_rename:
                _rename_fail(exc)
            raise HTTPException(503, 'Retained memory account manifest unavailable') from exc
        if durable_rename and not _rename_applied("research_fenced"):
            try:
                _rename_start("research_fenced")
                fence = getattr(research_handler, "fence_owner", None)
                receipt = fence(old_username) if callable(fence) else {"available": False}
                _rename_checkpoint("research_fenced", receipt)
            except Exception as exc:
                _rename_domain_error("research writer fence", exc)
        if durable_rename and not _rename_applied("runtime_caches_fenced"):
            _rename_start("runtime_caches_fenced")
            _rename_checkpoint(
                "runtime_caches_fenced",
                _invalidate_account_runtime(
                    request, old_username, new_username, invalidate_session_cache=False,
                ),
            )
        await _drain_owner_provider_flows(request, old_username)
        # Freeze the exact current claimed phase before any paired source fence.
        phase=None
        if typed_memory:
            try:
                phase_name='memory_fm_source_phase_1'
                saved=(rename_operation.get('manifest') or {}).get(phase_name)
                expected=_claimed_fm_source_phase(operation=rename_operation,claim_token=rename_claim_token,
                    database_id=rename_operation['manifest']['memory_participants']['fm']['database_id'],
                    account_id=rename_subject_id,operation_id=rename_operation_id,step='memory_fm',generation=1,
                    owner=old_username,new_owner=new_username)
                if saved is None:
                    if rename_steps.get('host_sources_fenced'): raise RuntimeError('original source phase unavailable')
                    _rename_start(phase_name,manifest=expected)
                    _rename_checkpoint(phase_name,expected)
                elif saved!=expected or (rename_steps.get(phase_name) or {}).get('receipt')!=expected:
                    raise RuntimeError('original source phase changed')
                phase=expected
            except Exception as exc:
                _rename_fail(exc)
                raise HTTPException(503,'Account memory source phase unavailable') from exc
        # The claimed account operation blocks new source publication. Fence
        # its exact old-account FM projections before changing auth identity.
        try:
            from src.openclank.chat_source_bridge import fence_account_host_sources, validate_account_source_fence_receipt, account_source_summary_step
            summary_step=account_source_summary_step(phase)
            if durable_rename and _rename_applied(summary_step):
                saved = (rename_steps[summary_step] or {}).get("receipt")
                validate_account_source_fence_receipt(saved, owner=old_username, account_id=rename_subject_id, kind='rename', operation_id=rename_operation_id, lifecycle=account_lifecycle,phase=phase)
            else:
                _rename_start(summary_step)
                source_receipt = await fence_account_host_sources(state=request.app.state, lifecycle=account_lifecycle,
                    operation_id=rename_operation_id, claim_token=rename_claim_token, owner=old_username,
                    account_id=rename_subject_id, kind="rename",phase=phase)
                validate_account_source_fence_receipt(source_receipt, owner=old_username, account_id=rename_subject_id, kind='rename', operation_id=rename_operation_id, lifecycle=account_lifecycle,phase=phase)
                _rename_checkpoint(summary_step, source_receipt)
        except Exception as exc:
            _rename_fail(exc)
            raise HTTPException(503, "Account memory source transition is unavailable") from exc
        # Do this before the first mutation. A later partial failure or rollback
        # must not leave either ownership key serving a pre-rename catalogue.
        copal_bridge = _copal_lifecycle_bridge(request)
        old_copal_owner = copal_owner_for_user(old_username)
        new_copal_owner = copal_owner_for_user(new_username)

        # Gate on auth first. Every mutation below is contingent on this
        # succeeding — doing it last meant a rejected rename (e.g. reserved
        # username) left file-backed owner fields already rewritten with no
        # way to roll them back.
        if not _rename_applied("auth_renamed"):
            _rename_start("auth_renamed")
            if durable_rename:
                account_lifecycle.set_operation_state(
                    rename_operation_id,
                    "auth_committing",
                    claim_token=rename_claim_token,
                )
            current_auth_owner = auth_manager.username_for_account_id(rename_subject_id)
            if durable_rename and current_auth_owner == new_username:
                ok = True
            elif durable_rename and current_auth_owner != old_username:
                exc = HTTPException(409, "Account identity no longer maps to rename source or target")
                _rename_fail_or_rewind(exc)
                raise exc
            else:
                try:
                    ok = auth_manager.rename_user(old_username, new_username, user)
                except Exception as exc:
                    _rename_fail_or_rewind(exc)
                    raise
            if ok:
                _rename_checkpoint("auth_renamed", True)
                if durable_rename:
                    account_lifecycle.set_operation_state(
                        rename_operation_id,
                        "committed",
                        claim_token=rename_claim_token,
                    )
                    account_lifecycle.set_operation_state(
                        rename_operation_id,
                        "converging",
                        claim_token=rename_claim_token,
                    )
        else:
            ok = True
        if not ok:
            _rename_fail_or_rewind(
                RuntimeError("authentication account rename was rejected")
            )
            raise HTTPException(400, "Cannot rename user")

        def _rollback_auth_rename() -> bool:
            if durable_rename:
                return False
            # On self-rename the admin session has already moved to the new
            # username, so the rollback must authenticate as the new user.
            rollback_user = new_username if user == old_username else user
            try:
                return bool(auth_manager.rename_user(new_username, old_username, rollback_user))
            except Exception as rollback_err:
                logger.error(
                    "Failed to roll back auth rename %s -> %s after owner migration failure: %s",
                    new_username, old_username, rollback_err,
                )
                return False

        if durable_rename and not _rename_applied("ancillary"):
            try:
                _rename_start("ancillary")
                receipt = ancillary_lifecycle.reconcile_owner_rename(
                    old_username,
                    new_username,
                    (rename_operation.get("manifest") or {})["ancillary"],
                    account_id=(rename_operation or {}).get('subject_id'),
                )
                _rename_checkpoint("ancillary", receipt)
            except Exception as exc:
                _rename_domain_error("ancillary owner state", exc)

        copal_renamed = False
        if not _rename_applied("copal"):
            try:
                _rename_start("copal")
                await _call_copal_lifecycle(
                    copal_bridge,
                    "rename_owner",
                    {
                        "old_owner": old_copal_owner,
                        "new_owner": new_copal_owner,
                        **(
                            {
                                "manifest": (
                                    rename_operation.get("manifest") or {}
                                )["copal"]
                            }
                            if durable_rename
                            else {}
                        ),
                    },
                )
                copal_renamed = copal_bridge is not None
                _rename_checkpoint("copal", True)
            except HTTPException as exc:
                _rename_fail(exc)
                _rollback_auth_rename()
                raise

        async def _rollback_copal_rename() -> None:
            if durable_rename:
                return
            if not copal_renamed:
                return
            try:
                await _call_copal_lifecycle(
                    copal_bridge,
                    "rename_owner",
                    {"old_owner": new_copal_owner, "new_owner": old_copal_owner},
                )
            except HTTPException as rollback_error:
                logger.error(
                    "Failed to roll back Copal owner rename %s -> %s: %s",
                    new_copal_owner,
                    old_copal_owner,
                    rollback_error,
                )

        memory_provider = getattr(request.app.state, "memory_provider", None)
        provider_renamed = False
        if typed_memory:
            manifest = (rename_operation.get('manifest') or {}).get('memory_participants') if durable_rename else None
            if not durable_rename or not manifest or rename_steps.get('memory'):
                _rename_fail(RuntimeError('ambiguous legacy memory checkpoint'))
                raise HTTPException(503, 'Retained memory account manifest unavailable')
            try:
                _memory_participants_current(request.app.state, manifest)
                for kind in (('native', 'fm') if joined_memory else ('fm',)):
                    step = 'memory_' + kind
                    if _rename_applied(step):
                        continue
                    started = bool(rename_steps.get(step))
                    fm_snapshot = (rename_operation.get('manifest') or {}).get(step) if started and kind == 'fm' else None
                    if kind == 'fm' and not started:
                        _, canonical = _memory_participants_current(request.app.state, manifest)
                        personal = (rename_operation.get('manifest') or {}).get('personal_rag')
                        if type(personal) is not dict or 'rag_relocation' not in personal:
                            raise RuntimeError('personal RAG relocation manifest unavailable')
                        _account_personal_rag_store(request).assert_relocation_current(old_username, new_username, personal)
                        fm_snapshot = {'source': await canonical.owner_stats(owner=old_username), 'target': await canonical.owner_stats(owner=new_username),
                            'generation': 1, 'prior_receipt_id': None, 'prior_receipt_hash': None,
                            'rag_relocation': personal['rag_relocation']}
                        if _inventory_present(fm_snapshot['target']): raise RuntimeError('retained memory target changed')
                    if kind == 'fm':
                        _account_personal_rag_store(request).assert_relocation_current(old_username, new_username,
                            (rename_operation.get('manifest') or {}).get('personal_rag'))
                    _rename_start(step, manifest=fm_snapshot if kind == 'fm' and not started else None)
                    receipt = await _move_memory_participant(request.app.state, manifest, kind, old_username, new_username,
                        lifecycle=account_lifecycle, operation_id=rename_operation_id, claim_token=rename_claim_token, started=started, fm_snapshot=fm_snapshot, step=step)
                    _rename_checkpoint(step, receipt)
                    if kind=='fm' and not _rename_applied('memory_fm_after_verified'):
                        _rename_start('memory_fm_after_verified')
                        _, canonical = _memory_participants_current(request.app.state, manifest)
                        await _move_memory_participant(request.app.state,manifest,'fm',old_username,new_username,
                            lifecycle=account_lifecycle,operation_id=rename_operation_id,claim_token=rename_claim_token,
                            started=True,fm_snapshot=fm_snapshot,step=step,cleanup_receipt=receipt)
                        verified=await _verify_fm_move_after(canonical,old_username,new_username,receipt)
                        _rename_checkpoint('memory_fm_after_verified',verified)
                provider_renamed = True
            except Exception as exc:
                _rename_fail(exc)
                raise HTTPException(503, 'Retained memory owner transition unavailable') from exc
        elif memory_provider and not _rename_applied("memory"):
            try:
                _rename_start("memory")
                if durable_rename:
                    source_stats = await memory_provider.owner_stats(owner=old_username)
                    target_stats = await memory_provider.owner_stats(owner=new_username)
                    source_present = _inventory_present(source_stats)
                    target_present = _inventory_present(target_stats)
                    if source_present and target_present:
                        raise RuntimeError("source and target memory both contain durable state")
                    if source_present:
                        await _memory_owner_transition(memory_provider, action='rename', owner=old_username, new_owner=new_username, lifecycle=account_lifecycle, operation_id=rename_operation_id, claim_token=rename_claim_token, state=request.app.state)
                else:
                    await _memory_owner_transition(memory_provider, action='rename', owner=old_username, new_owner=new_username, lifecycle=account_lifecycle, operation_id=rename_operation_id, claim_token=rename_claim_token, state=request.app.state)
                provider_renamed = True
                _rename_checkpoint("memory", True)
            except Exception as e:
                logger.error(
                    "Failed to rename active memory owner %s -> %s: %s",
                    old_username, new_username, e,
                )
                await _rollback_copal_rename()
                _rollback_auth_rename()
                _rename_fail(e)
                raise HTTPException(500, "Failed to rename user memory")

        if durable_rename and memory_provider:
            media_store, batch_store = _account_memory_domain_stores(_account_domain_provider(request.app.state, memory_provider, joined_memory))
            if not _rename_applied("memory_media"):
                try:
                    _rename_start("memory_media")
                    receipt = media_store.rename_owner(old_username, new_username)
                    _rename_checkpoint("memory_media", receipt)
                except Exception as exc:
                    _rename_domain_error("Memory media", exc)
            if not _rename_applied("import_staging"):
                try:
                    if typed_memory:
                        frozen=(rename_operation.get('manifest') or {}).get('import_staging')
                        if (type(frozen) is not dict or set(frozen)!={'source','target'}
                            or any(type(frozen.get(k)) is not dict or set(frozen[k])!={'count','bytes','fingerprint'} for k in ('source','target'))
                            or frozen['target'].get('count')!=0 or frozen['target'].get('bytes')!=0):
                            raise RuntimeError('original import staging manifest unavailable')
                        source,target=frozen['source'],frozen['target']
                    else:
                        source = batch_store.preview_owner_staging(old_username,guarded=typed_memory)
                        target = batch_store.preview_owner_staging(new_username,guarded=typed_memory)
                    _rename_start("import_staging")
                    receipt = batch_store.rename_owner_staging(
                        old_username,
                        new_username,
                        expected_source=source,
                        expected_target=target,
                        guarded=typed_memory,
                    )
                    _rename_checkpoint("import_staging", receipt)
                except Exception as exc:
                    _rename_domain_error("Memory import staging", exc)

        if durable_rename and not _rename_applied("skills"):
            try:
                _rename_start("skills")
                receipt = _transition_account_skills(
                    request,
                    old_username,
                    new_username,
                    (rename_operation.get("manifest") or {})["skills"],
                )
                _rename_checkpoint("skills", receipt)
            except Exception as exc:
                _rename_domain_error("skills", exc)

        if durable_rename and not _rename_applied("shell_audit"):
            try:
                _rename_start("shell_audit")
                receipt = _account_shell_audit_store().reconcile_owner_rename(
                    old_username,
                    new_username,
                    (rename_operation.get("manifest") or {})["shell_audit"],
                )
                _rename_checkpoint("shell_audit", receipt)
            except Exception as exc:
                _rename_domain_error("shell audit", exc)

        if durable_rename and not _rename_applied("file_domains"):
            try:
                file_store = _account_file_domain_store()
                file_manifest = (
                    (rename_operation.get("manifest") or {}).get("file_domains")
                    or file_store.preview_rename(old_username, new_username)
                )
                _rename_start("file_domains", manifest=file_manifest)
                receipt = file_store.reconcile_rename(
                    old_username,
                    new_username,
                    file_manifest,
                )
                _rename_checkpoint("file_domains", receipt)
                for covered in (
                    "preferences",
                    "completed_research",
                    "legacy_memory",
                ):
                    if not _rename_applied(covered):
                        _rename_checkpoint(covered, {"covered_by": "file_domains"})
            except Exception as exc:
                _rename_domain_error("file-backed account state", exc)

        if durable_rename and not _rename_applied("common_sql"):
            try:
                sql_store = _account_sql_domain_store()
                sql_manifest = (
                    (rename_operation.get("manifest") or {}).get("common_sql")
                    or sql_store.owner_inventory(old_username, account_id=rename_subject_id)
                )
                _rename_start("common_sql", manifest=sql_manifest)
                receipt = sql_store.reconcile_owner(
                    old_username,
                    new_username,
                    expected_source=sql_manifest,
                    account_id=rename_subject_id,
                )
                _rename_checkpoint("common_sql", receipt.as_dict())
            except Exception as exc:
                _rename_domain_error("common SQL", exc)

        # Usernames are ownership keys for user data. Rename the common
        # owner-scoped DB rows so the account keeps access to its sessions,
        # docs, email accounts, tasks, etc.
        try:
            if _rename_applied("common_sql"):
                raise _RenameDomainApplied()
            _rename_start("common_sql")
            from sqlalchemy import func
            from core.database import Base, SessionLocal
            db = SessionLocal()
            try:
                from core.database import ScheduledTask
                if db.query(ScheduledTask.id).filter(func.lower(ScheduledTask.owner).in_((old_username, new_username)), ScheduledTask.host_account_id.isnot(None)).first():
                    raise RuntimeError('bound task requires account-scoped SQL lifecycle')
                _drop_copal_calendar_projections_for_owner(db, old_username)
                for mapper in Base.registry.mappers:
                    model = mapper.class_
                    if not hasattr(model, "owner"):
                        continue
                    (
                        db.query(model)
                        .filter(func.lower(model.owner) == old_username)
                        .update({"owner": new_username}, synchronize_session=False)
                    )
                from core.database import MimoProjectionState, ModelShareSubscription

                # This normalized projection table predates the common
                # ``owner`` column contract and keys its tenant as
                # ``owner_id``.  Keep it explicit: a generic owner_id sweep
                # would risk rewriting future immutable subject identifiers.
                (
                    db.query(MimoProjectionState)
                    .filter(func.lower(MimoProjectionState.owner_id) == old_username)
                    .update(
                        {"owner_id": new_username},
                        synchronize_session=False,
                    )
                )

                (
                    db.query(ModelShareSubscription)
                    .filter(func.lower(ModelShareSubscription.subscriber) == old_username)
                    .update(
                        {"subscriber": new_username},
                        synchronize_session=False,
                    )
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
            _rename_checkpoint("common_sql", True)
        except _RenameDomainApplied:
            pass
        except Exception as e:
            logger.error("Failed to rename owner references %s -> %s: %s", old_username, new_username, e)
            if typed_memory:
                # Auth and staged stores remain under the durable claim;
                # recovery resumes the same frozen operation instead of rolling
                # back only a subset of the completed domains.
                _rename_fail(e)
                raise HTTPException(500, "Account rename remains pending recovery") from e
            if provider_renamed:
                try:
                    await _memory_owner_transition(memory_provider, action='rename', owner=new_username, new_owner=old_username, lifecycle=account_lifecycle, operation_id=rename_operation_id, claim_token=rename_claim_token, state=request.app.state)
                except Exception as rollback_error:
                    logger.error("Failed to roll back memory owner rename: %s", rollback_error)
            await _rollback_copal_rename()
            if not _rollback_auth_rename():
                logger.error(
                    "Auth rename %s -> %s could not be rolled back after owner migration failure",
                    old_username, new_username,
                )
            _rename_fail(e)
            raise HTTPException(500, "Failed to rename user data")

        # Per-user prefs are JSON-backed, not SQL-backed.
        try:
            if _rename_applied("preferences"):
                raise _RenameDomainApplied()
            _rename_start("preferences")
            from routes.prefs_routes import _load as _load_prefs, _save as _save_prefs
            with PREFERENCE_LOCK:
                prefs = _load_prefs()
                users = prefs.get("_users") if isinstance(prefs, dict) else None
                if isinstance(users, dict):
                    prefs_key = next(
                        (k for k in users if str(k).strip().lower() == old_username),
                        None,
                    )
                    new_taken = any(str(k).strip().lower() == new_username for k in users)
                    if prefs_key is not None and not new_taken:
                        users[new_username] = users.pop(prefs_key)
                        _save_prefs(prefs)
            _rename_checkpoint("preferences", True)
        except _RenameDomainApplied:
            pass
        except Exception as e:
            _rename_domain_error("preferences", e)
            logger.warning("Failed to rename user prefs %s -> %s: %s", old_username, new_username, e)

        # In-flight deep-research tasks live in the process-local
        # ResearchHandler registry. They are not covered by the persisted JSON
        # migration above, but the research routes filter and cancel by this
        # owner field while the job is running. Do this before sweeping
        # completed JSON files so a job that finishes during the rename saves
        # with the new owner or is caught by the disk sweep below.
        try:
            if _rename_applied("active_research"):
                raise _RenameDomainApplied()
            _rename_start("active_research")
            rh = getattr(request.app.state, "research_handler", None)
            reconcile_research = getattr(rh, "reconcile_owner_rename", None)
            if durable_rename and callable(reconcile_research):
                receipt = reconcile_research(
                    old_username,
                    new_username,
                    (rename_operation.get("manifest") or {})["active_research"],
                )
            else:
                rename_owner = getattr(rh, "rename_owner", None)
                receipt = (
                    rename_owner(old_username, new_username)
                    if callable(rename_owner)
                    else {"available": False}
                )
            _rename_checkpoint("active_research", receipt)
        except _RenameDomainApplied:
            pass
        except Exception as e:
            _rename_domain_error("active research", e)
            logger.warning("Failed to rename active research tasks %s -> %s: %s", old_username, new_username, e)

        # deep_research: each completed report is a standalone JSON file with
        # an `owner` field. research_routes filters by d.get("owner") == user,
        # so a stale owner makes every report invisible to the renamed user.
        try:
            if _rename_applied("completed_research"):
                raise _RenameDomainApplied()
            _rename_start("completed_research")
            dr_dir = Path(DEEP_RESEARCH_DIR)
            if dr_dir.is_dir():
                for p in dr_dir.glob("*.json"):
                    try:
                        d = json.loads(p.read_text(encoding="utf-8"))
                        if str(d.get("owner", "")).strip().lower() == old_username:
                            d["owner"] = new_username
                            atomic_write_json(str(p), d)
                    except Exception as err:
                        if durable_rename:
                            raise
                        logger.warning("Failed to update research owner in %s: %s", p.name, err)
            _rename_checkpoint("completed_research", True)
        except _RenameDomainApplied:
            pass
        except Exception as e:
            _rename_domain_error("completed research", e)
            logger.warning("Failed to rename research owner references %s -> %s: %s", old_username, new_username, e)

        # memory.json: a flat JSON array where each entry carries an `owner`
        # field. memory_manager.load(owner=user) filters on it, so stale
        # entries disappear from the memory panel.
        try:
            if _rename_applied("legacy_memory"):
                raise _RenameDomainApplied()
            _rename_start("legacy_memory")
            if os.path.isfile(MEMORY_FILE):
                with open(MEMORY_FILE, encoding="utf-8") as fh:
                    entries = json.loads(fh.read())
                if isinstance(entries, list):
                    changed = False
                    for entry in entries:
                        if isinstance(entry, dict) and str(entry.get("owner", "")).strip().lower() == old_username:
                            entry["owner"] = new_username
                            changed = True
                    if changed:
                        atomic_write_json(MEMORY_FILE, entries)
            _rename_checkpoint("legacy_memory", True)
        except _RenameDomainApplied:
            pass
        except Exception as e:
            _rename_domain_error("legacy memory", e)
            logger.warning("Failed to rename memory.json owner references %s -> %s: %s", old_username, new_username, e)

        # uploads.json: upload rows use owner metadata for access checks and
        # owner-prefixed index keys for dedupe. Rename both so attachments keep
        # resolving after the account username changes.
        try:
            if _rename_applied("uploads"):
                raise _RenameDomainApplied()
            _rename_start("uploads")
            upload_handler = getattr(request.app.state, "upload_handler", None)
            if durable_rename and upload_handler is not None:
                upload_manifest = (
                    (rename_operation.get("manifest") or {}).get("uploads")
                    or upload_handler.preview_owner_rename(old_username, new_username)
                )
                # Replace the applying marker with the frozen manifest before
                # the atomic metadata effect.
                _rename_start("uploads", manifest=upload_manifest)
                receipt = upload_handler.reconcile_owner_rename(
                    old_username,
                    new_username,
                    upload_manifest,
                )
                _rename_checkpoint("uploads", receipt)
            else:
                rename_owner = getattr(upload_handler, "rename_owner", None)
                if callable(rename_owner):
                    rename_owner(old_username, new_username)
                _rename_checkpoint("uploads", True)
        except _RenameDomainApplied:
            pass
        except Exception as e:
            _rename_domain_error("uploads", e)
            logger.warning("Failed to rename upload owner references %s -> %s: %s", old_username, new_username, e)

        # direct personal RAG uploads live in per-owner directories and the
        # vector metadata also carries the username used for owner-filtered
        # search. Keep both in sync with the auth rename.
        try:
            if _rename_applied("personal_rag"):
                raise _RenameDomainApplied()
            _rename_start("personal_rag")
            if durable_rename:
                personal_store = _account_personal_rag_store(request)
                personal_manifest = (
                    (rename_operation.get("manifest") or {}).get("personal_rag")
                    or personal_store.preview_owner_rename(old_username, new_username)
                )
                _rename_start("personal_rag", manifest=personal_manifest)
                personal_store.assert_relocation_current(old_username, new_username, personal_manifest)
                personal_proof = await _personal_native_relocation_proof(request.app.state, lifecycle=account_lifecycle,
                    operation_id=rename_operation_id, claim_token=rename_claim_token, step='memory_fm',
                    personal_manifest=personal_manifest, source=old_username, target=new_username) if typed_memory else None
                receipt = personal_store.reconcile_owner_rename(
                    old_username,
                    new_username,
                    personal_manifest,
                    native_relocation_proof=personal_proof,
                )
                _rename_checkpoint("personal_rag", receipt)
            else:
                from routes.personal_routes import rename_personal_upload_owner
                personal_docs_manager = getattr(request.app.state, "personal_docs_manager", None)
                if personal_docs_manager is not None:
                    rag_manager = getattr(personal_docs_manager, "rag_manager", None)
                    rename_personal_upload_owner(
                        old_username,
                        new_username,
                        personal_docs_manager=personal_docs_manager,
                        rag_manager=rag_manager,
                    )
                _rename_checkpoint("personal_rag", True)
        except _RenameDomainApplied:
            pass
        except Exception as e:
            _rename_domain_error("personal RAG", e)
            logger.warning("Failed to rename personal RAG upload owner references %s -> %s: %s", old_username, new_username, e)

        # skills: SKILL.md frontmatter carries owner: <username>; the usage
        # sidecar (_usage.json) keys entries as owner::skill-name. Both must
        # be updated or the renamed user's Skills panel goes empty.
        try:
            if _rename_applied("skills"):
                raise _RenameDomainApplied()
            _rename_start("skills")
            skills_root = Path(SKILLS_DIR)
            if skills_root.is_dir():
                _owner_re = re.compile(
                    r'(?m)^(owner:\s*)' + re.escape(old_username) + r'\s*$',
                    re.IGNORECASE,
                )
                for p in skills_root.rglob("SKILL.md"):
                    try:
                        text = p.read_text(encoding="utf-8")
                        new_text = _owner_re.sub(r'\g<1>' + new_username, text)
                        if new_text != text:
                            atomic_write_text(str(p), new_text)
                    except Exception as err:
                        if durable_rename:
                            raise
                        logger.warning("Failed to update skill owner in %s: %s", p, err)
                usage_path = skills_root / "_usage.json"
                if usage_path.is_file():
                    try:
                        usage = json.loads(usage_path.read_text(encoding="utf-8"))
                        if isinstance(usage, dict):
                            new_usage = {}
                            changed = False
                            for k, v in usage.items():
                                owner_part, sep, skill_part = k.partition("::")
                                if sep and owner_part.lower() == old_username:
                                    new_usage[new_username + "::" + skill_part] = v
                                    changed = True
                                else:
                                    new_usage[k] = v
                            if changed:
                                atomic_write_json(str(usage_path), new_usage)
                    except Exception as err:
                        if durable_rename:
                            raise
                        logger.warning("Failed to update skills usage keys %s -> %s: %s", old_username, new_username, err)
            _rename_checkpoint("skills", True)
        except _RenameDomainApplied:
            pass
        except Exception as e:
            _rename_domain_error("skills", e)
            logger.warning("Failed to rename skills owner references %s -> %s: %s", old_username, new_username, e)

        # The in-memory session cache (session_manager.sessions) stores each
        # session's owner at load time. Without this patch the renamed user's
        # sessions are invisible on the next /api/sessions call because
        # get_sessions_for_user does an exact `s.owner == username` comparison
        # against stale in-memory values.
        sm = getattr(request.app.state, "session_manager", None)
        scheduler = getattr(request.app.state, "task_scheduler", None)
        if durable_rename and scheduler is not None and not _rename_applied("task_runtime"):
            _rename_start("task_runtime")
            manifest = (rename_operation.get("manifest") or {}).get("task_runtime")
            if not manifest or manifest.get("available") is False:
                manifest = scheduler.preview_owner_runtime_rename(old_username, new_username)
            receipt = scheduler.reconcile_owner_runtime(old_username, new_username, manifest)
            _rename_checkpoint("task_runtime", receipt)
        if not _rename_applied("session_cache"):
            try:
                _rename_start("session_cache")
                invalidator = getattr(sm, "invalidate_owner_cache", None)
                receipt = (
                    invalidator(old_username, new_username)
                    if callable(invalidator)
                    else {"available": False}
                )
                _rename_checkpoint("session_cache", receipt)
            except Exception as exc:
                _rename_domain_error("session cache", exc)

        mimo_supervisor = getattr(request.app.state, "mimo_supervisor", None)
        if (
            not _rename_applied("mimo")
            and mimo_supervisor is not None
            and callable(getattr(mimo_supervisor, "reconcile_owner_rename", None))
        ):
            try:
                _rename_start("mimo")
                receipt = await mimo_supervisor.reconcile_owner_rename(
                    old_username,
                    new_username,
                    (rename_operation.get("manifest") or {})["mimo"],
                )
                _rename_checkpoint("mimo", receipt)
            except Exception as exc:
                _rename_fail(exc)
                raise HTTPException(500, f"Failed to rename user Open Clank agent state: {exc}") from exc
        elif not _rename_applied("mimo"):
            _rename_checkpoint("mimo", {"available": False})

        # Filesystem Locations/visibility are account-owned state too. Keep
        # their compatibility owner references in the same lifecycle event so
        # rename does not strand access or let a later username reuse inherit
        # the old account's rows.
        try:
            if _rename_applied("filesystem_registry"):
                raise _RenameDomainApplied()
            _rename_start("filesystem_registry")
            from src.openclank.filesystem_registry import FilesystemRootRegistry
            filesystem_registry = FilesystemRootRegistry()
            if durable_rename:
                receipt = filesystem_registry.reconcile_owner_rename(
                    old_username,
                    new_username,
                    (rename_operation.get("manifest") or {})["filesystem_registry"],
                )
            else:
                filesystem_registry.rename_owner(old_username, new_username)
                receipt = True
            _rename_checkpoint("filesystem_registry", receipt)
        except _RenameDomainApplied:
            pass
        except Exception as exc:
            _rename_fail(exc)
            raise HTTPException(500, f"Failed to rename filesystem access state: {exc}") from exc

        # These stores contain one-way, owner-bound provider replay/lease
        # state. Move them after every earlier fallible owner migration so an
        # unrelated rollback cannot consume those boundaries.
        if account_lifecycle is not None and not _rename_applied("normalized_lifecycle"):
            try:
                _rename_start("normalized_lifecycle")
                if durable_rename:
                    lifecycle_receipt = account_lifecycle.reconcile_rename(old_username, new_username)
                else:
                    lifecycle_receipt = account_lifecycle.rename_owner(old_username, new_username)
                _rename_checkpoint(
                    "normalized_lifecycle",
                    dict(lifecycle_receipt.stores) if durable_rename else True,
                )
            except Exception as exc:
                _rename_fail(exc)
                raise HTTPException(
                    500,
                    f"Failed to rename normalized user state: {exc}",
                ) from exc

        # The owner-rename loop above updated ApiToken.owner in the DB, but the
        # bearer-token cache still maps each token to the OLD owner. Without
        # refreshing it, the renamed user's API tokens resolve to the old (now
        # non-existent) owner and stop reaching their data until the cache next
        # goes dirty. Invalidate it now, like the token CRUD routes do.
        invalidator = getattr(request.app.state, "invalidate_token_cache", None)
        if not _rename_applied("token_cache"):
            _rename_start("token_cache")
            if callable(invalidator):
                invalidator()
            _rename_checkpoint("token_cache", True)
        if durable_rename and not _rename_applied("convergence_verified"):
            try:
                manifests = dict(rename_operation.get("manifest") or {})
                sql_verify = _account_sql_domain_store()
                sql_verify.verify_staged(
                    old_username,
                    new_username,
                    expected_source=manifests["common_sql"],
                    account_id=rename_subject_id,
                )
                file_verify = _account_file_domain_store()
                file_verify.verify(
                    old_username,
                    new_username,
                    manifests["file_domains"],
                    expected="staged",
                )
                if any(
                    int(value or 0)
                    for value in _account_skills_domain_store(request)
                    .preview_owner_purge(old_username)["counts"]
                    .values()
                ):
                    raise RuntimeError("skill source owner remains after rename")
                if _account_shell_audit_store().owner_inventory(old_username)[
                    "count"
                ]:
                    raise RuntimeError("shell audit source owner remains after rename")
                ancillary_verify = ancillary_lifecycle
                if ancillary_verify.owner_inventory(old_username,account_id=(rename_operation or {}).get('subject_id'))["count"]:
                    raise RuntimeError("ancillary source owner remains after rename")
                if account_lifecycle.owner_inventory(old_username)["count"]:
                    raise RuntimeError("normalized source owner remains after rename")
                if scheduler is not None and scheduler.owner_runtime_inventory(old_username)["count"]:
                    raise RuntimeError("scheduler runtime source remains after rename")
                if memory_provider and _inventory_present(
                    await memory_provider.owner_stats(owner=old_username)
                ):
                    raise RuntimeError("Memory source owner remains after rename")
                if joined_memory:
                    selected, canonical = _memory_participants_current(request.app.state, manifests['memory_participants'])
                    source_now, target_now = selected.lifecycle_snapshot(old_username), selected.lifecycle_snapshot(new_username)
                    original = manifests['memory_participants']['native']['source']
                    if (source_now['count'] or target_now['ids'] != original['ids'] or
                            target_now['content_fingerprint'] != original['content_fingerprint'] or
                            _inventory_present(await canonical.owner_stats(owner=old_username))):
                        raise RuntimeError('retained memory rename verification failed')
                upload_verify = getattr(request.app.state, "upload_handler", None)
                if upload_verify is not None and manifests.get("uploads"):
                    if upload_verify.owner_inventory(old_username)["count"]:
                        raise RuntimeError("upload source owner remains after rename")
                personal_verify = _account_personal_rag_store(request)
                if personal_verify.owner_inventory(old_username)["count"]:
                    raise RuntimeError("personal RAG source owner remains after rename")
                if memory_provider:
                    media_verify, staging_verify = _account_memory_domain_stores(
                        _account_domain_provider(request.app.state, memory_provider, joined_memory)
                    )
                    if media_verify.preview_owner_purge(old_username)["count"]:
                        raise RuntimeError("Memory media source remains after rename")
                    if staging_verify.preview_owner_staging(old_username,guarded=typed_memory)["count"]:
                        raise RuntimeError("import staging source remains after rename")
                filesystem_verify = FilesystemRootRegistry().owner_inventory(old_username)
                if filesystem_verify["count"]:
                    raise RuntimeError("filesystem registry source remains after rename")
                if callable(getattr(mimo_supervisor, "owner_lifecycle_inventory", None)):
                    if mimo_supervisor.owner_lifecycle_inventory(old_username)["count"]:
                        raise RuntimeError("Agent runtime source remains after rename")
            except Exception as exc:
                _rename_fail(exc)
                raise HTTPException(500, "Account rename verification failed") from exc
        release_research = getattr(research_handler, "release_owner_fence", None)
        if callable(release_research):
            release_research(old_username)
        _invalidate_account_runtime(
            request,
            old_username,
            new_username,
            invalidate_tokens=False,
        )
        source_summary = ((rename_steps.get(account_source_summary_step(phase)) or {}).get('receipt') or {}) if durable_rename else {}
        if durable_rename and source_summary.get('renewal') == 'pending' and not _rename_applied('host_sources_renewed'):
            try:
                from src.openclank.chat_source_bridge import renew_account_host_sources
                _rename_start('host_sources_renewed')
                renewal = await renew_account_host_sources(state=request.app.state, lifecycle=account_lifecycle,
                    operation_id=rename_operation_id, claim_token=rename_claim_token,
                    old_owner=old_username, new_owner=new_username, account_id=rename_subject_id)
                _rename_checkpoint('host_sources_renewed', renewal)
            except Exception:
                _rename_fail(RuntimeError('memory source revalidation pending'))
                await _sync_history_accounts(request)
                return {"ok":True, "username":new_username, "renamed_self":old_username == user,
                        "operation_id":rename_operation_id, "memory_source_revalidation":"pending"}
        _rename_checkpoint("convergence_verified", True, complete=True)
        await _sync_history_accounts(request)
        result = {"ok": True, "username": new_username, "renamed_self": old_username == user}
        if durable_rename:
            result["operation_id"] = rename_operation_id
            source_receipt = ((rename_steps.get(account_source_summary_step(phase)) or {}).get('receipt') or {})
            if source_receipt.get('renewal') == 'pending':
                result['memory_source_revalidation'] = 'complete' if _rename_applied('host_sources_renewed') else 'pending'
        return result

    @router.put("/users/{username}/admin")
    async def set_user_admin(username: str, body: SetAdminRequest, request: Request):
        """Promote/demote a user to/from admin. Admin only.

        The last remaining admin can't be demoted (no lockout). Self-demotion
        is allowed while another admin exists; the `self` flag tells the UI to
        reload the acting user into the normal-user view.
        """
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        result = auth_manager.set_admin(username, body.is_admin, user)
        if result is SetAdminResult.USER_NOT_FOUND:
            raise HTTPException(404, "User not found")
        if result is SetAdminResult.NOT_AUTHORIZED:
            raise HTTPException(403, "Admin only")
        if result is SetAdminResult.LAST_ADMIN:
            raise HTTPException(400, "Cannot demote the last admin")
        target = (username or "").strip().lower()
        return {
            "ok": True,
            "is_admin": body.is_admin,
            "self": target == (user or "").strip().lower(),
        }

    @router.post("/signup-toggle", deprecated=True)
    async def toggle_signup(request: Request):
        """
        Toggle open registration on/off. Admin only.

        DEPRECATED: This endpoint uses toggle semantics which can lead to unsafe state changes.
        Use PUT /open-signup instead.

        This endpoint is kept for backward compatibility and may be removed in future versions.
        """
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        auth_manager.signup_enabled = not auth_manager.signup_enabled
        return {"ok": True, "signup_enabled": auth_manager.signup_enabled}

    @router.put("/open-signup")
    async def set_signup_enabled(body: SetOpenRegistrationRequest, request: Request):
        """Set open signup enabled state. Admin only."""
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        auth_manager.signup_enabled = body.enabled
        return {"ok": True,"signup_enabled": auth_manager.signup_enabled}

    @router.delete("/users")
    @_guard_account_claim
    async def admin_delete_user(body: DeleteUserRequest, request: Request):
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        joined_memory = _require_memory_participant_join(request.app.state)

        def _invalidate_api_token_cache():
            try:
                invalidator = getattr(request.app.state, "invalidate_token_cache", None)
                if invalidator:
                    invalidator()
            except Exception:
                pass

        memory_provider = getattr(request.app.state, "memory_provider", None)
        target_owner = (body.username or "").strip().lower()
        if not target_owner:
            raise HTTPException(400, "Username required")
        if target_owner == str(user or "").strip().lower():
            raise HTTPException(400, "Cannot delete your own account")
        durable = account_lifecycle is not None and callable(
            getattr(account_lifecycle, "begin_delete_operation", None)
        )
        from src.memery_provider import MemeryProvider
        guarded_primary = (not joined_memory and isinstance(memory_provider,MemeryProvider)
            and getattr(memory_provider,'_guarded_v17',False) is True)
        if guarded_primary and not durable:
            raise HTTPException(503, 'Guarded memory account lifecycle is unavailable')
        typed_memory = joined_memory or (guarded_primary and durable)
        operation = None
        if durable and body.operation_id:
            try:
                operation = account_lifecycle.get_delete_operation(body.operation_id)
            except Exception as exc:
                raise HTTPException(404, "Account deletion operation not found") from exc
            if operation.get("source_owner") != target_owner:
                raise HTTPException(409, "Account deletion operation target does not match")
        known_users = getattr(auth_manager, "users", None)
        if (
            isinstance(known_users, dict)
            and target_owner not in known_users
            and operation is None
        ):
            raise HTTPException(404, "User not found")
        account_id_for = getattr(auth_manager, "account_id", lambda _username: None)
        target_subject_id = (
            operation.get("subject_id") if operation else account_id_for(target_owner)
        )
        actor_subject_id = account_id_for(user) or str(user)
        # Deleting then recreating the same username must not revive its old
        # 30-second endpoint catalogue, even if deletion later fails partway.
        tombstone_identity = target_subject_id or (
            "legacy-"
            + uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"openclank-account:{target_owner}",
            ).hex
        )
        tombstone_owner = f"deleted:{tombstone_identity}"
        if operation is not None and operation.get("tombstone_owner") != tombstone_owner:
            raise HTTPException(409, "Account deletion operation identity does not match")
        if operation is None:
            research_handler = getattr(request.app.state, "research_handler", None)
            active_research = getattr(research_handler, "_active_tasks", {})
            if not callable(getattr(research_handler, "fence_owner", None)) and any(
                isinstance(entry, dict)
                and str(entry.get("owner") or "").strip().lower() == target_owner
                for entry in getattr(active_research, "values", lambda: ())()
            ):
                raise HTTPException(
                    409,
                    "Finish or cancel the user's active research before deleting the account",
                )
        if durable and operation is None:
            operation = account_lifecycle.begin_delete_operation(
                actor=actor_subject_id,
                source_owner=target_owner,
                subject_id=tombstone_identity,
                tombstone_owner=tombstone_owner,
            )
        operation_id = str((operation or {}).get("operation_id") or "")
        claim_token = ""
        steps = dict((operation or {}).get("steps") or {})
        if durable:
            try:
                operation = account_lifecycle.claim_delete_operation(
                    operation_id,
                    force_foreign_takeover=bool(body.force_foreign_takeover),
                )
                claim_token = str(
                    ((operation.get("receipt") or {}).get("claim") or {}).get("token")
                    or ""
                )
                _register_account_claim_cleanup(
                    account_lifecycle, operation_id, claim_token, request,
                )
                if operation.get("state") in {"prepared", "partial"}:
                    operation = account_lifecycle.set_operation_state(
                        operation_id,
                        "staging",
                        claim_token=claim_token,
                    )
                steps = dict(operation.get("steps") or {})
            except Exception as exc:
                raise HTTPException(409, str(exc)) from exc

        def _checkpoint(step: str, receipt=True, *, complete: bool = False):
            nonlocal operation, steps
            if not durable:
                return
            operation = account_lifecycle.checkpoint_delete_operation(
                operation_id,
                step,
                receipt,
                complete=complete,
                claim_token=claim_token,
            )
            steps = dict(operation.get("steps") or {})

        def _start_step(step: str, *, manifest=None):
            nonlocal operation, steps
            if not durable:
                return
            operation = account_lifecycle.start_operation_step(
                operation_id,
                step,
                manifest=manifest,
                claim_token=claim_token,
            )
            steps = dict(operation.get("steps") or {})

        def _applied(step: str) -> bool:
            value = steps.get(step)
            return value is True or (
                isinstance(value, dict) and value.get("state") == "applied"
            )

        def _fail_operation(exc: BaseException):
            if durable:
                account_lifecycle.fail_delete_operation(
                    operation_id,
                    exc,
                    claim_token=claim_token,
                )

        def _has_inventory(payload) -> bool:
            if isinstance(payload, dict):
                for key, value in payload.items():
                    if key in {"owner", "workspace_id", "fingerprint"}:
                        continue
                    if _has_inventory(value):
                        return True
                return False
            if isinstance(payload, (list, tuple, set)):
                return any(_has_inventory(value) for value in payload)
            if isinstance(payload, bool):
                return payload
            if isinstance(payload, (int, float)):
                return payload != 0
            return False

        if durable:
            drain_owner_requests = getattr(
                request.app.state,
                "drain_account_owner_requests",
                None,
            )
            if not callable(drain_owner_requests):
                exc = RuntimeError("account request drain is unavailable")
                if steps:
                    _fail_operation(exc)
                else:
                    account_lifecycle.abort_operation(
                        operation_id,
                        exc,
                        claim_token=claim_token,
                    )
                raise HTTPException(
                    503,
                    "Account deletion cannot quiesce active requests",
                ) from exc
            try:
                # Repeat the bounded wait on recovery; timeout is not proof of
                # quiescence and must precede destructive inventory changes.
                await asyncio.wait_for(drain_owner_requests(target_owner), timeout=_ACCOUNT_REQUEST_DRAIN_SECONDS)
            except Exception as exc:
                if steps:
                    _fail_operation(exc)
                else:
                    account_lifecycle.abort_operation(
                        operation_id,
                        exc,
                        claim_token=claim_token,
                    )
                raise HTTPException(
                    503,
                    "Account deletion could not drain active requests",
                ) from exc
            quiesce_owner_writers = getattr(
                request.app.state,
                "quiesce_account_owner_writers",
                None,
            )
            if not callable(quiesce_owner_writers):
                exc = RuntimeError("account owner writer drain is unavailable")
                if steps:
                    _fail_operation(exc)
                else:
                    account_lifecycle.abort_operation(
                        operation_id,
                        exc,
                        claim_token=claim_token,
                    )
                raise HTTPException(
                    503,
                    "Account deletion cannot quiesce background writers",
                ) from exc
            try:
                await quiesce_owner_writers(target_owner, subject_id=(operation or {}).get('subject_id'))
            except Exception as exc:
                if steps:
                    _fail_operation(exc)
                else:
                    account_lifecycle.abort_operation(
                        operation_id,
                        exc,
                        claim_token=claim_token,
                    )
                raise HTTPException(
                    503,
                    "Account deletion could not quiesce background writers",
                ) from exc
            try:
                _mimo_lifecycle_supervisor(request)
                _copal_lifecycle_bridge(request)
            except Exception as exc:
                if steps:
                    _fail_operation(exc)
                else:
                    account_lifecycle.abort_operation(
                        operation_id,
                        exc,
                        claim_token=claim_token,
                    )
                if isinstance(exc, HTTPException):
                    raise
                raise HTTPException(
                    503,
                    "Account deletion lifecycle authority is unavailable",
                ) from exc

        if durable and not (operation.get("manifest") or {}).get("preflight_version"):
            try:
                normalized_source = account_lifecycle.owner_inventory(target_owner)
                normalized_target = account_lifecycle.owner_inventory(tombstone_owner)
                media_artifacts_preflight = account_lifecycle.media_artifact_inventory(operation['subject_id'])
                if normalized_source["count"] and normalized_target["count"]:
                    raise RuntimeError("normalized tombstone already contains state")
                sql_preflight = _account_sql_domain_store()
                sql_source = sql_preflight.owner_inventory(target_owner, account_id=target_subject_id)
                sql_target = sql_preflight.owner_inventory(tombstone_owner, account_id=target_subject_id)
                if sql_source["count"] and sql_target["count"]:
                    raise RuntimeError("SQL tombstone already contains state")
                file_preflight = _account_file_domain_store().preview_rename(
                    target_owner,
                    tombstone_owner,
                )
                skills_preflight = _account_skills_domain_store(
                    request
                ).preview_owner_rename(target_owner, tombstone_owner)
                shell_audit_preflight = _account_shell_audit_store().preview_owner_rename(
                    target_owner,
                    tombstone_owner,
                )
                ancillary_preflight = ancillary_lifecycle.preview_owner_rename(
                    target_owner,
                    tombstone_owner,
                    account_id=(operation or {}).get('subject_id'),
                )
                upload_preflight_store = getattr(request.app.state, "upload_handler", None)
                uploads_preflight = (
                    upload_preflight_store.preview_owner_rename(
                        target_owner,
                        tombstone_owner,
                    )
                    if upload_preflight_store is not None
                    else None
                )
                personal_preflight = _account_personal_rag_store(
                    request
                ).preview_owner_rename(target_owner, tombstone_owner)
                from src.openclank.filesystem_registry import FilesystemRootRegistry
                filesystem_preflight = FilesystemRootRegistry().preview_owner_rename(
                    target_owner,
                    tombstone_owner,
                )
                research_preflight_store = getattr(
                    request.app.state,
                    "research_handler",
                    None,
                )
                research_preview = getattr(
                    research_preflight_store,
                    "preview_owner_rename",
                    None,
                )
                research_preflight = (
                    research_preview(target_owner, tombstone_owner)
                    if callable(research_preview)
                    else {"available": False}
                )
                mimo_preflight_store = getattr(
                    request.app.state,
                    "mimo_supervisor",
                    None,
                )
                deletion_preview = getattr(mimo_preflight_store, "preview_owner_deletion", None)
                if callable(deletion_preview):
                    mimo_preflight = await deletion_preview(target_owner, tombstone_owner, tombstone_identity)
                else:
                    mimo_preflight = (
                        await mimo_preflight_store.preview_owner_rename(target_owner, tombstone_owner)
                        if callable(getattr(mimo_preflight_store, "preview_owner_rename", None))
                        else {"available": False}
                    )
                memory_source = memory_target = None
                media_preflight = staging_preflight = None
                if memory_provider is not None:
                    memory_source = await memory_provider.owner_stats(owner=target_owner)
                    memory_target = await memory_provider.owner_stats(owner=tombstone_owner)
                    if _inventory_present(memory_source) and _inventory_present(memory_target):
                        raise RuntimeError("Memory tombstone already contains state")
                    media_preflight_store, staging_preflight_store = (
                        _account_memory_domain_stores(_account_domain_provider(request.app.state, memory_provider, joined_memory))
                    )
                    media_preflight = {
                        "source": media_preflight_store.preview_owner_purge(target_owner),
                        "target": media_preflight_store.preview_owner_purge(tombstone_owner),
                    }
                    staging_preflight = {
                        "source": staging_preflight_store.preview_owner_staging(target_owner,guarded=typed_memory),
                        "target": staging_preflight_store.preview_owner_staging(tombstone_owner,guarded=typed_memory),
                    }
                    if media_preflight["source"]["count"] and media_preflight["target"]["count"]:
                        raise RuntimeError("Memory media tombstone already contains state")
                    if staging_preflight["source"]["count"] and staging_preflight["target"]["count"]:
                        raise RuntimeError("import staging tombstone already contains state")
                copal_preflight = await _call_copal_lifecycle(
                    _copal_lifecycle_bridge(request),
                    "preflight_rename_owner",
                    {
                        "old_owner": copal_owner_for_user(target_owner),
                        "new_owner": copal_owner_for_user(tombstone_owner),
                    },
                )
                from src.openclank.chat_source_bridge import canonical_source_provider
                memory_participants = None
                if canonical_source_provider(request.app.state) is not None and canonical_source_provider(request.app.state) is not memory_provider:
                    memory_participants = await _freeze_memory_participants(request.app.state, target_owner, tombstone_owner,lifecycle=account_lifecycle,operation_id=operation_id,claim_token=claim_token,account_id=target_subject_id,host_owner=target_owner)
                elif guarded_primary:
                    memory_participants = await _freeze_primary_guarded_memory(request.app.state, target_owner, tombstone_owner,lifecycle=account_lifecycle,operation_id=operation_id,claim_token=claim_token,account_id=target_subject_id,host_owner=target_owner)
                operation = account_lifecycle.freeze_operation_manifest(
                    operation_id,
                    {
                        "preflight_version": 1,
                        "task_runtime": (
                            request.app.state.task_scheduler.preview_owner_runtime_rename(
                                target_owner, tombstone_owner,
                            )
                            if getattr(request.app.state, "task_scheduler", None) is not None
                            else {"available": False}
                        ),
                        "normalized": {"source": normalized_source, "target": normalized_target},
                        "media_artifacts": media_artifacts_preflight,
                        "common_sql_tombstoned": sql_source,
                        "common_sql_target": sql_target,
                        "file_domains_tombstoned": file_preflight,
                        "skills_tombstoned": skills_preflight,
                        "shell_audit_tombstoned": shell_audit_preflight,
                        "ancillary_tombstoned": ancillary_preflight,
                        "uploads_tombstoned": uploads_preflight,
                        "personal_rag_tombstoned": personal_preflight,
                        "filesystem_registry_tombstoned": filesystem_preflight,
                        "research_tombstoned": research_preflight,
                        "mimo_tombstoned": mimo_preflight,
                        "memory": {"source": memory_source, "target": memory_target},
                        **({'memory_participants': memory_participants} if memory_participants is not None else {}),
                        "memory_media": media_preflight,
                        "import_staging": staging_preflight,
                        "copal": copal_preflight or {"available": False},
                    },
                    claim_token=claim_token,
                )
                steps = dict(operation.get("steps") or {})
            except Exception as exc:
                account_lifecycle.abort_operation(
                    operation_id,
                    exc,
                    claim_token=claim_token,
                )
                raise HTTPException(409, f"Account deletion preflight failed: {exc}") from exc

        if typed_memory and (not durable or not (operation.get('manifest') or {}).get('memory_participants')
                or steps.get('memory_tombstoned') or steps.get('memory_purged')):
            exc = RuntimeError('ambiguous legacy memory checkpoint')
            if durable:
                _fail_operation(exc)
            raise HTTPException(503, 'Retained memory account manifest unavailable') from exc
        # Reconcile the last issued guarded FM original before any resumed
        # consumer can consult its old schema cookie or inventory.
        if typed_memory:
            manifest=(operation.get('manifest') or {})['memory_participants']
            fm_snapshot=(operation.get('manifest') or {}).get('memory_fm_tombstoned')
            purge_step=steps.get('memory_fm_purged') or {}
            reverse_step=None
            if type(fm_snapshot) is dict and type(fm_snapshot.get('generation')) is int:
                reverse_step='memory_fm_reverse_'+str(fm_snapshot['generation'])
            reverse_state=steps.get(reverse_step) if reverse_step else None
            forward_state=steps.get('memory_fm_tombstoned')
            try:
                if type(purge_step) is dict and purge_step.get('state') in ('applying','applied'):
                    frozen=(operation.get('manifest') or {}).get('memory_fm_purged')
                    if type(frozen) is not dict or set(frozen)!={'snapshot'}:
                        raise RuntimeError('memory purge original unavailable')
                    receipt=await _purge_memory_participant(request.app.state,manifest,'fm',tombstone_owner,frozen['snapshot'],
                        lifecycle=account_lifecycle,operation_id=operation_id,claim_token=claim_token,started=True,step='memory_fm_purged')
                    if purge_step.get('state')=='applied' and purge_step.get('receipt')!=receipt:
                        raise RuntimeError('memory purge original changed')
                    if purge_step.get('state')=='applying': _checkpoint('memory_fm_purged',receipt)
                elif type(reverse_state) is dict and reverse_state.get('state') in ('applying','applied'):
                    receipt=await _move_memory_participant(request.app.state,manifest,'fm',tombstone_owner,target_owner,
                        lifecycle=account_lifecycle,operation_id=operation_id,claim_token=claim_token,started=True,reverse=True,
                        fm_snapshot=fm_snapshot,step='memory_fm_tombstoned')
                    if reverse_state.get('state')=='applying': _checkpoint(reverse_step,receipt)
                elif type(forward_state) is dict and forward_state.get('state') in ('applying','applied'):
                    receipt=await _move_memory_participant(request.app.state,manifest,'fm',target_owner,tombstone_owner,
                        lifecycle=account_lifecycle,operation_id=operation_id,claim_token=claim_token,started=True,
                        fm_snapshot=fm_snapshot,step='memory_fm_tombstoned')
                    if forward_state.get('state')=='applied' and forward_state.get('receipt')!=receipt:
                        raise RuntimeError('memory transition original changed')
                    if forward_state.get('state')=='applying': _checkpoint('memory_fm_tombstoned',receipt)
            except Exception as exc:
                _fail_operation(exc)
                raise HTTPException(503,'Retained memory original status unavailable') from exc
        pre_auth_steps = (
            "provider_flows_drained",
            "runtime_caches_fenced",
            "research_tombstoned",
            "ancillary_tombstoned",
            "skills_tombstoned",
            "shell_audit_tombstoned",
            "filesystem_registry_tombstoned",
            "mimo_tombstoned",
            "memory_tombstoned",
            "memory_native_tombstoned",
            "memory_fm_tombstoned",
            "uploads_tombstoned",
            "personal_rag_tombstoned",
            "copal_tombstoned",
            "memory_media_tombstoned",
            "import_staging_tombstoned",
            "lifecycle_tombstoned",
            "file_domains_tombstoned",
            "preferences_detached",
            "common_sql_tombstoned",
            "access_caches_fenced",
            "auth_deleted",
        )

        async def _compensate_delete_pre_auth(exc: BaseException) -> None:
            """Restore every staged domain before releasing the request claim."""
            if not durable:
                return
            stored = account_lifecycle.get_operation(operation_id)
            stored_steps = dict(stored.get("steps") or {})
            manifests = dict(stored.get("manifest") or {})

            def started(name: str) -> bool:
                value = stored_steps.get(name)
                return isinstance(value, dict) and value.get("state") in {
                    "applying",
                    "applied",
                }

            rollback_errors: dict[str, str] = {}
            rollback_results: dict[str, object] = {}
            if typed_memory and (started('memory_native_tombstoned') or started('memory_fm_tombstoned')):
                pending = stored_steps.get('memory_compensation_pending')
                if not isinstance(pending, dict) or pending.get('state') != 'applying':
                    account_lifecycle.start_operation_step(operation_id, 'memory_compensation_pending',
                        claim_token=claim_token)

            async def attempt(name: str, action) -> None:
                try:
                    result = action()
                    if inspect.isawaitable(result):
                        result = await result
                    if name in ('memory_native_tombstoned', 'memory_fm_tombstoned', 'import_staging_tombstoned'):
                        rollback_results[name] = result
                except Exception as rollback_exc:
                    rollback_errors[name] = (
                        " ".join(str(rollback_exc).split())[:200]
                        or rollback_exc.__class__.__name__
                    )

            sql_rollback = _account_sql_domain_store()
            file_rollback = _account_file_domain_store()
            ancillary_rollback = ancillary_lifecycle
            memory_rollback = getattr(request.app.state, "memory_provider", None)
            upload_rollback = getattr(request.app.state, "upload_handler", None)
            personal_rollback = _account_personal_rag_store(request)
            research_rollback = getattr(request.app.state, "research_handler", None)
            mimo_rollback = getattr(request.app.state, "mimo_supervisor", None)
            from src.openclank.filesystem_registry import FilesystemRootRegistry
            filesystem_rollback = FilesystemRootRegistry()

            if started("ancillary_tombstoned"):
                ancillary_token = hashlib.sha256(
                    f"{operation_id}:ancillary".encode("utf-8")
                ).hexdigest()[:32]
                await attempt(
                    "ancillary_tombstoned",
                    lambda: ancillary_rollback.compensate(
                        target_owner,
                        tombstone_owner,
                        manifests["ancillary_tombstoned"],
                        operation_token=ancillary_token,
                        account_id=(operation or {}).get('subject_id'),
                    ),
                )

            if started("common_sql_tombstoned"):
                async def restore_common_sql():
                    expected = manifests.get("common_sql_default_seed_preflight", manifests["common_sql_tombstoned"])
                    try:
                        return sql_rollback.compensate_owner(target_owner, tombstone_owner,
                            expected_source=expected, account_id=target_subject_id)
                    except Exception as exc:
                        from src.openclank.sql_owner_lifecycle import SqlOwnerLifecycleConflict
                        if (not isinstance(exc, SqlOwnerLifecycleConflict)
                            or stored_steps["common_sql_tombstoned"]["state"] != "applying"
                            or started("auth_deleted") or "common_sql_default_seed_preflight" in manifests
                            or account_id_for(target_owner) != target_subject_id):
                            raise
                        stopped = sql_rollback.stopped_default_seed_inventory(target_owner, tombstone_owner,
                            account_id=target_subject_id, original=expected)
                        _start_step("common_sql_default_seed_preflight", manifest=stopped)
                        return sql_rollback.compensate_owner(target_owner, tombstone_owner,
                            expected_source=stopped, account_id=target_subject_id)
                await attempt("common_sql_tombstoned", restore_common_sql)
                if (typed_memory and started("filesystem_registry_tombstoned") and started("memory_fm_tombstoned")
                    and not started("auth_deleted") and account_id_for(target_owner) == target_subject_id
                    and filesystem_rollback.repository.subject_for_username(target_owner) == target_subject_id
                    and filesystem_rollback.repository.subject_for_username(tombstone_owner) == target_subject_id):
                    await attempt("filesystem_registry_aliases", lambda:
                        filesystem_rollback.repository.restore_duplicated_deletion_alias(
                            target_owner, tombstone_owner, target_subject_id, operation_id=operation_id,
                            claim_token=claim_token))
            if started("file_domains_tombstoned"):
                await attempt(
                    "file_domains_tombstoned",
                    lambda: file_rollback.compensate(
                        target_owner,
                        tombstone_owner,
                        manifests["file_domains_tombstoned"],
                    ),
                )
            if started("skills_tombstoned"):
                await attempt(
                    "skills_tombstoned",
                    lambda: _transition_account_skills(
                        request,
                        target_owner,
                        tombstone_owner,
                        manifests["skills_tombstoned"],
                        compensate=True,
                    ),
                )
            if started("shell_audit_tombstoned"):
                await attempt(
                    "shell_audit_tombstoned",
                    lambda: _account_shell_audit_store().compensate_owner_rename(
                        target_owner,
                        tombstone_owner,
                        manifests["shell_audit_tombstoned"],
                    ),
                )
            if started("lifecycle_tombstoned"):
                await attempt(
                    "lifecycle_tombstoned",
                    lambda: account_lifecycle.reconcile_rename(
                        tombstone_owner,
                        target_owner,
                    ),
                )
            if started("import_staging_tombstoned") and memory_rollback is not None:
                _media, staging_rollback = _account_memory_domain_stores(_account_domain_provider(request.app.state, memory_rollback, joined_memory))

                def restore_staging():
                    source = staging_rollback.preview_owner_staging(tombstone_owner,guarded=typed_memory)
                    target = staging_rollback.preview_owner_staging(target_owner,guarded=typed_memory)
                    return staging_rollback.rename_owner_staging(
                        tombstone_owner,
                        target_owner,
                        expected_source=source,
                        expected_target=target,
                        guarded=typed_memory,
                    )

                await attempt("import_staging_tombstoned", restore_staging)
            if started("memory_media_tombstoned") and memory_rollback is not None:
                media_rollback, _staging = _account_memory_domain_stores(_account_domain_provider(request.app.state, memory_rollback, joined_memory))
                await attempt(
                    "memory_media_tombstoned",
                    lambda: media_rollback.rename_owner(tombstone_owner, target_owner),
                )
            if started("copal_tombstoned"):
                await attempt(
                    "copal_tombstoned",
                    lambda: _call_copal_lifecycle(
                        _copal_lifecycle_bridge(request),
                        "compensate_owner_rename",
                        {
                            "old_owner": copal_owner_for_user(target_owner),
                            "new_owner": copal_owner_for_user(tombstone_owner),
                            "manifest": manifests["copal"],
                        },
                    ),
                )
            if started("uploads_tombstoned") and upload_rollback is not None:
                await attempt(
                    "uploads_tombstoned",
                    lambda: upload_rollback.compensate_owner_rename(
                        target_owner,
                        tombstone_owner,
                        manifests["uploads_tombstoned"],
                    ),
                )
            if typed_memory:
                async def restore_fm():
                    snapshot = manifests.get('memory_fm_tombstoned')
                    if type(snapshot) is not dict or type(snapshot.get('generation')) is not int:
                        raise RuntimeError('retained memory forward snapshot unavailable')
                    generation = snapshot['generation']
                    relocation = snapshot.get('rag_relocation')
                    inverse = {'from': relocation['to'], 'to': relocation['from']} if relocation is not None else None
                    personal_rollback.assert_relocation_current(tombstone_owner, target_owner, {'rag_relocation': inverse})
                    forward_step = f'memory_fm_forward_{generation}'
                    forward = (stored_steps.get(forward_step) or {}).get('receipt')
                    if forward is None:
                        forward = await _move_memory_participant(request.app.state, manifests['memory_participants'],
                            'fm', target_owner, tombstone_owner, lifecycle=account_lifecycle,
                            operation_id=operation_id, claim_token=claim_token, started=True,
                            fm_snapshot=snapshot, step='memory_fm_tombstoned')
                        _checkpoint(forward_step, forward)
                    verified_step=f'memory_fm_forward_verified_{generation}'
                    if not _applied(verified_step):
                        _start_step(verified_step)
                        _, canonical = _memory_participants_current(request.app.state, manifests['memory_participants'])
                        await _move_memory_participant(request.app.state,manifests['memory_participants'],'fm',target_owner,tombstone_owner,
                            lifecycle=account_lifecycle,operation_id=operation_id,claim_token=claim_token,started=True,
                            fm_snapshot=snapshot,step='memory_fm_tombstoned',cleanup_receipt=forward)
                        verified=await _verify_fm_move_after(canonical,target_owner,tombstone_owner,forward)
                        _checkpoint(verified_step,verified)
                    result = forward.get('result') if type(forward) is dict else None
                    if type(result) is not dict or forward.get('state') != 'applied':
                        raise RuntimeError('retained memory forward receipt unavailable')
                    reverse_step = f'memory_fm_reverse_{generation}'
                    previous = stored_steps.get(reverse_step) or {}
                    reverse_snapshot = manifests.get(reverse_step)
                    _, canonical = _memory_participants_current(request.app.state, manifests['memory_participants'],
                        status_only=previous.get('state') in ('applying','applied'))
                    from src.openclank.chat_source_bridge import (fence_account_host_sources,
                        validate_account_source_fence_receipt, account_source_summary_step)
                    current_operation=account_lifecycle.get_operation(operation_id)
                    reverse_phase=_claimed_fm_source_phase(operation=current_operation,claim_token=claim_token,
                        database_id=manifests['memory_participants']['fm']['database_id'],account_id=stored['subject_id'],
                        operation_id=operation_id,step='memory_fm_tombstoned',generation=generation,action='reverse',
                        owner=tombstone_owner,new_owner=target_owner)
                    phase_step='memory_fm_source_phase_'+str(generation)+'_reverse'
                    frozen=(current_operation.get('manifest') or {}).get(phase_step)
                    if frozen is None:
                        if previous.get('state') in ('applying','applied'):
                            raise RuntimeError('original reverse source phase unavailable')
                        current_source = await canonical.owner_stats(owner=tombstone_owner)
                        current_target = await canonical.owner_stats(owner=target_owner)
                        if (current_source.get('fingerprint') != result['target_after_fingerprint']
                                or current_target.get('fingerprint') != result['source_after_fingerprint']):
                            raise RuntimeError('retained memory compensation state changed')
                        _start_step(phase_step,manifest=reverse_phase); _checkpoint(phase_step,reverse_phase)
                    elif frozen!=reverse_phase or ((current_operation.get('steps') or {}).get(phase_step) or {}).get('receipt')!=reverse_phase:
                        raise RuntimeError('retained reverse source phase changed')
                    summary_step=account_source_summary_step(reverse_phase)
                    if _applied(summary_step):
                        source_receipt=(steps[summary_step] or {}).get('receipt')
                    else:
                        if previous.get('state') in ('applying','applied'):
                            raise RuntimeError('original reverse source summary unavailable')
                        _start_step(summary_step)
                        source_receipt=await fence_account_host_sources(state=request.app.state,lifecycle=account_lifecycle,
                            operation_id=operation_id,claim_token=claim_token,owner=target_owner,
                            account_id=stored['subject_id'],kind='delete',phase=reverse_phase)
                        validate_account_source_fence_receipt(source_receipt,owner=target_owner,account_id=stored['subject_id'],
                            kind='delete',operation_id=operation_id,lifecycle=account_lifecycle,phase=reverse_phase)
                        _checkpoint(summary_step,source_receipt)
                    validate_account_source_fence_receipt(source_receipt,owner=target_owner,account_id=stored['subject_id'],
                        kind='delete',operation_id=operation_id,lifecycle=account_lifecycle,phase=reverse_phase)
                    if previous.get('state') not in ('applying', 'applied'):
                        current_source = await canonical.owner_stats(owner=tombstone_owner)
                        current_target = await canonical.owner_stats(owner=target_owner)
                        reverse_snapshot = {'source_fingerprint': current_source['fingerprint'],
                            'target_fingerprint': current_target['fingerprint'],
                            'prior_receipt_id': forward['receipt_id'], 'prior_receipt_hash': forward['receipt_hash']}
                        _start_step(reverse_step, manifest=reverse_snapshot)
                    elif (type(reverse_snapshot) is not dict
                            or reverse_snapshot.get('prior_receipt_id') != forward['receipt_id']
                            or reverse_snapshot.get('prior_receipt_hash') != forward['receipt_hash']):
                        raise RuntimeError('retained memory compensation proof changed')
                    from src.memery_provider import _HostOwnerTransition
                    from src.openclank.chat_source_bridge import issue_account_source_transition
                    transition = _HostOwnerTransition(database_id=manifests['memory_participants']['fm']['database_id'],
                        account_id=stored['subject_id'], operation_id=operation_id, step='memory_fm_tombstoned',
                        generation=generation, action='reverse', owner=tombstone_owner, new_owner=target_owner,
                        expected_source_fingerprint=reverse_snapshot['source_fingerprint'],
                        expected_target_fingerprint=reverse_snapshot['target_fingerprint'],
                        prior_receipt_id=forward['receipt_id'], prior_receipt_hash=forward['receipt_hash'],
                        rag_relocation=({'from': snapshot['rag_relocation']['to'], 'to': snapshot['rag_relocation']['from']}
                            if snapshot['rag_relocation'] is not None else None))
                    capability = issue_account_source_transition(lifecycle=account_lifecycle, operation_id=operation_id,
                        claim_token=claim_token, provider=canonical, action='rename', owner=tombstone_owner, new_owner=target_owner)
                    receipt = await canonical._host_owner_transition(transition, capability=capability,
                        status=previous.get('state') in ('applying', 'applied'))
                    if receipt.get('state') != 'applied':
                        raise RuntimeError('retained memory reverse original unavailable')
                    if previous.get('state')=='applied' and previous.get('receipt')!=receipt:
                        raise RuntimeError('retained memory reverse receipt changed')
                    if previous.get('state')!='applied': _checkpoint(reverse_step, receipt)
                    cleanup_step='memory_fm_reverse_cache_'+str(generation)
                    if not _applied(cleanup_step):
                        _start_step(cleanup_step)
                        await canonical._host_owner_transition_cache_cleanup(transition,capability=capability,receipt=receipt)
                        _checkpoint(cleanup_step,{'receipt_id':receipt['receipt_id'],'receipt_hash':receipt['receipt_hash']})
                    renewal_step='host_sources_renewed_'+summary_step.removeprefix('host_sources_fenced_')
                    renewal_saved=steps.get(renewal_step)
                    if renewal_saved:
                        if (type(renewal_saved) is not dict or renewal_saved.get('state') not in ('applying','applied')
                            or (steps.get(reverse_step) or {}).get('receipt')!=receipt):
                            raise RuntimeError('retained reverse renewal checkpoint changed')
                    else:
                        source_now = await canonical.owner_stats(owner=tombstone_owner)
                        target_now = await canonical.owner_stats(owner=target_owner)
                        if (source_now.get('fingerprint') != receipt['result']['source_after_fingerprint']
                                or target_now.get('fingerprint') != receipt['result']['target_after_fingerprint']):
                            raise RuntimeError('retained memory reverse result changed')
                    if source_receipt.get('renewal')=='pending':
                        from src.openclank.chat_source_bridge import renew_account_host_sources
                        if not _applied(renewal_step):
                            _start_step(renewal_step)
                            renewal=await renew_account_host_sources(state=request.app.state,lifecycle=account_lifecycle,
                                operation_id=operation_id,claim_token=claim_token,old_owner=target_owner,
                                new_owner=target_owner,account_id=stored['subject_id'])
                            _checkpoint(renewal_step,renewal)
                    return receipt

                for kind in (('fm', 'native') if joined_memory else ('fm',)):
                    step = 'memory_' + kind + '_tombstoned'
                    if started(step):
                        await attempt(step, restore_fm if kind == 'fm' else lambda kind=kind: _move_memory_participant(request.app.state,
                            manifests['memory_participants'], kind, tombstone_owner, target_owner,
                            lifecycle=account_lifecycle, operation_id=operation_id, claim_token=claim_token, started=True, reverse=True,
                            fm_snapshot=manifests.get('memory_fm_tombstoned') if kind == 'fm' else None))
            elif started("memory_tombstoned") and memory_rollback is not None:
                async def restore_memory():
                    source = await memory_rollback.owner_stats(owner=target_owner)
                    tombstone = await memory_rollback.owner_stats(owner=tombstone_owner)
                    if _inventory_present(source) and _inventory_present(tombstone):
                        raise RuntimeError("memory compensation found split owner state")
                    if _inventory_present(tombstone):
                        await _memory_owner_transition(memory_rollback, action='rename', owner=tombstone_owner, new_owner=target_owner, lifecycle=account_lifecycle, operation_id=operation_id, claim_token=claim_token, state=request.app.state)

                await attempt("memory_tombstoned", restore_memory)
            # Canonical RAG rows belong to the Memory provider while the
            # Personal adapter owns their source-path directories. Restore
            # the canonical owner first so the reverse Personal transition
            # can rewrite paths on rows that are visible under source_owner.
            if started("personal_rag_tombstoned"):
                async def restore_personal():
                    proof = None
                    if typed_memory:
                        receipt = rollback_results.get('memory_fm_tombstoned')
                        snapshot = manifests.get('memory_fm_tombstoned') or {}
                        if type(receipt) is not dict or receipt.get('state') != 'applied':
                            raise RuntimeError('personal native reverse receipt unavailable')
                        relocation = snapshot.get('rag_relocation')
                        inverse = {'from': relocation['to'], 'to': relocation['from']} if relocation is not None else None
                        _, provider = _memory_participants_current(request.app.state, manifests['memory_participants'])
                        from src.openclank.chat_source_bridge import issue_account_source_transition
                        from routes.personal_routes import _issue_native_relocation_proof
                        capability = issue_account_source_transition(lifecycle=account_lifecycle, operation_id=operation_id,
                            claim_token=claim_token, provider=provider, action='rename', owner=tombstone_owner, new_owner=target_owner)
                        proof = _issue_native_relocation_proof(capability=capability, provider=provider,
                            step=f"memory_fm_reverse_{snapshot['generation']}", receipt=receipt, relocation=inverse,
                            source=tombstone_owner, target=target_owner, manifest={'rag_relocation': inverse})
                    return personal_rollback.compensate(target_owner, tombstone_owner,
                        manifests['personal_rag_tombstoned'], native_relocation_proof=proof)
                await attempt(
                    "personal_rag_tombstoned",
                    restore_personal,
                )
            if started("mimo_tombstoned") and callable(
                getattr(mimo_rollback, "compensate_owner_rename", None)
            ):
                await attempt(
                    "mimo_tombstoned",
                    lambda: mimo_rollback.compensate_owner_rename(
                        target_owner,
                        tombstone_owner,
                        manifests.get("mimo_stopped_preflight", manifests["mimo_tombstoned"]),
                    ),
                )
            if started("filesystem_registry_tombstoned"):
                await attempt(
                    "filesystem_registry_tombstoned",
                    lambda: filesystem_rollback.compensate_owner_rename(
                        target_owner,
                        tombstone_owner,
                        manifests["filesystem_registry_tombstoned"],
                    ),
                )
            if started("research_tombstoned"):
                compensate_research = getattr(
                    research_rollback,
                    "compensate_owner_rename",
                    None,
                )
                if callable(compensate_research):
                    await attempt(
                        "research_tombstoned",
                        lambda: compensate_research(target_owner, tombstone_owner),
                    )
                release = getattr(research_rollback, "release_owner_fence", None)
                if callable(release):
                    await attempt("research_fence", lambda: release(target_owner))

            if rollback_errors:
                recovery_error = RuntimeError(
                    "pre-auth compensation failed in "
                    + ", ".join(sorted(rollback_errors))
                )
                # Every staging adapter is a reconciler. Remove all pre-auth
                # applied markers even when one reverse transition failed so
                # the next request re-observes source/tombstone state instead
                # of skipping domains that were successfully restored.
                if typed_memory:
                    _fail_operation(recovery_error)
                    raise RuntimeError('account deletion compensation remains pending') from exc
                account_lifecycle.rewind_operation_steps(
                    operation_id,
                    pre_auth_steps,
                    recovery_error,
                    claim_token=claim_token,
                )
                raise RuntimeError(
                    "account deletion compensation did not restore every domain: "
                    + ", ".join(sorted(rollback_errors))
                ) from exc
            if typed_memory and (started('memory_native_tombstoned') or started('memory_fm_tombstoned')):
                fm_snapshot = manifests.get('memory_fm_tombstoned')
                if started('memory_fm_tombstoned'):
                    generation = fm_snapshot['generation']
                    _checkpoint(f'memory_compensation_complete_{generation}',
                        {'state':'complete', 'results':rollback_results})
                account_lifecycle.checkpoint_delete_operation(operation_id, 'memory_compensation_pending',
                    {'state':'complete', 'generation': fm_snapshot.get('generation') if type(fm_snapshot) is dict else None,
                     'results':rollback_results}, claim_token=claim_token)
            account_lifecycle.rewind_operation_steps(
                operation_id,
                pre_auth_steps,
                exc,
                claim_token=claim_token,
            )

        async def _delete_pre_auth_error(
            label: str,
            exc: BaseException,
            *,
            status_code: int = 503,
        ) -> None:
            await _compensate_delete_pre_auth(exc)
            raise HTTPException(status_code, label) from exc

        if (durable and ((operation.get("receipt") or {}).get("error") == "pre-auth compensation failed in mimo_tombstoned"
            or "mimo_stopped_preflight" in (operation.get("manifest") or {}))
            and (steps.get("mimo_tombstoned") or {}).get("state") == "applying"
            and not any(steps.get(name) for name in ("memory_native_tombstoned", "memory_fm_tombstoned", "auth_deleted"))):
            manifests = operation["manifest"]
            supervisor = _mimo_lifecycle_supervisor(request)
            if "mimo_stopped_preflight" not in manifests:
                if (account_id_for(target_owner) != tombstone_identity or operation["subject_id"] != tombstone_identity
                    or operation["tombstone_owner"] != f"deleted:{tombstone_identity}"):
                    raise HTTPException(409, "Account deletion subject changed during compensation")
                try:
                    stopped = await supervisor.preview_owner_deletion(
                        target_owner, tombstone_owner, tombstone_identity, original=manifests["mimo_tombstoned"])
                    _start_step("mimo_stopped_preflight", manifest=stopped)
                except Exception as exc:
                    _fail_operation(RuntimeError("pre-auth compensation failed in mimo_tombstoned"))
                    raise HTTPException(409, "Cannot prove the Agent owner move was unapplied") from exc
            await _compensate_delete_pre_auth(RuntimeError("Agent preflight revalidated after worker shutdown"))
            raise HTTPException(409, "Account compensation completed; retry the operation")

        if typed_memory and isinstance(steps.get('memory_compensation_pending'), dict) and (
                steps['memory_compensation_pending'].get('state') == 'applying'
                or (steps['memory_compensation_pending'].get('state') == 'applied'
                    and (steps['memory_compensation_pending'].get('receipt') or {}).get('generation')
                        == ((operation.get('manifest') or {}).get('memory_fm_tombstoned') or {}).get('generation')
                    and any(steps.get(name) for name in ('memory_native_tombstoned', 'memory_fm_tombstoned')))):
            await _compensate_delete_pre_auth(RuntimeError('resume pending account compensation'))
            raise HTTPException(409, 'Account compensation completed; retry the operation')
        if durable and not _applied("provider_flows_drained"):
            try:
                _start_step("provider_flows_drained")
                await _drain_owner_provider_flows(request, target_owner)
                _checkpoint("provider_flows_drained", True)
            except Exception as exc:
                await _delete_pre_auth_error(
                    "Cannot drain active provider login flows for deletion",
                    exc,
                )
        elif not durable:
            await _drain_owner_provider_flows(request, target_owner)

        phase=None
        if typed_memory:
            try:
                snapshot=(operation.get('manifest') or {}).get('memory_fm_tombstoned')
                if type(snapshot) is dict and (steps.get('memory_fm_tombstoned') or {}).get('state') in ('applying','applied'):
                    generation=snapshot.get('generation')
                else:
                    generation,_,_=_next_fm_generation(operation,'memory_fm_tombstoned')
                phase_name='memory_fm_source_phase_'+str(generation)
                expected=_claimed_fm_source_phase(operation=operation,claim_token=claim_token,
                    database_id=operation['manifest']['memory_participants']['fm']['database_id'],
                    account_id=target_subject_id,operation_id=operation_id,step='memory_fm_tombstoned',generation=generation,
                    owner=target_owner,new_owner=tombstone_owner)
                saved=(operation.get('manifest') or {}).get(phase_name)
                if saved is None:
                    if generation==1 and steps.get('host_sources_fenced'): raise RuntimeError('original source phase unavailable')
                    _start_step(phase_name,manifest=expected)
                    _checkpoint(phase_name,expected)
                elif saved!=expected or (steps.get(phase_name) or {}).get('receipt')!=expected:
                    raise RuntimeError('original source phase changed')
                phase=expected
            except Exception as exc:
                _fail_operation(exc)
                raise HTTPException(503,'Account memory source phase unavailable') from exc
        try:
            from src.openclank.chat_source_bridge import fence_account_host_sources, validate_account_source_fence_receipt, account_source_summary_step
            summary_step=account_source_summary_step(phase)
            if durable and _applied(summary_step):
                saved = (steps[summary_step] or {}).get("receipt")
                validate_account_source_fence_receipt(saved, owner=target_owner, account_id=target_subject_id, kind='delete', operation_id=operation_id, lifecycle=account_lifecycle,phase=phase)
            else:
                _start_step(summary_step)
                source_receipt = await fence_account_host_sources(state=request.app.state, lifecycle=account_lifecycle,
                    operation_id=operation_id, claim_token=claim_token, owner=target_owner,
                    account_id=target_subject_id, kind="delete",phase=phase)
                validate_account_source_fence_receipt(source_receipt, owner=target_owner, account_id=target_subject_id, kind='delete', operation_id=operation_id, lifecycle=account_lifecycle,phase=phase)
                _checkpoint(summary_step, source_receipt)
        except Exception as exc:
            await _delete_pre_auth_error("Cannot fence account memory sources for deletion", exc)

        if durable and not _applied("runtime_caches_fenced"):
            try:
                _start_step("runtime_caches_fenced")
                _checkpoint(
                    "runtime_caches_fenced",
                    _invalidate_account_runtime(
                        request, target_owner, tombstone_owner, invalidate_session_cache=False,
                    ),
                )
            except Exception as exc:
                await _delete_pre_auth_error(
                    "Cannot fence account runtime caches for deletion",
                    exc,
                )

        research_handler = getattr(request.app.state, "research_handler", None)
        if durable and not _applied("research_tombstoned"):
            try:
                _start_step("research_tombstoned")
                fence = getattr(research_handler, "fence_owner", None)
                if callable(fence):
                    fence(target_owner)
                rename_research = getattr(research_handler, "rename_owner", None)
                reconcile_research = getattr(
                    research_handler,
                    "reconcile_owner_rename",
                    None,
                )
                receipt = (
                    reconcile_research(
                        target_owner,
                        tombstone_owner,
                        (operation.get("manifest") or {})["research_tombstoned"],
                    )
                    if callable(reconcile_research)
                    else {
                        "changed": (
                            rename_research(target_owner, tombstone_owner)
                            if callable(rename_research)
                            else 0
                        )
                    }
                )
                _checkpoint("research_tombstoned", receipt)
            except Exception as exc:
                await _delete_pre_auth_error(
                    "Cannot fence active research for deletion",
                    exc,
                )

        ancillary_store = None
        ancillary_manifest = None
        ancillary_token = (
            hashlib.sha256(f"{operation_id}:ancillary".encode("utf-8")).hexdigest()[:32]
            if durable
            else ""
        )
        if durable and not _applied("ancillary_tombstoned"):
            try:
                ancillary_store = ancillary_lifecycle
                ancillary_manifest = (operation.get("manifest") or {})[
                    "ancillary_tombstoned"
                ]
                _start_step("ancillary_tombstoned")
                receipt = ancillary_store.stage_owner_to_tombstone(
                    target_owner,
                    tombstone_owner,
                    ancillary_manifest,
                    operation_token=ancillary_token,
                    account_id=(operation or {}).get('subject_id'),
                )
                _checkpoint("ancillary_tombstoned", receipt)
            except Exception as exc:
                await _delete_pre_auth_error(
                    "Cannot snapshot ancillary account state for deletion",
                    exc,
                )
        elif durable:
            ancillary_store = ancillary_lifecycle
            ancillary_manifest = (operation.get("manifest") or {})[
                "ancillary_tombstoned"
            ]

        skills_store = _account_skills_domain_store(request) if durable else None
        if durable and not _applied("skills_tombstoned"):
            try:
                _start_step("skills_tombstoned")
                receipt = _transition_account_skills(
                    request,
                    target_owner,
                    tombstone_owner,
                    (operation.get("manifest") or {})["skills_tombstoned"],
                )
                _checkpoint("skills_tombstoned", receipt)
            except Exception as exc:
                await _delete_pre_auth_error(
                    "Cannot snapshot account Skills state for deletion",
                    exc,
                )

        shell_audit_store = _account_shell_audit_store() if durable else None
        if durable and not _applied("shell_audit_tombstoned"):
            try:
                _start_step("shell_audit_tombstoned")
                receipt = shell_audit_store.reconcile_owner_rename(
                    target_owner,
                    tombstone_owner,
                    (operation.get("manifest") or {})["shell_audit_tombstoned"],
                )
                _checkpoint("shell_audit_tombstoned", receipt)
            except Exception as exc:
                await _delete_pre_auth_error(
                    "Cannot snapshot shell audit state for deletion",
                    exc,
                )

        filesystem_registry = None
        if durable and not _applied("filesystem_registry_tombstoned"):
            try:
                from src.openclank.filesystem_registry import FilesystemRootRegistry
                filesystem_registry = FilesystemRootRegistry()
                _start_step("filesystem_registry_tombstoned")
                receipt = filesystem_registry.reconcile_owner_rename(
                    target_owner,
                    tombstone_owner,
                    (operation.get("manifest") or {})[
                        "filesystem_registry_tombstoned"
                    ],
                )
                _checkpoint("filesystem_registry_tombstoned", receipt)
            except Exception as exc:
                await _delete_pre_auth_error(
                    "Cannot snapshot filesystem access state for deletion",
                    exc,
                )
        elif durable:
            from src.openclank.filesystem_registry import FilesystemRootRegistry
            filesystem_registry = FilesystemRootRegistry()

        mimo_supervisor = getattr(request.app.state, "mimo_supervisor", None)
        if durable and not _applied("mimo_tombstoned"):
            try:
                manifests = operation["manifest"]
                expected = manifests.get("mimo_stopped_preflight", manifests["mimo_tombstoned"])
                _start_step("mimo_tombstoned")
                receipt = (
                    await mimo_supervisor.reconcile_owner_rename(
                        target_owner,
                        tombstone_owner,
                        expected,
                    )
                    if callable(
                        getattr(mimo_supervisor, "reconcile_owner_rename", None)
                    )
                    else {"available": False}
                )
                _checkpoint("mimo_tombstoned", receipt)
            except Exception as exc:
                await _delete_pre_auth_error(
                    "Cannot snapshot Open Clank agent state for deletion",
                    exc,
                )

        memory_tombstoned = False
        if typed_memory:
            manifest = (operation.get('manifest') or {})['memory_participants']
            try:
                _memory_participants_current(request.app.state, manifest)
                for kind in (('native', 'fm') if joined_memory else ('fm',)):
                    step = 'memory_' + kind + '_tombstoned'
                    if _applied(step):
                        continue
                    previously_started = bool(steps.get(step))
                    fm_snapshot = (operation.get('manifest') or {}).get(step) if previously_started and kind == 'fm' else None
                    if kind == 'fm' and not previously_started:
                        _, canonical = _memory_participants_current(request.app.state, manifest)
                        personal = (operation.get('manifest') or {}).get('personal_rag_tombstoned')
                        if type(personal) is not dict or 'rag_relocation' not in personal:
                            raise RuntimeError('personal RAG relocation manifest unavailable')
                        _account_personal_rag_store(request).assert_relocation_current(target_owner, tombstone_owner, personal)
                        generation, prior_id, prior_hash = _next_fm_generation(operation, step)
                        fm_snapshot = {'source': await canonical.owner_stats(owner=target_owner), 'target': await canonical.owner_stats(owner=tombstone_owner),
                            'generation': generation, 'prior_receipt_id': prior_id, 'prior_receipt_hash': prior_hash,
                            'rag_relocation': personal['rag_relocation']}
                        if _inventory_present(fm_snapshot['target']): raise RuntimeError('retained memory target changed')
                    if kind == 'fm':
                        _account_personal_rag_store(request).assert_relocation_current(target_owner, tombstone_owner,
                            (operation.get('manifest') or {}).get('personal_rag_tombstoned'))
                    _start_step(step, manifest=fm_snapshot if kind == 'fm' and not previously_started else None)
                    receipt = await _move_memory_participant(request.app.state, manifest, kind, target_owner, tombstone_owner,
                        lifecycle=account_lifecycle, operation_id=operation_id, claim_token=claim_token, started=previously_started, fm_snapshot=fm_snapshot, step=step)
                    if kind == 'fm':
                        marker = f"memory_fm_forward_{fm_snapshot['generation']}"
                        saved = (steps.get(marker) or {}).get('receipt')
                        if saved is not None and saved != receipt:
                            raise RuntimeError('retained memory forward receipt changed')
                        if saved is None:
                            _checkpoint(marker, receipt)
                    _checkpoint(step, receipt)
                    if kind=='fm':
                        verified_step=f'memory_fm_forward_verified_{fm_snapshot["generation"]}'
                        if not _applied(verified_step):
                            _start_step(verified_step)
                            _, canonical = _memory_participants_current(request.app.state, manifest)
                            await _move_memory_participant(request.app.state,manifest,'fm',target_owner,tombstone_owner,
                                lifecycle=account_lifecycle,operation_id=operation_id,claim_token=claim_token,started=True,
                                fm_snapshot=fm_snapshot,step=step,cleanup_receipt=receipt)
                            verified=await _verify_fm_move_after(canonical,target_owner,tombstone_owner,receipt)
                            _checkpoint(verified_step,verified)
                memory_tombstoned = True
            except Exception as exc:
                await _delete_pre_auth_error('Cannot stage retained memory for deletion', exc)
        elif memory_provider and not _applied("memory_tombstoned"):
            try:
                _start_step("memory_tombstoned")
                if durable:
                    source_inventory = await memory_provider.owner_stats(owner=target_owner)
                    target_inventory = await memory_provider.owner_stats(owner=tombstone_owner)
                    source_present = _has_inventory(source_inventory)
                    target_present = _has_inventory(target_inventory)
                    if source_present and target_present:
                        raise RuntimeError(
                            "source and tombstone memory both contain durable state"
                        )
                    if source_present:
                        await _memory_owner_transition(memory_provider, action='rename', owner=target_owner, new_owner=tombstone_owner, lifecycle=account_lifecycle, operation_id=operation_id, claim_token=claim_token, state=request.app.state)
                else:
                    await _memory_owner_transition(memory_provider, action='rename', owner=target_owner, new_owner=tombstone_owner, lifecycle=account_lifecycle, operation_id=operation_id, claim_token=claim_token, state=request.app.state)
                memory_tombstoned = True
                _checkpoint("memory_tombstoned", True)
            except Exception as exc:
                await _delete_pre_auth_error(
                    "Cannot snapshot user memory for deletion",
                    exc,
                )
        elif _applied("memory_tombstoned"):
            memory_tombstoned = True

        upload_handler = getattr(request.app.state, "upload_handler", None)
        upload_manifest = None
        if durable and upload_handler is not None and not _applied("uploads_tombstoned"):
            try:
                upload_manifest = (
                    (operation.get("manifest") or {}).get("uploads_tombstoned")
                    or upload_handler.preview_owner_rename(target_owner, tombstone_owner)
                )
                _start_step("uploads_tombstoned", manifest=upload_manifest)
                receipt = upload_handler.stage_owner_to_tombstone(
                    target_owner,
                    tombstone_owner,
                    upload_manifest,
                )
                _checkpoint("uploads_tombstoned", receipt)
            except Exception as exc:
                await _delete_pre_auth_error(
                    "Cannot snapshot uploads for deletion",
                    exc,
                )
        elif durable and upload_handler is not None:
            upload_manifest = (operation.get("manifest") or {}).get("uploads_tombstoned")

        personal_store = None
        personal_manifest = None
        if durable and not _applied("personal_rag_tombstoned"):
            try:
                personal_store = _account_personal_rag_store(request)
                personal_manifest = (
                    (operation.get("manifest") or {}).get("personal_rag_tombstoned")
                    or personal_store.preview_owner_rename(target_owner, tombstone_owner)
                )
                _start_step("personal_rag_tombstoned", manifest=personal_manifest)
                personal_store.assert_relocation_current(target_owner, tombstone_owner, personal_manifest)
                personal_proof = await _personal_native_relocation_proof(request.app.state, lifecycle=account_lifecycle,
                    operation_id=operation_id, claim_token=claim_token, step='memory_fm_tombstoned',
                    personal_manifest=personal_manifest, source=target_owner, target=tombstone_owner) if typed_memory else None
                receipt = personal_store.stage_to_tombstone(
                    target_owner,
                    tombstone_owner,
                    personal_manifest,
                    native_relocation_proof=personal_proof,
                )
                _checkpoint("personal_rag_tombstoned", receipt)
            except Exception as exc:
                await _delete_pre_auth_error(
                    "Cannot snapshot personal RAG for deletion",
                    exc,
                )
        elif durable:
            personal_store = _account_personal_rag_store(request)
            personal_manifest = (operation.get("manifest") or {}).get(
                "personal_rag_tombstoned"
            )

        copal_bridge = _copal_lifecycle_bridge(request)
        if durable and not _applied("copal_tombstoned"):
            try:
                _start_step("copal_tombstoned")
                receipt = await _call_copal_lifecycle(
                    copal_bridge,
                    "rename_owner",
                    {
                        "old_owner": copal_owner_for_user(target_owner),
                        "new_owner": copal_owner_for_user(tombstone_owner),
                        "manifest": (operation.get("manifest") or {})["copal"],
                    },
                )
                _checkpoint("copal_tombstoned", receipt or {"available": False})
            except Exception as exc:
                await _delete_pre_auth_error(
                    "Cannot snapshot Copal state for deletion",
                    exc,
                )

        media_store = batch_store = None
        if durable and memory_provider:
            media_store, batch_store = _account_memory_domain_stores(_account_domain_provider(request.app.state, memory_provider, joined_memory))
            if not _applied("memory_media_tombstoned"):
                try:
                    _start_step("memory_media_tombstoned")
                    receipt = media_store.rename_owner(target_owner, tombstone_owner)
                    _checkpoint("memory_media_tombstoned", receipt)
                except Exception as exc:
                    await _delete_pre_auth_error(
                        "Cannot snapshot Memory media for deletion",
                        exc,
                    )
            if not _applied("import_staging_tombstoned"):
                try:
                    if typed_memory:
                        prior=steps.get('import_staging_tombstoned')
                        if prior:
                            frozen=(operation.get('manifest') or {}).get('import_staging_tombstoned')
                            if type(frozen) is not dict or set(frozen)!={'source','target'}:
                                raise RuntimeError('original staging move unavailable')
                        else:
                            frozen={'source':batch_store.preview_owner_staging(target_owner,guarded=typed_memory),
                                    'target':batch_store.preview_owner_staging(tombstone_owner,guarded=typed_memory)}
                            _start_step('import_staging_tombstoned',manifest=frozen)
                        _,canonical=_memory_participants_current(request.app.state,(operation.get('manifest') or {})['memory_participants'])
                        _verify_guarded_delete_staging(store=batch_store,provider=canonical,lifecycle=account_lifecycle,
                            operation_id=operation_id,claim_token=claim_token,account_id=target_subject_id,
                            source=target_owner,target=tombstone_owner,phase='before_move')
                        source,target=frozen['source'],frozen['target']
                    else:
                        _start_step("import_staging_tombstoned")
                        source = batch_store.preview_owner_staging(target_owner,guarded=typed_memory)
                        target = batch_store.preview_owner_staging(tombstone_owner,guarded=typed_memory)
                    receipt = batch_store.rename_owner_staging(
                        target_owner,
                        tombstone_owner,
                        expected_source=source,
                        expected_target=target,
                        guarded=typed_memory,
                    )
                    _checkpoint("import_staging_tombstoned", receipt)
                except Exception as exc:
                    await _delete_pre_auth_error(
                        "Cannot snapshot import staging for deletion",
                        exc,
                    )

        lifecycle_tombstoned = False
        if account_lifecycle is not None and not _applied("lifecycle_tombstoned"):
            try:
                _start_step("lifecycle_tombstoned")
                if durable:
                    receipt = account_lifecycle.reconcile_rename(
                        target_owner,
                        tombstone_owner,
                    )
                else:
                    receipt = account_lifecycle.rename_owner(target_owner, tombstone_owner)
                lifecycle_tombstoned = True
                _checkpoint(
                    "lifecycle_tombstoned",
                    dict(receipt.stores) if durable else True,
                )
            except Exception as exc:
                await _delete_pre_auth_error(
                    "Cannot snapshot normalized user state for deletion",
                    exc,
                )

        elif _applied("lifecycle_tombstoned"):
            lifecycle_tombstoned = True

        file_store = None
        file_manifest = None
        if durable and not _applied("file_domains_tombstoned"):
            try:
                file_store = _account_file_domain_store()
                file_manifest = (
                    (operation.get("manifest") or {}).get("file_domains_tombstoned")
                    or file_store.preview_rename(target_owner, tombstone_owner)
                )
                _start_step("file_domains_tombstoned", manifest=file_manifest)
                receipt = file_store.stage_to_tombstone(
                    target_owner,
                    tombstone_owner,
                    file_manifest,
                )
                _checkpoint("file_domains_tombstoned", receipt)
                _checkpoint(
                    "preferences_detached",
                    {"covered_by": "file_domains_tombstoned"},
                )
            except Exception as exc:
                await _delete_pre_auth_error(
                    "Cannot snapshot file-backed account state",
                    exc,
                )
        elif durable:
            file_store = _account_file_domain_store()
            file_manifest = (operation.get("manifest") or {}).get(
                "file_domains_tombstoned"
            )

        sql_tombstoned = False
        sql_store = None
        sql_manifest = None
        if durable and not _applied("common_sql_tombstoned"):
            try:
                sql_store = _account_sql_domain_store()
                sql_manifest = (
                    (operation.get("manifest") or {}).get("common_sql_default_seed_preflight")
                    or (operation.get("manifest") or {}).get("common_sql_tombstoned")
                    or sql_store.owner_inventory(target_owner, account_id=target_subject_id)
                )
                _start_step("common_sql_tombstoned", manifest=None if "common_sql_tombstoned" in operation["manifest"] else sql_manifest)
                receipt = sql_store.reconcile_owner(
                    target_owner,
                    tombstone_owner,
                    expected_source=sql_manifest,
                    account_id=target_subject_id,
                )
                sql_tombstoned = True
                _checkpoint("common_sql_tombstoned", receipt.as_dict())
            except Exception as exc:
                await _delete_pre_auth_error(
                    "Cannot snapshot SQL-owned account state for deletion",
                    exc,
                )
        elif _applied("common_sql_tombstoned"):
            sql_tombstoned = True
            sql_store = _account_sql_domain_store()
            manifests = operation.get("manifest") or {}
            sql_manifest = manifests.get("common_sql_default_seed_preflight", manifests.get("common_sql_tombstoned"))

        try:
            if not _applied("preferences_detached"):
                _start_step("preferences_detached")
            if _applied("preferences_detached"):
                prefs_snapshot = None
            elif durable:
                _rename_user_prefs(target_owner, tombstone_owner)
                prefs_snapshot = None
            else:
                prefs_snapshot = _detach_user_prefs(target_owner)
            _checkpoint("preferences_detached", True)
        except Exception as exc:
            await _delete_pre_auth_error(
                "Cannot remove user preferences",
                exc,
            )

        if durable and not _applied("access_caches_fenced"):
            _start_step("access_caches_fenced")
            _invalidate_api_token_cache()
            try:
                from services.memory.principal_context import invalidate_principal_cache

                invalidate_principal_cache(target_owner)
                invalidate_principal_cache(tombstone_owner)
            except Exception:
                logger.debug("Principal cache fence was unavailable", exc_info=True)
            _checkpoint("access_caches_fenced", True)

        try:
            if _applied("auth_deleted"):
                ok = True
            else:
                _start_step("auth_deleted")
                username_for_subject = getattr(
                    auth_manager,
                    "username_for_account_id",
                    lambda _subject: target_owner,
                )(target_subject_id)
                if durable and username_for_subject is None:
                    ok = True
                    auth_receipt = {
                        "recovered": True,
                        "account_absent": True,
                    }
                else:
                    if durable:
                        account_lifecycle.set_operation_state(
                            operation_id,
                            "auth_committing",
                            claim_token=claim_token,
                        )
                    delete_barrier = (
                        getattr(auth_manager, "delete_user_auth_barrier", None)
                        if durable
                        else None
                    )
                    ok = (
                        delete_barrier(body.username, user)
                        if callable(delete_barrier)
                        else auth_manager.delete_user(body.username, user)
                    )
                    auth_receipt = True
                if ok:
                    _checkpoint("auth_deleted", auth_receipt)
                    # S29: account-wide achievement ledger is keyed by the
                    # immutable account id (stable across username rename).
                    # Delete removes it with the account; rename needs no
                    # move because the id does not change.
                    try:
                        from src.openclank.copal_treehouse_repository import TreeHouseRepository
                        from src.constants import DATA_DIR
                        import os as _os
                        from pathlib import Path as _Path
                        _th_path = _os.environ.get("TREEHOUSE_REPOSITORY_PATH") or str(_Path(DATA_DIR) / "treehouse.sqlite3")
                        _th_repo = TreeHouseRepository(_th_path)
                        _th_repo.purge_achievement_ledger(str(target_subject_id or target_owner))
                    except Exception:
                        logger.exception("Failed to purge TreeHouse achievement ledger after account delete")
                    if durable:
                        account_lifecycle.set_operation_state(
                            operation_id,
                            "committed",
                            claim_token=claim_token,
                        )
        except Exception as auth_exc:
            current_auth_owner = getattr(
                auth_manager,
                "username_for_account_id",
                lambda _subject: target_owner,
            )(target_subject_id)
            compensate = not durable or current_auth_owner == target_owner
            if durable and compensate:
                # Auth is still authoritative at the source, so every staged
                # owner move must be reversed before this request releases its
                # claim. The helper also rewinds applied checkpoints for replay.
                _invalidate_api_token_cache()
                await _compensate_delete_pre_auth(auth_exc)
                raise
            if compensate:
                try:
                    if durable:
                        _rename_user_prefs(tombstone_owner, target_owner)
                    else:
                        _restore_user_prefs(prefs_snapshot)
                except Exception:
                    logger.exception("Failed to restore user prefs after user-delete failure")
                if memory_tombstoned:
                    try:
                        await _memory_owner_transition(memory_provider, action='rename', owner=tombstone_owner, new_owner=target_owner, lifecycle=account_lifecycle, operation_id=operation_id, claim_token=claim_token, state=request.app.state)
                    except Exception:
                        logger.exception("Failed to restore tombstoned memory after user-delete failure")
                if lifecycle_tombstoned:
                    try:
                        account_lifecycle.rename_owner(tombstone_owner, target_owner)
                    except Exception:
                        logger.exception(
                            "Failed to restore normalized state after user-delete failure"
                        )
                if sql_tombstoned:
                    try:
                        sql_store.compensate_owner(
                            target_owner,
                            tombstone_owner,
                            expected_source=sql_manifest,
                            account_id=target_subject_id,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to restore SQL state after user-delete failure"
                        )
                if file_store is not None and file_manifest is not None:
                    try:
                        file_store.compensate(
                            target_owner,
                            tombstone_owner,
                            file_manifest,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to restore file-backed state after user-delete failure"
                        )
                if media_store is not None and _applied("memory_media_tombstoned"):
                    try:
                        media_store.rename_owner(tombstone_owner, target_owner)
                    except Exception:
                        logger.exception(
                            "Failed to restore Memory media after user-delete failure"
                        )
                if batch_store is not None and _applied("import_staging_tombstoned"):
                    try:
                        source = batch_store.preview_owner_staging(tombstone_owner,guarded=typed_memory)
                        target = batch_store.preview_owner_staging(target_owner,guarded=typed_memory)
                        batch_store.rename_owner_staging(
                            tombstone_owner,
                            target_owner,
                            expected_source=source,
                            expected_target=target,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to restore import staging after user-delete failure"
                        )
                if upload_handler is not None and upload_manifest is not None:
                    try:
                        upload_handler.compensate_owner_rename(
                            target_owner,
                            tombstone_owner,
                            upload_manifest,
                        )
                    except Exception:
                        logger.exception("Failed to restore uploads after user-delete failure")
                if personal_store is not None and personal_manifest is not None:
                    try:
                        personal_store.compensate(
                            target_owner,
                            tombstone_owner,
                            personal_manifest,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to restore personal RAG after user-delete failure"
                        )
                if copal_bridge is not None and _applied("copal_tombstoned"):
                    try:
                        await _call_copal_lifecycle(
                            copal_bridge,
                            "compensate_owner_rename",
                            {
                                "old_owner": copal_owner_for_user(target_owner),
                                "new_owner": copal_owner_for_user(tombstone_owner),
                                "manifest": copal_manifest,
                            },
                        )
                    except Exception:
                        logger.exception("Failed to restore Copal after user-delete failure")
                compensate_research = getattr(
                    research_handler,
                    "compensate_owner_rename",
                    None,
                )
                if callable(compensate_research) and _applied("research_tombstoned"):
                    try:
                        compensate_research(target_owner, tombstone_owner)
                    except Exception:
                        logger.exception(
                            "Failed to restore active research after user-delete failure"
                        )
            # delete_user can touch ApiToken rows before a later auth-store write
            # fails. Dirty the bearer cache anyway so a partial token purge does
            # not leave already-cached tokens authenticating until restart.
            _invalidate_api_token_cache()
            _fail_operation(RuntimeError("authentication account deletion failed"))
            raise
        if not ok:
            if not durable:
                try:
                    _restore_user_prefs(prefs_snapshot)
                except Exception:
                    logger.exception("Failed to restore user prefs after rejected user delete")
                if memory_tombstoned:
                    await _memory_owner_transition(memory_provider, action='rename', owner=tombstone_owner, new_owner=target_owner, lifecycle=account_lifecycle, operation_id=operation_id, claim_token=claim_token, state=request.app.state)
                if lifecycle_tombstoned:
                    account_lifecycle.rename_owner(tombstone_owner, target_owner)
            exc = HTTPException(400, "Cannot delete user")
            if durable:
                await _compensate_delete_pre_auth(exc)
                raise exc
            _fail_operation(exc)
            raise exc
        scheduler = getattr(request.app.state, "task_scheduler", None)
        if durable and scheduler is not None:
            if not _applied("task_runtime_tombstoned"):
                _start_step("task_runtime_tombstoned")
                manifest = (operation.get("manifest") or {}).get("task_runtime")
                if not manifest or manifest.get("available") is False:
                    manifest = scheduler.preview_owner_runtime_rename(target_owner, tombstone_owner)
                receipt = scheduler.reconcile_owner_runtime(target_owner, tombstone_owner, manifest)
                _checkpoint("task_runtime_tombstoned", receipt)
            if not _applied("task_runtime_purged"):
                _start_step("task_runtime_purged")
                expected = (steps["task_runtime_tombstoned"].get("receipt") or {}).get("target")
                receipt = scheduler.purge_owner_runtime(tombstone_owner, expected=expected)
                _checkpoint("task_runtime_purged", receipt)
        if durable and not _applied("preferences_purged"):
            try:
                _start_step("preferences_purged")
                _detach_user_prefs(tombstone_owner)
                _checkpoint("preferences_purged", True)
            except Exception as exc:
                _fail_operation(exc)
                raise HTTPException(
                    500,
                    "User deleted; tombstoned preference purge failed",
                ) from exc
        if durable and not _applied("common_sql_purged"):
            try:
                _start_step("common_sql_purged")
                receipt = sql_store.purge_owner(
                    tombstone_owner,
                    expected_inventory=sql_manifest,
                    account_id=target_subject_id,
                )
                _checkpoint("common_sql_purged", receipt.as_dict())
            except Exception as exc:
                _fail_operation(exc)
                raise HTTPException(
                    500,
                    "User deleted; tombstoned SQL-state purge failed",
                ) from exc
        if durable and file_store is not None and not _applied("file_domains_purged"):
            try:
                _start_step("file_domains_purged")
                receipt = file_store.purge_owner(
                    tombstone_owner,
                    expected=file_manifest,
                )
                _checkpoint("file_domains_purged", receipt)
                _checkpoint("preferences_purged", {"covered_by": "file_domains_purged"})
            except Exception as exc:
                _fail_operation(exc)
                raise HTTPException(
                    500,
                    "User deleted; tombstoned file-state purge failed",
                ) from exc
        if durable and ancillary_store is not None and not _applied("ancillary_purged"):
            try:
                _start_step("ancillary_purged")
                receipt = ancillary_store.purge_owner(
                    tombstone_owner,
                    expected=ancillary_manifest["source"],
                    operation_token=ancillary_token,
                    account_id=(operation or {}).get('subject_id'),
                )
                _checkpoint("ancillary_purged", receipt)
            except Exception as exc:
                _fail_operation(exc)
                raise HTTPException(
                    500,
                    "User deleted; tombstoned ancillary-state purge failed",
                ) from exc
        if durable and skills_store is not None and not _applied("skills_purged"):
            try:
                _start_step("skills_purged")
                staged = dict(
                    (steps.get("skills_tombstoned") or {}).get("receipt") or {}
                )
                expected = staged.get("target") or skills_store.preview_owner_purge(
                    tombstone_owner
                )
                receipt = skills_store.purge_owner(
                    tombstone_owner,
                    expected=expected,
                )
                if receipt.get("complete") is False:
                    raise RuntimeError("skill owner purge did not complete")
                _sync_account_skill_runtime(target_owner, tombstone_owner)
                _checkpoint("skills_purged", receipt)
            except Exception as exc:
                _fail_operation(exc)
                raise HTTPException(
                    500,
                    "User deleted; tombstoned Skills purge failed",
                ) from exc
        if (
            durable
            and shell_audit_store is not None
            and not _applied("shell_audit_purged")
        ):
            try:
                _start_step("shell_audit_purged")
                staged = dict(
                    (steps.get("shell_audit_tombstoned") or {}).get("receipt")
                    or {}
                )
                expected = staged.get("target") or shell_audit_store.owner_inventory(
                    tombstone_owner
                )
                receipt = shell_audit_store.purge_owner(
                    tombstone_owner,
                    expected=expected,
                )
                if receipt.get("complete") is False:
                    raise RuntimeError("shell audit owner purge did not complete")
                _checkpoint("shell_audit_purged", receipt)
            except Exception as exc:
                _fail_operation(exc)
                raise HTTPException(
                    500,
                    "User deleted; tombstoned shell audit purge failed",
                ) from exc
        if durable and upload_handler is not None and not _applied("uploads_purged"):
            try:
                _start_step("uploads_purged")
                token = hashlib.sha256(
                    f"{operation_id}:uploads".encode("utf-8")
                ).hexdigest()[:32]
                receipt = upload_handler.purge_owner_lifecycle(
                    tombstone_owner,
                    expected=(upload_manifest or {}).get("source"),
                    operation_token=token,
                )
                _checkpoint("uploads_purged", receipt)
            except Exception as exc:
                _fail_operation(exc)
                raise HTTPException(500, "User deleted; upload purge failed") from exc
        if durable and personal_store is not None and not _applied("personal_rag_purged"):
            try:
                _start_step("personal_rag_purged")
                token = hashlib.sha256(
                    f"{operation_id}:personal-rag".encode("utf-8")
                ).hexdigest()[:32]
                receipt = personal_store.purge_owner(
                    tombstone_owner,
                    expected=(personal_manifest or {}).get("source"),
                    operation_token=token,
                )
                _checkpoint("personal_rag_purged", receipt)
            except Exception as exc:
                _fail_operation(exc)
                raise HTTPException(500, "User deleted; personal RAG purge failed") from exc
        if durable and not _applied("copal_purged"):
            try:
                _start_step("copal_purged")
                receipt = await _call_copal_lifecycle(
                    copal_bridge,
                    "purge_owner",
                    {
                        "owner": copal_owner_for_user(tombstone_owner),
                        "expected": _tombstone_inventory(steps, "copal_tombstoned", "target_after",
                                                         copal_owner_for_user(tombstone_owner)) if copal_bridge else None,
                    },
                )
                _checkpoint("copal_purged", receipt or {"available": False})
            except Exception as exc:
                _fail_operation(exc)
                raise HTTPException(500, "User deleted; Copal purge failed") from exc
        if durable and not _applied("research_purged"):
            try:
                _start_step("research_purged")
                purge_research = getattr(research_handler, "purge_owner", None)
                receipt = (
                    purge_research(
                        tombstone_owner,
                        expected=(
                            (operation.get("manifest") or {})[
                                "research_tombstoned"
                            ]
                        )["source"],
                    )
                    if callable(purge_research)
                    else {"available": False}
                )
                _checkpoint("research_purged", receipt)
            except Exception as exc:
                _fail_operation(exc)
                raise HTTPException(500, "User deleted; active research purge failed") from exc
        if durable and media_store is not None and not _applied("memory_media_purged"):
            try:
                _start_step("memory_media_purged")
                expected = dict(
                    (steps.get("memory_media_tombstoned") or {}).get("receipt")
                    or media_store.preview_owner_purge(tombstone_owner)
                )
                expected = {
                    "count": int(expected.get("count") or 0),
                    "fingerprint": expected.get("fingerprint"),
                }
                receipt = media_store.purge_owner(tombstone_owner, expected=expected)
                _checkpoint("memory_media_purged", receipt)
            except Exception as exc:
                _fail_operation(exc)
                raise HTTPException(500, "User deleted; Memory media purge failed") from exc
        if durable and batch_store is not None and not _applied("import_staging_purged"):
            try:
                if typed_memory:
                    _,canonical=_memory_participants_current(request.app.state,(operation.get('manifest') or {})['memory_participants'])
                    _verify_guarded_delete_staging(store=batch_store,provider=canonical,lifecycle=account_lifecycle,
                        operation_id=operation_id,claim_token=claim_token,account_id=target_subject_id,
                        source=target_owner,target=tombstone_owner,phase='before_purge')
                _start_step("import_staging_purged")
                expected = dict(
                    (steps.get("import_staging_tombstoned") or {}).get("receipt")
                    or batch_store.preview_owner_staging(tombstone_owner,guarded=typed_memory)
                )
                expected = {
                    key: expected[key]
                    for key in ("count", "bytes", "fingerprint")
                    if key in expected
                }
                receipt = batch_store.purge_owner_staging(
                    tombstone_owner,
                    expected=expected,
                    guarded=typed_memory,
                )
                _checkpoint("import_staging_purged", receipt)
            except Exception as exc:
                _fail_operation(exc)
                raise HTTPException(500, "User deleted; import staging purge failed") from exc
        if typed_memory and memory_tombstoned:
            try:
                manifest = (operation.get('manifest') or {})['memory_participants']
                for kind in (('native', 'fm') if joined_memory else ('fm',)):
                    step = 'memory_' + kind + '_purged'
                    if _applied(step):
                        if kind == 'fm':
                            frozen = (operation.get('manifest') or {}).get(step)
                            if type(frozen) is not dict or set(frozen) != {'snapshot'}:
                                raise RuntimeError('memory purge checkpoint unavailable')
                            receipt = await _purge_memory_participant(request.app.state, manifest, kind, tombstone_owner, frozen['snapshot'],
                                lifecycle=account_lifecycle, operation_id=operation_id, claim_token=claim_token, started=True, step=step)
                            if receipt != (steps.get(step) or {}).get('receipt'):
                                raise RuntimeError('memory purge receipt changed')
                            if not _applied('memory_fm_purged_verified'):
                                _start_step('memory_fm_purged_verified')
                                _, canonical = _memory_participants_current(request.app.state, manifest)
                                await _purge_memory_participant(request.app.state,manifest,'fm',tombstone_owner,frozen['snapshot'],
                                    lifecycle=account_lifecycle,operation_id=operation_id,claim_token=claim_token,
                                    started=True,step=step,cleanup_receipt=receipt)
                                after = await canonical.owner_stats(owner=tombstone_owner)
                                if (_semantic_inventory_present(after) or after.get('fingerprint') != receipt['result']['source_after_fingerprint']):
                                    raise RuntimeError('retained memory purge remains incomplete')
                                _checkpoint('memory_fm_purged_verified', {'receipt_id':receipt['receipt_id'], 'receipt_hash':receipt['receipt_hash']})
                        continue
                    previously_started = bool(steps.get(step))
                    frozen = (operation.get('manifest') or {}).get(step) if previously_started else None
                    if not previously_started:
                        selected, canonical = _memory_participants_current(request.app.state, manifest)
                        snapshot = selected.lifecycle_snapshot(tombstone_owner) if kind == 'native' else await _claimed_fm_purge_stats(canonical,tombstone_owner,lifecycle=account_lifecycle,operation_id=operation_id,claim_token=claim_token)
                        if kind == 'native' and (snapshot['ids'] != manifest['native']['source']['ids'] or
                                snapshot['content_fingerprint'] != manifest['native']['source']['content_fingerprint']):
                            raise RuntimeError('native memory tombstone changed')
                        frozen = {'snapshot': snapshot}
                    if type(frozen) is not dict or set(frozen) != {'snapshot'}:
                        raise RuntimeError('memory purge checkpoint unavailable')
                    _start_step(step, manifest=frozen if not previously_started else None)
                    receipt = await _purge_memory_participant(request.app.state, manifest, kind, tombstone_owner, frozen['snapshot'],
                        lifecycle=account_lifecycle, operation_id=operation_id, claim_token=claim_token, started=previously_started, step=step)
                    _checkpoint(step, receipt)
                    if kind == 'fm':
                        verified_step = 'memory_fm_purged_verified'
                        if not _applied(verified_step):
                            _start_step(verified_step)
                            _, canonical = _memory_participants_current(request.app.state, manifest)
                            await _purge_memory_participant(request.app.state,manifest,'fm',tombstone_owner,frozen['snapshot'],
                                lifecycle=account_lifecycle,operation_id=operation_id,claim_token=claim_token,
                                started=True,step=step,cleanup_receipt=receipt)
                            after = await canonical.owner_stats(owner=tombstone_owner)
                            if (_semantic_inventory_present(after) or after.get('fingerprint') != receipt['result']['source_after_fingerprint']):
                                raise RuntimeError('retained memory purge remains incomplete')
                            _checkpoint(verified_step, {'receipt_id':receipt['receipt_id'], 'receipt_hash':receipt['receipt_hash']})
            except Exception as exc:
                _fail_operation(exc)
                raise HTTPException(500, 'User deleted; retained memory purge remains pending') from exc
        elif memory_tombstoned and not _applied("memory_purged"):
            try:
                _start_step("memory_purged")
                receipt = await _memory_owner_transition(memory_provider, action='purge', owner=tombstone_owner, lifecycle=account_lifecycle, operation_id=operation_id, claim_token=claim_token, state=request.app.state)
                _checkpoint("memory_purged", receipt)
            except Exception as exc:
                _fail_operation(exc)
                logger.exception("Deleted user memory remains tombstoned and inaccessible")
                raise HTTPException(500, f"User deleted; tombstoned memory purge failed: {exc}") from exc
        if lifecycle_tombstoned and not _applied("lifecycle_purged"):
            try:
                _start_step("lifecycle_purged")
                receipt = account_lifecycle.purge_owner(tombstone_owner, subject_id=(operation or {}).get('subject_id'),
                    expected_media_inventory=(operation.get('manifest') or {}).get('media_artifacts'))
                _checkpoint(
                    "lifecycle_purged",
                    dict(receipt.stores) if durable else True,
                )
            except Exception as exc:
                _fail_operation(exc)
                logger.exception(
                    "Deleted user normalized state remains tombstoned and inaccessible"
                )
                raise HTTPException(
                    500,
                    "User deleted; tombstoned normalized-state purge failed "
                    f"for {tombstone_owner}: {exc}",
                ) from exc
        if durable and (
            not _applied("supervisor_purged")
            and mimo_supervisor is not None
            and callable(getattr(mimo_supervisor, "purge_owner_lifecycle", None))
        ):
            try:
                _start_step("supervisor_purged")
                receipt = await mimo_supervisor.purge_owner_lifecycle(
                    tombstone_owner,
                    expected=_tombstone_inventory(steps, "mimo_tombstoned", "target", tombstone_owner),
                )
                _checkpoint("supervisor_purged", receipt)
            except Exception as exc:
                _fail_operation(exc)
                raise HTTPException(500, f"User deleted; Open Clank agent state purge failed: {exc}") from exc
        if not _applied("filesystem_registry_purged"):
            try:
                _start_step("filesystem_registry_purged")
                from src.openclank.filesystem_registry import FilesystemRootRegistry
                filesystem_registry = filesystem_registry or FilesystemRootRegistry()
                purge_filesystem_owner = tombstone_owner if durable else target_owner
                receipt = filesystem_registry.purge_owner_lifecycle(
                    purge_filesystem_owner,
                    expected=(
                        (operation.get("manifest") or {})[
                            "filesystem_registry_tombstoned"
                        ]
                    )["source"] if durable else None,
                )
                _checkpoint("filesystem_registry_purged", receipt)
            except Exception as exc:
                _fail_operation(exc)
                raise HTTPException(500, f"User deleted; filesystem access state purge failed: {exc}") from exc
        if target_subject_id and not _applied("file_policy_purged"):
            try:
                _start_step("file_policy_purged")
                from src.openclank.file_policy import FilePolicyRepository
                receipt = FilePolicyRepository().purge_subject(
                    target_subject_id,
                    actor_subject_id=actor_subject_id,
                )
                _checkpoint("file_policy_purged", receipt)
            except Exception as exc:
                _fail_operation(exc)
                raise HTTPException(500, f"User deleted; canonical file-policy purge failed: {exc}") from exc
        if durable and not _applied("closure_verified"):
            try:
                _start_step("closure_verified")
                remaining = {
                    "task_runtime_source": (
                        scheduler.owner_runtime_inventory(target_owner)["count"]
                        if scheduler is not None else 0
                    ),
                    "task_runtime_tombstone": (
                        scheduler.owner_runtime_inventory(tombstone_owner)["count"]
                        if scheduler is not None else 0
                    ),
                    "normalized_source": account_lifecycle.owner_inventory(target_owner)["count"],
                    "normalized_tombstone": account_lifecycle.owner_inventory(tombstone_owner)["count"],
                    "sql_source": sql_store.owner_inventory(target_owner, account_id=target_subject_id)["count"],
                    "sql_tombstone": sql_store.owner_inventory(tombstone_owner, account_id=target_subject_id)["count"],
                    "files_source": file_store.owner_inventory(target_owner)["count"],
                    "files_tombstone": file_store.owner_inventory(tombstone_owner)["count"],
                    "skills_source": _account_skills_domain_store(
                        request
                    ).preview_owner_purge(target_owner)["count"],
                    "skills_tombstone": _account_skills_domain_store(
                        request
                    ).preview_owner_purge(tombstone_owner)["count"],
                    "shell_audit_source": _account_shell_audit_store().owner_inventory(
                        target_owner
                    )["count"],
                    "shell_audit_tombstone": _account_shell_audit_store().owner_inventory(
                        tombstone_owner
                    )["count"],
                    "ancillary_source": ancillary_store.owner_inventory(target_owner,account_id=(operation or {}).get('subject_id'))["count"],
                    "ancillary_tombstone": ancillary_store.owner_inventory(tombstone_owner,account_id=(operation or {}).get('subject_id'))["count"],
                    "uploads_source": (
                        upload_handler.owner_inventory(target_owner)["count"]
                        if upload_handler is not None else 0
                    ),
                    "uploads_tombstone": (
                        upload_handler.owner_inventory(tombstone_owner)["count"]
                        if upload_handler is not None else 0
                    ),
                    "personal_source": personal_store.owner_inventory(target_owner)["count"],
                    "personal_tombstone": personal_store.owner_inventory(tombstone_owner)["count"],
                    "media_source": media_store.preview_owner_purge(target_owner)["count"],
                    "media_tombstone": media_store.preview_owner_purge(tombstone_owner)["count"],
                    "staging_source": batch_store.preview_owner_staging(target_owner,guarded=typed_memory)["count"],
                    "staging_tombstone": batch_store.preview_owner_staging(tombstone_owner,guarded=typed_memory)["count"],
                    "filesystem_source": filesystem_registry.owner_inventory(target_owner)["count"],
                    "filesystem_tombstone": filesystem_registry.owner_inventory(tombstone_owner)["count"],
                    "mimo_source": (
                        mimo_supervisor.owner_lifecycle_inventory(target_owner)["count"]
                        if callable(getattr(mimo_supervisor, "owner_lifecycle_inventory", None))
                        else 0
                    ),
                    "mimo_tombstone": (
                        mimo_supervisor.owner_lifecycle_inventory(tombstone_owner)["count"]
                        if callable(getattr(mimo_supervisor, "owner_lifecycle_inventory", None))
                        else 0
                    ),
                    "research_source": (
                        research_handler.owner_inventory(target_owner)["count"]
                        if callable(getattr(research_handler, "owner_inventory", None))
                        else 0
                    ),
                    "research_tombstone": (
                        research_handler.owner_inventory(tombstone_owner)["count"]
                        if callable(getattr(research_handler, "owner_inventory", None))
                        else 0
                    ),
                }
                if typed_memory:
                    selected, canonical = _memory_participants_current(request.app.state, (operation.get('manifest') or {})['memory_participants'])
                    fm_snapshot = (operation.get('manifest') or {}).get('memory_fm_tombstoned') or {}
                    forward = (steps.get(f"memory_fm_forward_{fm_snapshot.get('generation')}") or {}).get('receipt') or {}
                    purge = (steps.get('memory_fm_purged') or {}).get('receipt') or {}
                    expected = {target_owner: (forward.get('result') or {}).get('source_after_fingerprint'),
                                tombstone_owner: (purge.get('result') or {}).get('source_after_fingerprint')}
                    for checked_owner in (target_owner, tombstone_owner):
                        current = await canonical.owner_stats(owner=checked_owner)
                        if ((joined_memory and selected.lifecycle_snapshot(checked_owner)['count'])
                                or _semantic_inventory_present(current) or not expected[checked_owner]
                                or current.get('fingerprint') != expected[checked_owner]):
                            raise RuntimeError('retained memory owner remains after deletion')
                if any(int(value) for value in remaining.values()):
                    raise RuntimeError("account lifecycle verification found retained owner state")
                _checkpoint("closure_verified", {"remaining": remaining})
            except Exception as exc:
                _fail_operation(exc)
                raise HTTPException(500, "User deleted; lifecycle verification failed") from exc
        # delete_user removes the user's ApiToken rows, but the bearer-auth
        # middleware serves from an in-memory prefix->token cache that only
        # rebuilds when flagged dirty. Without this, a deleted user's already
        # cached token keeps authenticating until some other token op or a
        # restart clears the cache. Mirror what the token routes do.
        _invalidate_account_runtime(request, target_owner, tombstone_owner)
        release_research = getattr(research_handler, "release_owner_fence", None)
        if callable(release_research):
            release_research(target_owner)
        _checkpoint("token_cache_invalidated", True, complete=True)
        result = {"ok": True}
        if durable:
            result["operation_id"] = operation_id
        await _sync_history_accounts(request)
        return result

    @router.get("/account-operations")
    async def list_account_operations(request: Request):
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        list_active = getattr(account_lifecycle, "list_active_operations", None)
        if not callable(list_active):
            raise HTTPException(503, "Account lifecycle operations are unavailable")
        return {"operations": list_active()}

    @router.get("/account-operations/{operation_id}")
    async def get_account_operation(operation_id: str, request: Request):
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        if account_lifecycle is None or not callable(
            getattr(account_lifecycle, "get_operation", None)
        ):
            raise HTTPException(503, "Account lifecycle operations are unavailable")
        try:
            return account_lifecycle.get_operation(operation_id)
        except Exception as exc:
            raise HTTPException(404, "Account lifecycle operation not found") from exc

    @router.post("/account-operations/{operation_id}/resume")
    async def resume_account_operation(
        operation_id: str,
        request: Request,
        force_foreign_takeover: bool = False,
    ):
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        if account_lifecycle is None or not callable(
            getattr(account_lifecycle, "get_operation", None)
        ):
            raise HTTPException(503, "Account lifecycle operations are unavailable")
        try:
            operation = account_lifecycle.get_operation(operation_id)
        except Exception as exc:
            raise HTTPException(404, "Account lifecycle operation not found") from exc
        if operation.get("state") == "complete":
            return {"ok": True, "operation_id": operation_id, "resumed": False}
        if operation.get("state") == "aborted":
            raise HTTPException(409, "Aborted account lifecycle operations cannot be resumed")
        if operation.get("kind") == "delete":
            return await admin_delete_user(
                DeleteUserRequest(
                    username=str(operation.get("source_owner") or ""),
                    operation_id=operation_id,
                    force_foreign_takeover=force_foreign_takeover,
                ),
                request,
            )
        if operation.get("kind") == "rename":
            return await rename_user(
                str(operation.get("source_owner") or ""),
                RenameUserRequest(
                    username=str(operation.get("target_owner") or ""),
                    operation_id=operation_id,
                    force_foreign_takeover=force_foreign_takeover,
                ),
                request,
            )
        raise HTTPException(409, "Unsupported account lifecycle operation")

    # ---- Feature visibility (admin-managed) ----

    @router.get("/features")
    async def get_features():
        """Public: returns which UI features are enabled."""
        return _load_features()

    @router.post("/features")
    async def set_features(request: Request):
        """Admin only: update feature toggles."""
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        body = await request.json()
        current = _load_features()
        for key in current:
            if key in body and isinstance(body[key], bool):
                current[key] = body[key]
        _save_features(current)
        return current

    # ---- App settings (admin-managed) ----

    @router.get("/settings")
    async def get_settings(request: Request, session_id: str | None = None):
        """Return global policy with caller-owned model selections."""
        user = _get_current_user(request)
        if not user:
            # Login needs public feature/policy endpoints, not app preferences.
            return {}
        settings = _load_settings()
        if user:
            settings = _settings_for_user(settings, user)
            supervisor = getattr(getattr(request.app, "state", None), "mimo_supervisor", None)
            # Effective native values require an explicit owned chat target.
            # Rendering Settings never resolves process.cwd or cold-admits a worker.
            settings["agent_settings_effective"] = {"status": "pending", "reason": "TARGET_REQUIRED"}
            if session_id:
                from routes.session_routes import _verify_session_owner
                from src.openclank.chat_routing import resolve_chat_route, ChatRouteUnavailable

                _verify_session_owner(request, session_id)
                manager = getattr(request.app.state, "session_manager", None)
                try:
                    session = manager.get_session(session_id) if manager is not None else None
                    if session is None: raise KeyError(session_id)
                except KeyError as exc:
                    raise HTTPException(404, "Session is unavailable") from exc
                try:
                    route = resolve_chat_route(owner=user, endpoint_id=session.endpoint_id, model_route_id=session.provider_model_route_id)
                except ChatRouteUnavailable:
                    route = None
                if route is None:
                    settings["agent_settings_effective"] = {"status": "unavailable", "reason": "CHAT_ROUTE_UNAVAILABLE"}
                elif supervisor is None:
                    settings["agent_settings_effective"] = {"status": "unavailable", "reason": "SUPERVISOR_UNAVAILABLE"}
                else:
                    target = {"owner": user, "session_id": session_id, "workspace_id": session.workspace_id, "model_route_id": session.provider_model_route_id}
                    try:
                        value = await supervisor.ready_effective_agent_settings(user, route.connection_id, route.provider_model_id, session_id=session_id)
                        settings["agent_settings_effective"] = {"status": "available", "value": value, "target": target, "apply": "next_admission"}
                    except Exception as exc:
                        code = str(getattr(exc, "code", "") or "")
                        if code not in {"SUPERVISOR_UNAVAILABLE", "MODEL_NOT_PROJECTED", "SESSION_NOT_READY", "OWNER_REVOKED"}:
                            code = "ENGINE_READBACK_UNAVAILABLE"
                        settings["agent_settings_effective"] = {"status": "unavailable", "reason": code, "target": target}
        else:
            # The route is intentionally readable before login for UI boot.
            # Never expose the operator's selected endpoints/models there.
            settings = _settings_for_user(settings, "")
        # Return the versioned endpoint envelope while retaining invalid legacy
        # rows in the read projection so users can repair them in Settings.
        try:
            settings["reminder_endpoints"] = normalize_endpoints(
                settings.get("reminder_endpoints"), allow_invalid=True
            )
        except ReminderEndpointError:
            # A malformed historical value must remain inspectable and must not
            # make the whole settings page disappear.
            settings["reminder_endpoints"] = {
                "version": 1,
                "endpoints": [],
                "errors": ["Saved reminder endpoints need repair"],
            }
        settings["code_index_scheduler"] = await code_index_settings_readback(
            getattr(request.app.state, "memory_provider", None), settings
        )
        if user and auth_manager.is_admin(user):
            return settings
        return scrub_settings(settings)

    @router.post("/settings")
    async def set_settings(request: Request):
        """Save caller model choices; only admins may change global policy."""
        user = _get_current_user(request)
        if not user:
            raise HTTPException(403, "Admin only")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Settings must be an object")
        if CODE_INDEX_REVISION_KEY in body or "code_index_scheduler" in body:
            raise HTTPException(400, "Indexing scheduler readback and revision are read-only")
        current = _load_settings()
        model_update = {
            key: body[key]
            for key in PER_USER_MODEL_SETTING_KEYS
            if key in body
        }
        if "agent_settings" in body:
            try:
                model_update["agent_settings"] = validate_agent_settings(body["agent_settings"])
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        if "agent_settings" in model_update:
            from src.openclank.chat_routing import resolve_chat_route, ChatRouteUnavailable
            from src.agent_settings import model_route_references
            for path, selector in model_route_references(model_update["agent_settings"]):
                try: resolve_chat_route(owner=user, model_route_id=selector)
                except ChatRouteUnavailable as exc: raise HTTPException(400, f"{path} is unavailable to this owner") from exc
        if "reminder_endpoints" in model_update:
            try:
                model_update["reminder_endpoints"] = normalize_endpoints(
                    model_update["reminder_endpoints"]
                )
            except ReminderEndpointError as exc:
                # Reject before _save_for_user/_save_settings so an invalid
                # draft cannot replace the last known-good configuration.
                raise HTTPException(400, str(exc)) from exc
            _validate_reminder_endpoints(model_update["reminder_endpoints"], user or "")
        global_update = {
            key: body[key]
            for key in DEFAULT_SETTINGS
            if key in body and key not in PER_USER_MODEL_SETTING_KEYS
        }
        if "memory_graph" in body:
            global_update["memory_graph"] = body["memory_graph"]
        if user and global_update and not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        if "memory_graph" in global_update:
            from src.openclank.memory_policy import _graph_projection
            graph = _graph_projection({"state":"available", "value":global_update["memory_graph"]})
            if graph.get("state") != "available" or graph.get("unknown_fields_present") is True:
                raise HTTPException(400, "Invalid memory graph settings")

        scoped = _settings_for_user(current, user) if user else current
        _validate_model_settings_update(model_update, scoped, request, user or "")
        # Per-key validation for numeric settings: coerce to int and clamp to a
        # sane range so a bad value can't disable the agent or let it run away.
        _INT_RANGES = {
            "agent_max_rounds": (1, 200),
            "agent_max_tool_calls": (0, 1000),  # 0 = unlimited
        }
        for key, val in global_update.items():
            if key in _INT_RANGES:
                lo, hi = _INT_RANGES[key]
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{key} must be an integer")
                val = max(lo, min(val, hi))
            global_update[key] = val

        # Reload under one synchronous lock, so concurrent global patches cannot
        # overwrite each other or reuse a scheduler revision. No await or native
        # request is allowed while this lock is held. Preparation never mutates
        # load_settings' shared cached dict, including rejected/failed saves.
        from src.settings import _read_persisted_settings, settings_policy_guard
        # Reject deterministic settings errors before taking a durable policy fence.
        with SETTINGS_LOCK:
            try:
                prepare_code_index_settings(_load_settings(), global_update)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        prepare_user_settings = current_account = None
        before_preferences = proposed_preferences = None
        if user:
            account_id = auth_manager.account_id(user)
            if not account_id:
                raise HTTPException(409, "Account identity is unavailable; retry")
            def current_account():
                try:
                    return (_get_current_user(request) == user and auth_manager.account_id(user) == account_id
                        and auth_manager.username_for_account_id(account_id) == user
                        and (not global_update or auth_manager.is_admin(user)))
                except Exception:
                    return False
        if user and (model_update or "agent_settings_reset" in body):
            def prepare_user_settings(prefs):
                updates = dict(model_update)
                incoming=body.get("agent_settings")
                resets=body.get("agent_settings_reset")
                distill_write=(isinstance(incoming,dict) and "distill" in incoming) or (
                    isinstance(resets,list) and any(isinstance(path,str) and path.startswith("distill.") for path in resets))
                prior=prefs.get("agent_settings", {})
                if distill_write and not (type(prefs.get('_skill_draft_policy_version')) is int and prefs['_skill_draft_policy_version']==1):
                    prior=dict(prior) if isinstance(prior,dict) else {}
                    prior.pop('distill',None)
                if "agent_settings" in body or "agent_settings_reset" in body:
                    revision = agent_settings_revision(prefs.get("agent_settings"))
                    expected = body.get("agent_settings_expected_revision")
                    if expected is not None and expected != revision:
                        raise HTTPException(409, {"code": "AGENT_SETTINGS_CONFLICT", "revision": revision})
                    if "agent_settings" in body and body["agent_settings"] is None:
                        if body.get("agent_settings_reset"):
                            raise HTTPException(400, "Whole override reset cannot be combined with field resets")
                        updates.pop("agent_settings", None)
                        prefs.pop("agent_settings", None)
                        prefs.pop('_skill_draft_policy_version',None)
                    else:
                        try:
                            merged = patch_agent_settings(prior, body.get("agent_settings", {}), reset=body.get("agent_settings_reset"))
                        except ValueError as exc:
                            raise HTTPException(400, str(exc)) from exc
                        if merged: updates["agent_settings"] = merged
                        else:
                            updates.pop("agent_settings", None)
                            prefs.pop("agent_settings", None)
                        if distill_write:
                            if isinstance(merged.get('distill'),dict): prefs['_skill_draft_policy_version']=1
                            else: prefs.pop('_skill_draft_policy_version',None)
                prefs.update(updates)
                return prefs, None
            from routes.prefs_routes import _protected_preferences
            with PREFERENCE_LOCK:
                before_preferences = copy.deepcopy(_load_for_user(user))
                proposed_preferences, _ = prepare_user_settings(copy.deepcopy(before_preferences))
                proposed_preferences = _protected_preferences(before_preferences, proposed_preferences,
                    allow_legacy_memory=True, allow_skill_draft_policy=True)
                if current_account() is not True:
                    raise HTTPException(409, "Account identity changed; retry")
        try:
            with settings_policy_guard(global_update, user=user, before_preferences=before_preferences,
                                       proposed_preferences=proposed_preferences, before_save=current_account):
                try:
                    base = dict(_load_settings())
                    if 'memory_graph' not in global_update:
                        persisted = _read_persisted_settings()
                        if 'memory_graph' in persisted: base['memory_graph'] = persisted['memory_graph']
                        else: base.pop('memory_graph', None)
                    current = prepare_code_index_settings(base, global_update)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
                if user:
                    if model_update or "agent_settings_reset" in body:
                        try:
                            _mutate_user(user, prepare_user_settings, allow_skill_draft_policy=True, before_save=current_account)
                        except HTTPException:
                            raise
                        except ValueError as exc:
                            raise HTTPException(409, "Settings or account changed; retry") from exc
                    if global_update:
                        if current_account is None or current_account() is not True:
                            raise HTTPException(409, "Settings account or administrator authority changed")
                        _save_settings(current, allow_memory_graph='memory_graph' in global_update)
                    result = _settings_for_user(current, user)
                else:
                    # Historical explicit auth-disabled single-user behavior.
                    if "agent_settings" in body and body["agent_settings"] is None:
                        model_update.pop("agent_settings", None)
                        current.pop("agent_settings", None)
                    current.update(model_update)
                    _save_settings(current, allow_memory_graph='memory_graph' in global_update)
                    result = current
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(409, {"code":"POLICY_TRANSITION_UNCERTAIN",
                "message":"Settings update did not complete; some values may have changed. Review current settings before taking further action."}) from exc
        result["code_index_scheduler"] = await code_index_settings_readback(
            getattr(request.app.state, "memory_provider", None), current,
            apply=any(key in global_update for key in CODE_INDEX_MANAGED_KEYS),
        )
        return result if user and auth_manager.is_admin(user) else scrub_settings(result)

    # ---- Integrations CRUD ----


    @router.get("/integrations")
    async def list_integrations_route(request: Request):
        """List all integrations (admin only, keys masked)."""
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        items = load_integrations()
        # Mask API keys for frontend display
        safe = [mask_integration_secret(item) for item in items]
        return {"integrations": safe}

    @router.get("/integrations/presets")
    async def list_presets():
        """List available integration presets."""
        return {"presets": {k: {kk: vv for kk, vv in v.items() if kk != "api_key"} for k, v in INTEGRATION_PRESETS.items()}}

    @router.post("/integrations")
    async def create_integration(request: Request):
        """Create a new integration (admin only)."""
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        body = await request.json()
        item = add_integration(body)
        return {"ok": True, "integration": mask_integration_secret(item)}

    @router.put("/integrations/{integration_id}")
    async def update_integration_route(integration_id: str, request: Request):
        """Update an existing integration (admin only)."""
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        body = await request.json()
        item = update_integration(integration_id, body)
        if not item:
            raise HTTPException(404, "Integration not found")
        return {"ok": True, "integration": mask_integration_secret(item)}

    @router.delete("/integrations/{integration_id}")
    async def delete_integration_route(integration_id: str, request: Request):
        """Delete an integration (admin only)."""
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        ok = delete_integration(integration_id)
        if not ok:
            raise HTTPException(404, "Integration not found")
        return {"ok": True}

    @router.post("/integrations/{integration_id}/test")
    async def test_integration_route(integration_id: str, request: Request):
        """Test connectivity to an integration (admin only)."""
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        integ = get_integration(integration_id)
        if not integ:
            raise HTTPException(404, "Integration not found")
        preset = (integ.get("preset") or integ.get("name", "")).lower()

        # ntfy is special: a GET / proves the server is reachable but
        # publishes nothing, so the user has no way to know whether
        # subscribers will actually receive notifications. Instead, do
        # the real thing — POST a one-line "connectivity test" message
        # to the topic the Reminders panel is configured to use. If the
        # subscriber app is wired up correctly, this is what the green
        # checkmark + a phone ping confirms together.
        if preset == "ntfy":
            import httpx
            from urllib.parse import urlparse
            # Strip any path/query the user accidentally pasted in the
            # base URL (e.g. `http://host:8091/odysseus`) — otherwise
            # the topic gets appended after the path and we publish to
            # `/odysseus/odysseus` (which ntfy 404s on). ntfy itself
            # only ever serves from the root.
            raw_base = (integ.get("base_url") or "").strip()
            parsed = urlparse(raw_base)
            base = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else raw_base.rstrip("/")
            settings = _load_settings()
            topic = (settings.get("reminder_ntfy_topic") or "reminders").strip() or "reminders"
            full_url = f"{base}/{topic}"
            api_key = integ.get("api_key", "")
            auth_type = (integ.get("auth_type") or "none").lower()
            headers = {
                "Title": "Open Clank connectivity test",
                "Tags": "white_check_mark",
                "Priority": "default",
            }
            if api_key:
                if auth_type == "bearer":
                    headers["Authorization"] = f"Bearer {api_key}"
                elif auth_type == "header":
                    headers[integ.get("auth_header") or "Authorization"] = api_key
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    r = await client.post(
                        full_url,
                        content="Connectivity test from Open Clank. If you see this on your phone, ntfy is wired up correctly.",
                        headers=headers,
                    )
                if r.is_success:
                    # Tell the user EXACTLY where it went and what to
                    # subscribe to on their phone, so they can match
                    # without guesswork. The doubled-topic / wrong-host
                    # mistakes are easier to spot when the actual URL
                    # is right there in the success line.
                    return {
                        "ok": True,
                        "message": (
                            f"Sent to {full_url} — on your ntfy app, "
                            f"subscribe to topic \"{topic}\" with server "
                            f"\"{base}\" (or paste the full URL: {full_url})."
                        ),
                    }
                return {"ok": False, "message": f"ntfy returned HTTP {r.status_code} from {full_url}: {r.text[:200]}"}
            except Exception as e:
                hint = ""
                if parsed.hostname not in ("127.0.0.1", "localhost"):
                    hint = " If this is Docker Compose ntfy, set NTFY_BIND to that host/Tailscale IP and NTFY_BASE_URL to the same server URL in .env, then recreate ntfy."
                return {"ok": False, "message": f"ntfy publish to {full_url} failed: {e}.{hint}"[:500]}

        if preset == "discord_webhook":
            import httpx
            webhook_url = (integ.get("base_url") or "").strip()
            if not webhook_url:
                return {"ok": False, "message": "No webhook URL set — paste the full Discord webhook URL into the Base URL field."}
            payload = {
                "embeds": [{
                    "title": "Open Clank connectivity test",
                    "description": "If you see this, your Discord Webhook integration is wired up correctly.",
                    "color": 5793266,
                }]
            }
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    r = await client.post(webhook_url, json=payload)
                if r.is_success:
                    return {"ok": True, "message": "Test embed sent — check your Discord channel to confirm it arrived."}
                return {"ok": False, "message": f"Discord returned HTTP {r.status_code}: {r.text[:200]}"}
            except Exception as e:
                return {"ok": False, "message": f"Request failed: {e}"[:400]}

        # All other presets: GET against a known health endpoint.
        # Fall back to detecting from name if preset is missing.
        health_paths = {
            "miniflux": "/v1/me",
            "gitea": "/api/v1/version",
            "linkding": "/api/tags/",
            "homeassistant": "/api/",
            "home assistant": "/api/",
        }
        path = health_paths.get(preset, "/")
        result = await execute_api_call(integration_id, "GET", path)
        if result.get("exit_code", 1) == 0:
            return {"ok": True, "message": "Connection successful"}
        return {"ok": False, "message": (result.get("error") or "Connection failed")[:300]}

    return router
