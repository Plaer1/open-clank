"""Read-only owner-qualified managed orchestration routes."""
from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import effective_user
from src.openclank.chat_routing import ChatRouteUnavailable, resolve_chat_route
from src.openclank.orchestration_projection import (
    ProjectionError, build_projection, decode_cursor, encode_cursor, resolve_binding,
    find_session_actor, sanitize_actor, validate_actors, validate_tasks,
    TaskObservationScope, project_task_observations, decode_task_observation_cursor,
)
from src.openclank.session_map import OwnerSessionMap
from src.openclank.attachment_inventory import input_receipt_attempt_prefix
from src.openclank.attachment_inventory import input_receipt_claim_scope
from src.openclank.user_input_receipts import InputRequestError, handoff_root_operation_id, materialize_upload_content, native_admission, native_handoff, normalize_admission, same_client_receipt, should_resume, unsupported_receipt_policy


def effective_receipt_policy_ceiling(request, owner, host_chat_id, value, session_manager, *, theme_status_only=False, requested_tools=None, recall_expected_workspace=None):
    """Resolve admission evidence from current host authority plus frozen choices."""
    from src.tool_policy import conservative_legacy_turn_policy, resolve_turn_policy
    from src.settings import get_setting
    from src.tool_security import SCOPED_FILE_TOOLS, unavailable_strict_agent_tools
    if requested_tools is not None and (type(requested_tools) is not frozenset
            or requested_tools != frozenset({'recall_memory'}) or not requested_tools.isdisjoint(SCOPED_FILE_TOOLS)):
        raise ValueError("unsupported policy projection")
    auth = getattr(request.app.state, "auth_manager", None)
    try:
        privileges = auth.get_privileges(owner) if auth else {}
    except Exception as exc:
        raise HTTPException(503, {"code": "POLICY_AUTHORITY_UNAVAILABLE", "message": "Current account privileges are unavailable"}) from exc
    privileges = privileges or {}
    if not privileges.get("can_use_agent", True):
        raise HTTPException(403, "Agent access is disabled for this account.")
    if requested_tools is not None:
        # Recall needs current durable metadata, not a mutable, hydrated history cache.
        from core.database import SessionLocal, Session as DbSession
        from src.openclank.conversation_archive import get_conversation_archive
        from src.openclank.session_operations import visibility
        if type(recall_expected_workspace) is not str or auth is None:
            raise ValueError("recall session authority unavailable")
        with SessionLocal() as db:
            row = db.query(DbSession.owner, DbSession.workspace_id, DbSession.mimo_state).filter(DbSession.id == host_chat_id).first()
        account = auth.account_id(owner)
        if (not row or not account or not row[0] or auth.account_id(row[0]) != account
                or auth.username_for_account_id(account) != owner
                or (row[1] is None and recall_expected_workspace != '')
                or (row[1] is not None and (type(row[1]) is not str or not row[1] or row[1] != recall_expected_workspace))):
            raise ValueError("recall session authority unavailable")
        visibility(row[2], require_settled=False)
        if get_conversation_archive().chat_erasure_started(owner=owner, chat_id=host_chat_id):
            raise ValueError("recall session authority unavailable")
        workspace_id = row[1] if row[1] is not None else ''
    else:
        get_host_session = getattr(session_manager, "get_session", None)
        session_obj = get_host_session(host_chat_id, allow_pending_history=True) if callable(get_host_session) else None
        workspace_id = str(getattr(session_obj, "workspace_id", "") or "")
    workspace = ""
    if workspace_id:
        from routes.session_routes import _validated_session_workspace
        try:
            workspace = _validated_session_workspace(request, owner, workspace_id).path
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, {"code": "POLICY_AUTHORITY_UNAVAILABLE", "message": "Current workspace authority is unavailable"}) from exc
    disabled = (set(unavailable_strict_agent_tools(owner, workspace, workspace_id=workspace_id, chat_id=host_chat_id))
                if requested_tools is None else set())
    if auth is None:
        # Test/minimal hosts without account authority remain safe by
        # recording no available tools in the admission ceiling.
        from src.tool_policy import known_tool_names
        disabled.update(known_tool_names())
    if not privileges.get("can_use_bash", True):
        disabled.update({"bash", "python"})
    if not privileges.get("can_use_browser", True):
        disabled.update({"builtin_browser", "browser_status", "browser_observe", "browser_control"})
    if not privileges.get("can_use_documents", True):
        disabled.update({"create_document", "edit_document", "update_document", "suggest_document"})
    if not privileges.get("can_manage_memory", True):
        disabled.update({"manage_memory", "recall_memory", "manage_skills", "workflow_artifact"})
    if not privileges.get("can_generate_images", True):
        disabled.add("generate_image")
    if not privileges.get("can_use_research", True):
        disabled.update({"manage_research", "trigger_research"})
    global_disabled = get_setting("disabled_tools", [])
    if isinstance(global_disabled, list):
        disabled.update(item for item in global_disabled if isinstance(item, str))
    choices = value.get("turnPolicy") or conservative_legacy_turn_policy()
    try:
        from routes.chat_routes import _agent_memory_read_allowed, _load_prefs_for_user
        prefs = _load_prefs_for_user(owner) or {}
        memory_allowed = _agent_memory_read_allowed(prefs, incognito=choices["incognito"], no_memory=choices["noMemory"])
        resolved = resolve_turn_policy(choices, disabled_tools=disabled, last_user_message=value.get("text", ""), memory_read_allowed=memory_allowed, theme_status_only=theme_status_only)
    except Exception as exc:
        raise HTTPException(503, {"code": "POLICY_AUTHORITY_UNAVAILABLE", "message": "Current tool policy could not be resolved"}) from exc
    policy = resolved.policy
    disabled_result = set(policy.disabled_tools)
    hidden_result = set(policy.hidden_tools)
    allowed_result = set(resolved.allowed_tools) if resolved.allowed_tools is not None else None
    if requested_tools is not None:
        disabled_result &= requested_tools
        hidden_result &= requested_tools
        if allowed_result is not None:
            allowed_result &= requested_tools
    return {
        "version": 1,
        "blockAllToolCalls": bool(policy.block_all_tool_calls or auth is None),
        "disabledTools": sorted(disabled_result),
        "hiddenTools": sorted(hidden_result),
        "allowedTools": [] if auth is None else (sorted(allowed_result) if allowed_result is not None else None),
        **({"projectionTools": sorted(requested_tools)} if requested_tools is not None else {}),
    }

def install_input_policy_resolver(request, bridge, session_manager):

    async def native_projection(identity, entry, current, projection):
        from src.openclank.acp_bridge import _mimo_tool_policy
        from src.tool_security import brokered_agent_file_tools
        root = current.get('rootSessionID')
        binding = await bridge._handle_session_binding_read({'sessionID': root})
        owner, chat, workspace_id, cwd = current.get('owner'), current.get('stableChatID'), current.get('authorityWorkspaceID'), binding.get('canonicalCwd')
        from pathlib import Path
        import os
        from src.constants import APP_DB
        auth, account = getattr(request.app.state, 'auth_manager', None), entry.get('ownerAccountID')
        repository = getattr(request.app.state, 'files_policy_repository', None)
        admission, barrier, credential = entry.get('ownerAdmission'), entry.get('ownerAdmissionBarrier'), entry.get('originalCredentialCurrent')
        if (not owner or not auth or not account or not repository or not admission or not barrier or not callable(credential)
                or getattr(admission, 'released', True) or entry.get('ownerAdmissionAccount') != account
                or not credential() or not await barrier.is_live(owner, admission)
                or Path(repository.db_path).resolve() != Path(os.environ.get('OPEN_CLANK_AUTHORITY_DB_PATH') or APP_DB).resolve()
                or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                or current.get('holderID') != entry.get('holderID') or current.get('holderID') != bridge._managed_provider_generation
                or not chat or type(workspace_id) is not str or not isinstance(cwd, str) or not cwd
                or binding.get('owner') != owner or binding.get('stableChatID') != chat
                or binding.get('authorityWorkspaceID') != workspace_id or binding.get('transition')):
            return None
        generation = repository.generation()
        brokered = await asyncio.to_thread(brokered_agent_file_tools, owner, cwd, workspace_id=workspace_id, chat_id=chat) if workspace_id else ()
        again = await bridge.resolve_managed_actor_execution(identity['sessionID'], identity['rootOperationID'], identity['actorID'], identity['executionRevision'])
        if (again != current or await bridge._handle_session_binding_read({'sessionID': root}) != binding
                or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                or getattr(request.app.state, 'files_policy_repository', None) is not repository or repository.generation() != generation
                or entry.get('ownerAdmission') is not admission or entry.get('ownerAdmissionBarrier') is not barrier
                or entry.get('originalCredentialCurrent') is not credential or not credential() or not await barrier.is_live(owner, admission)
                or bridge._managed_provider_generation != entry.get('holderID')
                or bridge._managed_actor_turns.get(bridge._managed_turn_key(identity)) is not entry or entry.get('state') != 'active'
                or entry.get('ownerAdmissionStopped') or getattr(bridge, '_ownerAdmissionHolderStopped', False)):
            return None
        if workspace_id == "":
            # A personal chat has an account memory scope and no Files authority.
            # Use the literal native tool name so a LifeTools wildcard cannot join it.
            allowed = projection["allowedTools"]
            denied = set(projection["disabledTools"]) | set(projection["hiddenTools"])
            recall = (not projection["blockAllToolCalls"] and "recall_memory" not in denied
                      and "memory" not in denied and (allowed is None or "recall_memory" in allowed))
            return ({"*": False, "memory": recall}, repository, generation)
        return (_mimo_tool_policy({'allowed_tools': [] if projection['blockAllToolCalls'] else projection['allowedTools'],
            'disabled_tools': sorted(set(projection['disabledTools']) | set(projection['hiddenTools'])),
            'brokered_file_tools': sorted(brokered)}), repository, generation)

    async def resolve(identity, receipts):
        from src.openclank.acp_bridge import _mimo_tool_policy
        from src.tool_policy import conservative_legacy_turn_policy
        key = bridge._managed_turn_key(identity)
        entry = bridge._managed_actor_turns.get(key)
        if not entry:
            raise ValueError("managed input policy turn is no longer active")
        current = await bridge.resolve_managed_actor_execution(
            identity["sessionID"], identity["rootOperationID"], identity["actorID"], identity["executionRevision"]
        )
        if current.get("authority"):
            raise ValueError("private sessions do not admit durable input receipts")
        # Optional source candidates are independent of permission decisions.
        # Failure never prevents the already-authorized steering operation.
        queued_ingress = {}
        try:
            from src.openclank.queued_source import match_queued_boundary
            from src.openclank.memory_policy import STORE
            import hashlib
            auth = request.app.state.auth_manager
            account = entry.get('ownerAccountID')
            privilege = bool((auth.get_privileges(current['owner']) or {}).get('can_manage_memory',True))
            if (account and auth.account_id(current['owner']) == account and auth.username_for_account_id(account) == current['owner']
                and identity['actorID'] == 'main' and identity['sessionID'] == current['rootSessionID']):
                for item in receipts:
                    proof = match_queued_boundary(item.get('queuedSourceProof'),owner=current['owner'],account_id=account,
                        chat_id=current['stableChatID'],session_id=identity['sessionID'],actor_id=identity['actorID'],
                        workspace_id=current['authorityWorkspaceID'],receipt=item)
                    if proof:
                        choices = proof['choices']
                        policy = STORE.effective_snapshot(current['owner'],read_privilege=privilege,write_privilege=privilege,
                            incognito=choices['incognito'],no_memory=choices['noMemory'],compare_mode=choices['compareMode'])
                        if policy.get('eligibility_state') == 'available' and policy.get('capture_allowed') is True:
                            queued_ingress[item['requestID']] = {'proof':proof,'proofSHA256':hashlib.sha256(item['queuedSourceProof'].encode('utf-8')).hexdigest(),
                                'payloadHash':item['payloadHash']}
        except Exception:
            queued_ingress = {}
        handoff = entry.get('queuedHandoff')
        if handoff is not None and (type(handoff) is not dict or not any(
            item.get('requestID') == handoff.get('requestID') and item.get('payloadHash') == handoff.get('payloadHash')
            and item.get('requestID') in queued_ingress for item in receipts)):
            queued_ingress = {}
        execution_ceiling = entry.get("rawPolicyCeiling")
        ceiling_choice = (execution_ceiling or {}).get("choiceSnapshot") or {}
        decisions = []
        allowed = None
        disabled, hidden = set(), set()
        block = False
        modes = set()
        for receipt in receipts:
            choices = receipt.get("turnPolicy") or conservative_legacy_turn_policy()
            ceiling = receipt.get("admissionCeiling")
            current_projection = effective_receipt_policy_ceiling(
                request, current["owner"], current["stableChatID"], {"turnPolicy": choices, "text": receipt["text"]}, session_manager
            )
            state, reason = "ready", None
            if (not ceiling or receipt["mapRevision"] > current["mapRevision"] or
                any(receipt[k] != current[k] for k in ("mappingRevision", "workspaceRevision"))):
                state, reason = "unavailable", "authority_changed"
            elif unsupported_receipt_policy(receipt.get("turnPolicy"), legacy=True):
                state, reason = "unavailable", unsupported_receipt_policy(receipt.get("turnPolicy"), legacy=True)
            elif ceiling_choice.get("noMemory"):
                state, reason = "unavailable", "No Memory follow-ups are unavailable in this existing actor context."
            elif choices.get("useResearch"):
                state, reason = "unavailable", "research_workflow_unavailable"
            ceiling = ceiling or {"blockAllToolCalls": True, "disabledTools": [], "hiddenTools": [], "allowedTools": []}
            receipt_allowed = current_projection["allowedTools"]
            ceiling_allowed = ceiling["allowedTools"]
            if receipt_allowed is None:
                receipt_allowed = ceiling_allowed
            elif ceiling_allowed is not None:
                receipt_allowed = sorted(set(receipt_allowed) & set(ceiling_allowed))
            if receipt_allowed is not None:
                allowed = set(receipt_allowed) if allowed is None else allowed & set(receipt_allowed)
            receipt_disabled = set(current_projection["disabledTools"]) | set(ceiling["disabledTools"])
            receipt_hidden = set(current_projection["hiddenTools"]) | set(ceiling["hiddenTools"])
            receipt_block = bool(current_projection["blockAllToolCalls"] or ceiling["blockAllToolCalls"])
            disabled.update(receipt_disabled); hidden.update(receipt_hidden); block |= receipt_block
            modes.add(choices["mode"])
            decision = {k: receipt[k] for k in ("requestID", "payloadHash", "executionRevision", "mapRevision", "mappingRevision", "workspaceRevision")}
            decision.update(state=state, choiceSnapshot=receipt.get("turnPolicy"), guideOnly=receipt_block,
                toolProjection={"blockAllToolCalls": receipt_block, "disabledTools": sorted(receipt_disabled), "hiddenTools": sorted(receipt_hidden), "allowedTools": receipt_allowed})
            if reason:
                decision["reason"] = reason
            decisions.append(decision)
        compatible = len(modes) == 1 and all(item["state"] == "ready" for item in decisions)
        no_memory = bool(ceiling_choice.get("noMemory") or any((item.get("turnPolicy") or {}).get("noMemory") for item in receipts))
        if compatible and no_memory:
            def pre_fence():
                from core.database import SessionLocal
                from src.openclank.chat_source_bridge import require_root_veto_source
                from src.openclank.conversation_archive import get_conversation_archive
                from src.openclank.transcript_projection import get_managed_binding
                if bridge._managed_actor_turns.get(key) is not entry:
                    raise ValueError("managed privacy turn changed")
                binding = get_managed_binding(current["stableChatID"], owner=current["owner"])
                if (binding.get("owner") != current["owner"] or binding.get("stableChatID") != current["stableChatID"]
                    or binding.get("engineSessionID") != current["rootSessionID"] or binding.get("transition") is not None
                    or binding.get("mappingRevision") != current["mappingRevision"]
                    or binding.get("workspaceRevision") != current["workspaceRevision"]):
                    raise ValueError("managed privacy binding changed")
                archive = get_conversation_archive()
                account = archive.resolve_owner(current["owner"])
                if not account:
                    raise ValueError("privacy owner unavailable")
                with SessionLocal() as db:
                    require_root_veto_source(db, archive, owner=account, chat_id=current["stableChatID"], root_operation_id=identity["rootOperationID"])
            def persist_veto():
                from core.database import SessionLocal
                from src.openclank.chat_source_bridge import veto_root_capture
                from src.openclank.conversation_archive import get_conversation_archive
                archive = get_conversation_archive()
                account = archive.resolve_owner(current["owner"])
                if not account:
                    raise ValueError("privacy owner unavailable")
                with SessionLocal() as db:
                    return veto_root_capture(db, archive, owner=account, chat_id=current["stableChatID"], root_operation_id=identity["rootOperationID"])
            try:
                from src.openclank.chat_source_bridge import guarded_source_mutation
                async with guarded_source_mutation(request, owner=current["owner"], chat_id=current["stableChatID"],
                        message_ids=[identity["rootOperationID"]], pre_fence=pre_fence, async_guard=bridge._managed_execution_lock):
                    await asyncio.to_thread(persist_veto)
                refreshed = await bridge.resolve_managed_actor_execution(identity["sessionID"], identity["rootOperationID"], identity["actorID"], identity["executionRevision"])
                if any(refreshed.get(key) != current.get(key) for key in ("owner", "stableChatID", "rootSessionID", "mappingRevision", "workspaceRevision")) or refreshed.get("mapRevision", -1) < current.get("mapRevision", 0):
                    raise ValueError("privacy authority changed")
            except Exception:
                compatible = False
                for decision in decisions:
                    decision["state"] = "unavailable"
                    decision["reason"] = "privacy_authority_unavailable"

        if execution_ceiling is not None:
            disabled.update(execution_ceiling["disabledTools"])
            hidden.update(execution_ceiling["hiddenTools"])
            block |= execution_ceiling["blockAllToolCalls"]
            ceiling_allowed = execution_ceiling["allowedTools"]
            if ceiling_allowed is not None:
                allowed = set(ceiling_allowed) if allowed is None else allowed & set(ceiling_allowed)
        # A missing retained execution ceiling never grants tools from a later
        # foreground or receipt, including for the main actor.
        if execution_ceiling is None:
            block = True
        projection = {"blockAllToolCalls": block, "disabledTools": sorted(disabled), "hiddenTools": sorted(hidden), "allowedTools": sorted(allowed) if allowed is not None else None}
        qualified_native = await native_projection(identity, entry, current, projection)
        native = qualified_native[0] if qualified_native else {'*': False}
        from src.openclank.browser_control_service import configured_browser_control_service
        service = configured_browser_control_service()
        async with bridge._managed_execution_lock:
            active = bridge._managed_actor_turns.get(key)
            if active is not entry:
                raise ValueError("managed input policy turn has finished")
            if (qualified_native and (getattr(request.app.state, 'files_policy_repository', None) is not qualified_native[1]
                    or qualified_native[1].generation() != qualified_native[2] or entry.get('ownerAdmissionStopped')
                    or getattr(bridge, '_ownerAdmissionHolderStopped', False)
                    or not callable(entry.get('originalCredentialCurrent')) or not entry['originalCredentialCurrent']())):
                native = {'*': False}
            ceiling_choice = (execution_ceiling or {}).get("choiceSnapshot") or {}
            receipt_identity = tuple((item["requestID"], item["payloadHash"]) for item in receipts)
            privacy = {"receipts": receipt_identity,
                "noMemory": bool(ceiling_choice.get("noMemory") or any((item.get("turnPolicy") or {}).get("noMemory") for item in receipts)),
                "incognito": bool(ceiling_choice.get("incognito") or any((item.get("turnPolicy") or {}).get("incognito") for item in receipts))}
            previous = active.get("receiptPrivacy")
            if previous is not None and previous != privacy:
                raise ValueError("managed receipt privacy changed for active turn")
            active["receiptPrivacy"] = privacy
            # A missing proof, vetoed batch, or changed account suppresses only
            # source observation; it cannot grant capture or change steering.
            active['queuedSourceIngress'] = queued_ingress if compatible and not privacy['noMemory'] and not privacy['incognito'] and not ceiling_choice.get('compareMode') else {}
            if compatible and no_memory:
                root_state = bridge._turns.get(current["rootSessionID"])
                if root_state is not None and root_state.root_operation_id == identity["rootOperationID"]:
                    root_state.privacy_capture_veto = True
            active["policyRevision"] = active.get("policyRevision", 0) + 1
            active["activePolicyProjection"] = projection if compatible else {**projection, "blockAllToolCalls": True}
            active["policyInputs"] = [{"turnPolicy": item.get("turnPolicy"), "text": item["text"]} for item in receipts]
            if service is not None:
                service.publish_managed_turn_policy(owner=current["owner"], host_session_id=current["stableChatID"],
                    workspace_id=current["authorityWorkspaceID"], binding_revision=current["workspaceRevision"], identity=identity,
                    envelope={"mode": next(iter(modes)) if len(modes) == 1 else "guide_only", "block_all": block or not compatible,
                        "allowed_tools": projection["allowedTools"], "disabled_tools": sorted(disabled | hidden)})
        return {**identity, "decisions": decisions, "batchDisposition": "ready" if compatible else "unavailable", "batchMode": next(iter(modes)) if len(modes) == 1 else None, "batchGuideOnly": block, "toolProjection": projection, "nativeToolPolicy": native}

    bridge.set_managed_input_policy_resolver(resolve)

    async def resolve_start(identity, entry):
        ceiling = entry.get("rawPolicyCeiling") or {}
        choices = ceiling.get("choiceSnapshot")
        if not choices:
            return
        current = await bridge.resolve_managed_actor_execution(
            identity["sessionID"], identity["rootOperationID"], identity["actorID"], identity["executionRevision"]
        )
        if current.get("authority"):
            choices = {**choices, "incognito": True, "noMemory": True}
        fresh = effective_receipt_policy_ceiling(request, current["owner"], current["stableChatID"],
            {"turnPolicy": choices, "text": ceiling.get("inputText", "")}, session_manager,
            theme_status_only=entry.get('themeStatusOnly') is True)
        allowed = fresh["allowedTools"]
        if ceiling["allowedTools"] is not None:
            allowed = list(ceiling["allowedTools"]) if allowed is None else sorted(set(allowed) & set(ceiling["allowedTools"]))
        disabled = set(fresh["disabledTools"]) | set(ceiling["disabledTools"]) | set(fresh["hiddenTools"]) | set(ceiling["hiddenTools"])
        projection = {"blockAllToolCalls": fresh["blockAllToolCalls"] or ceiling["blockAllToolCalls"],
            "disabledTools": sorted(disabled), "hiddenTools": [], "allowedTools": allowed}
        qualified_native = await native_projection(identity, entry, current, projection)
        from src.openclank.browser_control_service import configured_browser_control_service
        service = configured_browser_control_service()
        async with bridge._managed_execution_lock:
            active = bridge._managed_actor_turns.get(bridge._managed_turn_key(identity))
            if active is not entry:
                raise ValueError("managed turn finished while resolving host authority")
            active["policyRevision"] = active.get("policyRevision", 0) + 1
            active["activePolicyProjection"] = projection
            valid_native = bool(qualified_native and getattr(request.app.state, 'files_policy_repository', None) is qualified_native[1]
                and qualified_native[1].generation() == qualified_native[2] and not entry.get('ownerAdmissionStopped')
                and not getattr(bridge, '_ownerAdmissionHolderStopped', False)
                and callable(entry.get('originalCredentialCurrent')) and entry['originalCredentialCurrent']())
            active["nativeToolPolicy"] = qualified_native[0] if valid_native else None
            active["nativePolicyFilesWitness"] = (qualified_native[1], qualified_native[2], active["policyRevision"]) if valid_native else None
            active["policyInputs"] = [{"turnPolicy": choices, "text": ceiling.get("inputText", "")}]
            if service is not None and not current.get("authority"):
                service.publish_managed_turn_policy(owner=current["owner"], host_session_id=current["stableChatID"],
                    workspace_id=current["authorityWorkspaceID"], binding_revision=current["workspaceRevision"], identity=identity,
                    envelope={"mode": choices["mode"], "block_all": fresh["blockAllToolCalls"] or ceiling["blockAllToolCalls"],
                        "allowed_tools": allowed, "disabled_tools": sorted(disabled)})

    bridge._managed_start_policy_resolver = resolve_start

    async def resolve_start_replay(identity, entry):
        import copy
        key = bridge._managed_turn_key(identity)
        async with bridge._managed_execution_lock:
            if bridge._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active':
                return None
            revision = entry.get('policyRevision')
            observed_projection = copy.deepcopy(entry.get('activePolicyProjection'))
            policy_inputs = copy.deepcopy(entry.get('policyInputs'))
        if not isinstance(observed_projection, dict) or not isinstance(policy_inputs, list) or not policy_inputs:
            return None
        current = await bridge.resolve_managed_actor_execution(identity['sessionID'], identity['rootOperationID'],
            identity['actorID'], identity['executionRevision'])
        projection = copy.deepcopy(observed_projection)
        for item in policy_inputs:
            if not isinstance(item, dict) or not isinstance(item.get('turnPolicy'), dict):
                return None
            fresh = effective_receipt_policy_ceiling(request, current['owner'], current['stableChatID'], item,
                session_manager, theme_status_only=entry.get('themeStatusOnly') is True)
            projection['blockAllToolCalls'] = bool(projection['blockAllToolCalls'] or fresh['blockAllToolCalls'])
            projection['disabledTools'] = sorted(set(projection['disabledTools']) | set(fresh['disabledTools']))
            projection['hiddenTools'] = sorted(set(projection['hiddenTools']) | set(fresh['hiddenTools']))
            if fresh['allowedTools'] is not None:
                projection['allowedTools'] = (list(fresh['allowedTools']) if projection['allowedTools'] is None
                    else sorted(set(projection['allowedTools']) & set(fresh['allowedTools'])))
        qualified = await native_projection(identity, entry, current, projection)
        if not qualified:
            return None
        async with bridge._managed_execution_lock:
            if (bridge._managed_actor_turns.get(key) is not entry or entry.get('state') != 'active'
                    or entry.get('policyRevision') != revision or entry.get('activePolicyProjection') != observed_projection
                    or entry.get('policyInputs') != policy_inputs
                    or getattr(request.app.state, 'files_policy_repository', None) is not qualified[1]
                    or qualified[1].generation() != qualified[2] or entry.get('ownerAdmissionStopped')
                    or getattr(bridge, '_ownerAdmissionHolderStopped', False)
                    or not callable(entry.get('originalCredentialCurrent')) or not entry['originalCredentialCurrent']()):
                return None
            return (qualified[0], revision, qualified[1], qualified[2], entry.get('originalCredentialCurrent'))

    bridge._managed_replay_native_policy_resolver = resolve_start_replay

    async def resolve_tool(identity, entry, tool_name, kind, current):
        from src.tool_policy import conservative_legacy_turn_policy, known_tool_names
        if getattr(request.app.state, "auth_manager", None) is None:
            return False
        if kind == 'tool' and tool_name == 'gcf_arguments':
            try:
                from routes.prefs_routes import _load_for_user
                from src.agent_settings import validate_agent_settings
                preferences = _load_for_user(current['owner']) or {}
                if validate_agent_settings(preferences.get('agent_settings', {})).get('tokenomics', {}).get('gcf_argument_decode') is not True:
                    return False
            except Exception:
                return False
        active = entry.get("activePolicyProjection")
        inputs = entry.get("policyInputs")
        if not active or not inputs or active["blockAllToolCalls"]:
            return False
        if current.get("authorityWorkspaceID") == "" and (kind, tool_name) not in {
            ("tool", "memory"), ("memory_read", "recall_memory"),
        }:
            return False
        privacy = entry.get("receiptPrivacy")
        if privacy is not None and (privacy.get("incognito") or privacy.get("noMemory")) and (kind in {"memory_read", "skill_use"} or tool_name in {"workflow_artifact", "memory", "manage_rag"}):
            return False
        theme_status = (tool_name == 'theme_emoji_assets' and kind == 'tool'
            and current.get('toolOperation') == 'status' and entry.get('themeStatusOnly') is True)
        if tool_name == 'theme_emoji_assets' and entry.get('themeStatusOnly') is True and not theme_status:
            return False
        canonical_names = {tool_name}
        required_all = set()
        if kind == "memory_read":
            if tool_name != "recall_memory":
                return False
        elif kind == "tool" and tool_name == "memory":
            # The Native tool carries both read and write operations. Name
            # admission requires one available surface; the actual operation
            # independently rechecks its canonical permission before issue.
            canonical_names = {"manage_memory", "recall_memory"}
        elif kind == "skill_use":
            if tool_name != "skill":
                return False
            required_all = {"manage_memory"}
            canonical_names = set(required_all)
        elif kind == "project_policy_read":
            from src.openclank.acp_bridge import _MIMO_TOOL_ALIASES
            canonical_names = {name for name, aliases in _MIMO_TOOL_ALIASES.items() if tool_name in aliases}
            if not canonical_names and tool_name in known_tool_names():
                canonical_names = {tool_name}
            if not canonical_names:
                return False
        elif kind == "mcp_tool":
            parts = tool_name.split("__")
            if len(parts) != 3 or parts[0] != "mcp" or not all(parts[1:]):
                return False
            manager = getattr(request.app.state, "mcp_manager", None)
            if manager is None:
                return False
            catalog = {item.get("function", {}).get("name") for item in manager.get_all_openai_schemas()}
            if tool_name not in catalog:
                return False
        elif tool_name not in known_tool_names():
            return False
        memory_alias = kind == "tool" and tool_name == "memory"
        def policy_allows(projection):
            denied = set(projection["disabledTools"]) | set(projection["hiddenTools"])
            allowed = projection["allowedTools"]
            if memory_alias:
                possible = canonical_names - denied
                return bool(possible if allowed is None else possible.intersection(allowed))
            return not (canonical_names & denied) and (allowed is None or
                (bool(canonical_names.intersection(allowed)) and required_all.issubset(allowed)))
        if not policy_allows(active): return False
        # Re-resolve current privileges, prefs and workspace for every call.
        for item in inputs:
            choices = item.get("turnPolicy") or conservative_legacy_turn_policy()
            if kind == 'memory_read':
                import copy
                from src.openclank.memory_job_admission import _completed_turn_thread
                policy_input = copy.deepcopy({"turnPolicy": choices, "text": item["text"]})
                revision = entry.get('policyRevision')
                holder = bridge._managed_provider_generation
                fresh, cancelled = await _completed_turn_thread(lambda: effective_receipt_policy_ceiling(
                    request, current["owner"], current["stableChatID"], policy_input, session_manager,
                    theme_status_only=theme_status, requested_tools=frozenset({'recall_memory'}),
                    recall_expected_workspace=current.get('authorityWorkspaceID')))
                if cancelled: raise asyncio.CancelledError
                async with bridge._managed_execution_lock:
                    if (bridge._managed_actor_turns.get(bridge._managed_turn_key(identity)) is not entry
                            or entry.get('state') != 'active' or entry.get('policyRevision') != revision
                            or not holder or bridge._managed_provider_generation != holder):
                        return False
            else:
                fresh = effective_receipt_policy_ceiling(request, current["owner"], current["stableChatID"],
                    {"turnPolicy": choices, "text": item["text"]}, session_manager, theme_status_only=theme_status)
            if kind == 'memory_read' and fresh.get('projectionTools') != ['recall_memory']:
                return False
            if fresh["blockAllToolCalls"] or not policy_allows(fresh): return False
        return True

    bridge._managed_tool_policy_resolver = resolve_tool


def _get_observation_binding(request: Request, host_chat_id: str, map_revision: int | None, session_manager, owner_verifier=None):
    user = effective_user(request)
    if not getattr(request.state, "authenticated", False) or not user:
        raise HTTPException(401, "Authentication required")
    supervisor = getattr(request.app.state, "mimo_supervisor", None)
    runtime_home = getattr(supervisor, "_runtime_home", None)
    if not callable(runtime_home):
        raise HTTPException(503, {"code": "SESSION_MAP_UNAVAILABLE", "message": "Owner session map is unavailable"})
    try:
        mapping = OwnerSessionMap(runtime_home(user) / "session-map.json", user)
        snapshot = mapping.read()
        pinned_revision = snapshot.get("mapRevision") if map_revision is None else map_revision
        if isinstance(pinned_revision, bool) or not isinstance(pinned_revision, int):
            raise ProjectionError(503, "SESSION_MAP_UNAVAILABLE", "Owner session map revision is unavailable")
        binding = resolve_binding(snapshot, user, host_chat_id, pinned_revision)
    except ProjectionError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc
    except Exception as exc:
        raise HTTPException(503, {"code": "SESSION_MAP_UNAVAILABLE", "message": "Owner session map is unavailable"}) from exc
    # The host chat authorization check also rejects guessed IDs before native I/O.
    verify_owner = owner_verifier
    if verify_owner is None:
        from routes.session_routes import _verify_session_owner
        verify_owner = _verify_session_owner
    verify_owner(request, host_chat_id, session_manager)
    return user, supervisor, binding, mapping

def _ensure_observation_binding_current(mapping, owner, host_chat_id, binding):
    try:
        current = resolve_binding(mapping.read(), owner, host_chat_id, binding.map_revision)
    except ProjectionError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc
    except Exception as exc:
        raise HTTPException(503, {"code": "SESSION_MAP_UNAVAILABLE", "message": "Owner session map could not be reread safely"}) from exc
    if current.engine_session_id != binding.engine_session_id or current.binding_revision != binding.binding_revision:
        raise HTTPException(409, {"code": "STALE_MAP", "message": "Session mapping changed during orchestration read"})


async def inspect_owned_mcp_health(request, host_chat_id, *, expected_owner, session_manager, capabilities=False):
    """Read a passive MCP snapshot for one still-active owned main turn."""
    from routes.session_routes import _verify_session_owner
    from src.openclank.managed_protocol import validate_engine_method_request, validate_engine_method_result
    from src.openclank.mimo_supervisor import AgentWorkerLease
    unavailable = {'status':'unavailable','exec':None} if capabilities else {'status':'unavailable','servers':[],'omittedServers':0}
    method = '_openclank/session/v1/tools/capabilities' if capabilities else '_openclank/session/v1/mcp/health'
    await asyncio.to_thread(_verify_session_owner, request, host_chat_id, session_manager)
    try:
        owner, supervisor, binding, mapping = await asyncio.to_thread(
            _get_observation_binding, request, host_chat_id, None, session_manager)
        if owner != expected_owner: raise HTTPException(404, 'Session unavailable')
        pool = supervisor.owner_lifecycle_pool(); key = pool._key(owner)
        async with pool._owner_lock(key):
            epoch = pool._owner_lifecycle_epoch(key); pool._assert_owner_lifecycle(key,epoch)
            state = pool._states.get(key); worker = state.active if state else None
            if worker is None or state.status != 'ready' or not worker.is_alive(): return unavailable
            bridge = getattr(worker,'_bridge',None)
            holder = getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)
            generation = getattr(worker,'installed_generation',None)
            if (bridge is None or not holder or holder != getattr(bridge,'_managed_provider_generation',None)
                    or type(generation) is not int or generation != state.generation): return unavailable
            state.in_flight[worker] = state.in_flight.get(worker,0)+1
            state.drain_events.setdefault(worker,asyncio.Event()).clear()
        lease = AgentWorkerLease(pool,key,worker,owner_epoch=epoch)
        try:
            async with bridge._managed_execution_lock:
                candidates = [(k,v) for k,v in bridge._managed_actor_turns.items()
                    if isinstance(v,dict) and v.get('state') == 'active' and v.get('actorID') == 'main'
                    and v.get('sessionID') == binding.engine_session_id and v.get('stableChatID') == host_chat_id
                    and v.get('owner') == owner and v.get('ownerAccountID')]
                if len(candidates) != 1: return unavailable
                turn_key, entry = candidates[0]
                identity = {name:entry.get(name) for name in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')}
                account = entry['ownerAccountID']; policy = entry.get('policyRevision')
            auth = getattr(request.app.state,'auth_manager',None)
            def current_account():
                return bool(auth and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                    and auth.is_admin(owner) is True and (auth.get_privileges(owner) or {}).get('can_use_agent',True))
            if not current_account(): return unavailable
            native = validate_engine_method_request(method,identity)
            result = await asyncio.wait_for(worker.managed_engine_call(method,native),timeout=5.0)
            result = validate_engine_method_result(method,result)
            if result.get('status') == 'unavailable': return unavailable
            if not capabilities and len({item['serverKey'] for item in result['servers']}) != len(result['servers']): return unavailable
            if capabilities and result['exec']['declarations'] is not None and len(result['exec']['declarations'].encode('utf-8')) > 32768: return unavailable
            await asyncio.to_thread(_ensure_observation_binding_current,mapping,owner,host_chat_id,binding)
            await asyncio.to_thread(_verify_session_owner,request,host_chat_id,session_manager)
            async with bridge._managed_execution_lock:
                current = bridge._managed_actor_turns.get(turn_key)
                if current is not entry or current.get('state') != 'active' or current.get('policyRevision') != policy or current.get('ownerAccountID') != account:
                    return unavailable
            if (not current_account() or pool._states.get(key) is not state or state.active is not worker
                or state.status != 'ready' or state.generation != generation or not worker.is_alive()
                or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) != holder
                or getattr(bridge,'_managed_provider_generation',None) != holder): return unavailable
            pool._assert_owner_lifecycle(key,epoch)
            return result
        finally:
            await lease.release()
    except HTTPException as exc:
        if exc.status_code in {409,503}: return unavailable
        raise
    except Exception:
        return unavailable


async def inspect_owned_observations(request, host_chat_id, *, expected_owner, session_manager):
    """Read bounded observations from the currently mapped, already-live owner worker."""
    from services.stats.privacy import identity_handle
    unavailable = {"version":1, "availability":"unavailable", "worker":{"availability":"unavailable"},
                   "actors":{"availability":"unavailable", "items":[]},
                   "tasks":{"availability":"unavailable", "items":[]},
                   "reconnect":{"availability":"unavailable"}, "resources":{"availability":"unavailable"},
                   "backgroundJobs":{"availability":"unavailable"}}
    from routes.session_routes import _verify_session_owner
    auth = getattr(request.app.state, 'auth_manager', None)
    account = auth.account_id(expected_owner) if auth else None
    if not account or auth.username_for_account_id(account) != expected_owner:
        return unavailable
    await asyncio.to_thread(_verify_session_owner, request, host_chat_id, session_manager)
    try:
        owner, supervisor, binding, mapping = await asyncio.to_thread(
            _get_observation_binding, request, host_chat_id, None, session_manager)
    except HTTPException as exc:
        if exc.status_code in {404, 409, 503}:
            return unavailable
        raise
    if owner != expected_owner:
        raise HTTPException(404, "Session unavailable")
    try:
        if auth.account_id(owner) != account or auth.username_for_account_id(account) != owner:
            return unavailable
        pool = supervisor.owner_lifecycle_pool()
        key = pool._key(owner)
        epoch = pool._owner_lifecycle_epoch(key)
        pool._assert_owner_lifecycle(key, epoch)
        worker = pool.worker_for_owner(owner)
        state = pool._states.get(pool._key(owner))
        holder = getattr(getattr(worker, "_managed_callbacks", None), "holder_id", None)
        generation = getattr(worker, "installed_generation", None)
        process = getattr(worker, "_proc", None)
        bridge = getattr(worker, "_bridge", None)
        if (worker is None or state is None or state.active is not worker or state.status != "ready" or not worker.is_alive()
                or not isinstance(holder, str) or not holder or type(generation) is not int
                or generation != state.generation or process is None or bridge is None
                or getattr(bridge, "_managed_provider_generation", None) != holder):
            return unavailable
        pages = {}
        for kind, source in (("native_actor", "actor"), ("native_task", "task")):
            response = await pool.session_observation_request(host_chat_id, binding.engine_session_id, source, 32, None, owner=owner)
            if response.status_code != 200 or len(response.content) > 1024 * 1024:
                return unavailable
            body = response.json()
            if (not isinstance(body, dict) or set(body) - {"items", "nextCursor", "observedAt"}
                    or not isinstance(body.get("items"), list) or len(body["items"]) > 32
                    or type(body.get("observedAt")) is not int or body["observedAt"] < 0
                    or (body.get("nextCursor") is not None and (not isinstance(body["nextCursor"], str)
                        or not body["nextCursor"] or len(body["nextCursor"]) > 512))):
                return unavailable
            pages[kind] = {"availability":"partial" if body.get("nextCursor") else "available",
                "items":body["items"], "observedAt":body["observedAt"],
                **({"reason":"more_pages", "nextCursor":body["nextCursor"]} if body.get("nextCursor") else {})}
        projected = project_task_observations(TaskObservationScope(owner, binding, (binding.engine_session_id,)), pages)
        def clean(kind):
            page = projected["sources"][kind]
            items = []
            for row in page["items"]:
                ref = row["sourceRef"]
                opaque = identity_handle(owner, "operation_id", f'{kind}:{ref["sessionId"]}:{ref["nativeId"]}')
                items.append({"handle":opaque, "status":row["nativeStatus"],
                    **({"outcome":row["nativeOutcome"]} if "nativeOutcome" in row else {})})
            return {"availability":page["availability"], "items":items, "observedAt":page["observedAt"]}
        from src import bg_jobs
        from routes.session_routes import _validated_session_workspace
        import time
        session = session_manager.get_session(host_chat_id)
        workspace_id = str(getattr(session, 'workspace_id', '') or '')
        if (not session or session.owner != owner or getattr(session, 'incognito', False)
                or getattr(session, 'archived', False) or not workspace_id):
            return unavailable
        workspace = await asyncio.to_thread(_validated_session_workspace, request, owner, workspace_id)
        sources = {}
        for source in ('shell_job', 'media_job'):
            try:
                page = await asyncio.to_thread(bg_jobs.workload_job_page, source=source, account_id=account,
                    owner=owner, session_id=host_chat_id, workspace_id=workspace_id, workspace=workspace.path, limit=16)
                observed = int(time.time() * 1000)
                items = [{"handle":identity_handle(owner, "operation_id", f'{source}:{host_chat_id}:{row["id"]}'),
                    "kind":source, "status":row["status"], "revision":row["revision"]} for row in page["items"]]
                sources[source] = {"availability":"partial" if page["next_after"] else "available",
                    "items":items, "observedAt":observed}
            except Exception:
                sources[source] = {"availability":"unavailable", "items":[]}
        # Revalidate the original scope after every observation, including job awaits.
        await asyncio.to_thread(_ensure_observation_binding_current, mapping, owner, host_chat_id, binding)
        await asyncio.to_thread(_verify_session_owner, request, host_chat_id, session_manager)
        checked_workspace = await asyncio.to_thread(_validated_session_workspace, request, owner, workspace_id)
        current = pool._states.get(key)
        pool._assert_owner_lifecycle(key, epoch)
        if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                or session_manager.get_session(host_chat_id) is not session or session.owner != owner
                or str(getattr(session, 'workspace_id', '') or '') != workspace_id
                or getattr(session, 'incognito', False) or getattr(session, 'archived', False)
                or checked_workspace != workspace or current is not state or current.active is not worker
                or current.status != 'ready' or current.generation != generation
                or pool.worker_for_owner(owner) is not worker or not worker.is_alive()
                or getattr(worker, '_proc', None) is not process or getattr(worker, '_bridge', None) is not bridge
                or getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) != holder
                or getattr(bridge, '_managed_provider_generation', None) != holder):
            return unavailable
        background = {"sources":sources}
        return {"version":1, "availability":"available", "worker":{"availability":"available", "generation":generation, "status":"ready"},
                "actors":clean("native_actor"), "tasks":clean("native_task"), "reconnect":{"availability":"unavailable"},
                "resources":{"availability":"unavailable"}, "backgroundJobs":background}
    except HTTPException:
        raise
    except Exception:
        return unavailable


def _feedback_receipt_state(receipt):
    """Project queue evidence without treating admission as delivery."""
    state = receipt.get('state') if isinstance(receipt, dict) else None
    if state in {'persisted', 'pending-boundary', 'cancelled', 'failed'}:
        return state
    if state != 'consumed':
        return 'unknown'
    queue = receipt.get('queueDisposition')
    if not isinstance(queue, dict):
        return 'unknown'
    queue_state = queue.get('state')
    queue_id = queue.get('receiptID')
    if not isinstance(queue_id, str) or not queue_id or len(queue_id) > 256:
        return 'unknown'
    if (queue_state in {'accepted', 'claimed', 'settled'} and queue.get('delivered') is True
            and isinstance(queue.get('deliveryMessageID'), str) and 0 < len(queue['deliveryMessageID']) <= 256):
        return 'consumed'  # Queue delivery anchor, not provider-wire acknowledgement.
    if queue_state == 'cancelled' and queue.get('outcome') == 'never_ran' and queue.get('delivered') is False:
        return 'cancelled'
    if queue_state in {'accepted', 'claimed'} and queue.get('delivered') is False:
        return 'pending-boundary'
    return 'unknown'


def _feedback_queue_proof(receipt):
    if _feedback_receipt_state(receipt) != 'consumed':
        return None
    queue = receipt['queueDisposition']
    return {'version': 1, 'kind': 'queue_delivery', 'receipt_id': queue['receiptID'],
            'delivery_message_id': queue['deliveryMessageID']}


def setup_orchestration_routes(session_manager, *, owner_verifier=None, option_resolver=None) -> APIRouter:
    router = APIRouter(prefix="/api/orchestration", tags=["orchestration"])

    @router.get('/{host_chat_id}/workload-jobs/{source}')
    async def workload_jobs(request: Request, host_chat_id: str, source: str,
                            workspace_id: str | None = None, cursor: str | None = None, limit: int = 50):
        """Observe host-owned jobs without starting or reconciling a worker."""
        import base64, hashlib, json, time
        from fastapi.responses import JSONResponse
        from core.database import SessionLocal, Session as DbSession
        from routes.session_routes import _verify_session_owner, _validated_session_workspace
        from src import bg_jobs
        headers = {'Cache-Control':'private, no-store'}
        if source == 'memory_job':
            return await workload_memory_jobs(request, host_chat_id, workspace_id=workspace_id, cursor=cursor, limit=min(limit, 16))
        if source not in ('shell_job','media_job') or not 1 <= limit <= 100:
            return JSONResponse({'error':'invalid request'},status_code=400,headers=headers)
        try:
            owner = effective_user(request)
            auth = getattr(request.app.state,'auth_manager',None)
            barrier = getattr(request.app.state,'account_request_barrier',None)
            account = auth.account_id(owner) if auth else None
            if (not getattr(request.state,'authenticated',False) or not owner or not barrier or not account
                    or auth.username_for_account_id(account) != owner):
                raise ValueError('account unavailable')
            def chat_snapshot():
                with SessionLocal() as db:
                    row = db.query(DbSession).filter(DbSession.id == host_chat_id).first()
                    if row is None or row.owner != owner or row.archived or not row.workspace_id:
                        raise ValueError('chat unavailable')
                    cached = session_manager.sessions.get(host_chat_id)
                    if cached is not None and getattr(cached,'incognito',False): raise ValueError('private chat')
                    return str(row.workspace_id)
            async with barrier.admitted(owner):
                if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                        or await asyncio.to_thread(_verify_session_owner,request,host_chat_id,session_manager) != owner):
                    raise ValueError('account changed')
                chat_workspace = await asyncio.to_thread(chat_snapshot)
                selected_workspace = chat_workspace if workspace_id is None else workspace_id
                if (type(selected_workspace) is not str or not selected_workspace or selected_workspace != selected_workspace.strip()
                        or len(selected_workspace) > 256):
                    raise ValueError('workspace unavailable')
                binding = await asyncio.to_thread(_validated_session_workspace,request,owner,selected_workspace)
                scope_hash = hashlib.sha256(json.dumps([source,account,host_chat_id,chat_workspace,selected_workspace,binding.path],separators=(',',':')).encode()).hexdigest()
                after = ''
                if cursor:
                    if len(cursor) > 1024: raise ValueError('cursor invalid')
                    payload = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor)%4)))
                    if (type(payload) is not dict or set(payload) != {'v','scope','after'} or payload['v'] != 1
                            or payload['scope'] != scope_hash or type(payload['after']) is not str):
                        raise ValueError('cursor scope changed')
                    after = payload['after']
                if auth.account_id(owner) != account or auth.username_for_account_id(account) != owner:
                    raise ValueError('account changed')
                page = await asyncio.to_thread(bg_jobs.workload_job_page,source=source,account_id=account,owner=owner,
                    session_id=host_chat_id,workspace_id=selected_workspace,workspace=binding.path,after=after,limit=limit)
                now_workspace = await asyncio.to_thread(chat_snapshot)
                current_binding = await asyncio.to_thread(_validated_session_workspace,request,owner,selected_workspace)
                if (auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                        or now_workspace != chat_workspace or current_binding != binding):
                    raise ValueError('scope changed')
                observed = int(time.time()*1000)
                items = [{'sourceRef':{'kind':source,'sessionId':host_chat_id,'nativeId':row['id']},
                    'displayCategory':'execution','nativeStatus':row['status'],'sourceRevision':row['revision'],
                    'observedAt':observed,'title':('Shell job ' if source == 'shell_job' else 'Media job ') + row['id'][:8],
                    'capabilities':['inspect'],'linkCoverage':'unknown','links':[]} for row in page['items']]
                next_after = page['next_after']
                result = {'availability':'partial' if next_after else 'available','observedAt':observed,'items':items}
                if next_after:
                    raw = json.dumps({'v':1,'scope':scope_hash,'after':next_after},separators=(',',':')).encode()
                    result.update(reason='more_pages',nextCursor=base64.urlsafe_b64encode(raw).decode().rstrip('='))
                return JSONResponse({'version':1,'sources':{source:result}},headers=headers)
        except HTTPException as exc:
            return JSONResponse({'error':'job observation unavailable'},status_code=exc.status_code,headers=headers)
        except Exception:
            return JSONResponse({'error':'job observation unavailable'},status_code=503,headers=headers)

    async def workload_memory_jobs(request: Request, host_chat_id: str, *, workspace_id: str | None, cursor: str | None, limit: int):
        """Observe an already-qualified local S05 subset without starting its provider."""
        import base64, hashlib, json, time
        from fastapi.responses import JSONResponse
        from core.database import SessionLocal, Session as DbSession
        from routes.session_routes import _verify_session_owner, _validated_session_workspace
        from src.memery_database import GuardedStorePreparation
        from src.memery_external_sources import CurrentHostUserSource
        from src.openclank import conversation_archive
        from src.openclank.memory_job_admission import ForegroundRuntime, SingleHostProfile, TargetAuthority
        from src.openclank.memory_job_observations import source_job_page, qualification_current
        from src.openclank.memory_policy import STORE
        from src.openclank.orchestration_projection import project_memory_job_observations
        headers = {'Cache-Control':'private, no-store'}
        try:
            if type(limit) is not int or not 1 <= limit <= 16:
                raise ValueError('invalid page')
            state = request.app.state
            owner = effective_user(request); auth = getattr(state,'auth_manager',None)
            barrier = getattr(state,'account_request_barrier',None)
            supervisor = getattr(state,'mimo_supervisor',None); profile = getattr(state,'s05_profile',None)
            account = auth.account_id(owner) if auth and owner else None
            runtime = getattr(supervisor,'_s05_foreground_runtime',None)
            if (not getattr(request.state,'authenticated',False) or not account or not barrier or not supervisor
                    or type(profile) is not SingleHostProfile or type(runtime) is not ForegroundRuntime
                    or runtime.profile is not profile or getattr(supervisor,'_s05_profile',None) is not profile
                    or auth.username_for_account_id(account) != owner):
                raise ValueError('memory observation unavailable')
            prepared = getattr(profile.provider,'_guarded_preparation',None)
            if type(prepared) is not GuardedStorePreparation or prepared.database_path != profile.path:
                raise ValueError('memory observation unavailable')
            pool = supervisor.owner_lifecycle_pool()
            authority = TargetAuthority(auth_manager=auth,supervisor=pool)
            repository = getattr(state,'files_policy_repository',None)
            with conversation_archive._ARCHIVE_LOCK:
                archive = conversation_archive._ARCHIVE
            if archive is None: raise ValueError('archive unavailable')
            verifier = CurrentHostUserSource(owner=owner,auth_manager=auth,session_factory=SessionLocal,
                archive=archive,workspace_repository=repository,policy_store=STORE,guarded_preparation=prepared)
            def current():
                profile.assert_current();prepared.assert_valid()
                with conversation_archive._ARCHIVE_LOCK:
                    if conversation_archive._ARCHIVE is not archive: raise ValueError('archive changed')
                if (getattr(state,'s05_profile',None) is not profile or getattr(supervisor,'_s05_profile',None) is not profile
                    or getattr(supervisor,'_s05_foreground_runtime',None) is not runtime or runtime.profile is not profile
                    or getattr(profile.provider,'_guarded_preparation',None) is not prepared
                    or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
                    or _verify_session_owner(request,host_chat_id,session_manager)!=owner):
                    raise ValueError('account changed')
                with SessionLocal() as db:
                    row=db.query(DbSession).filter(DbSession.id==host_chat_id).first()
                    if row is None or row.owner!=owner or row.archived:
                        raise ValueError('chat unavailable')
                    chat_workspace=row.workspace_id
                    if chat_workspace is not None and (type(chat_workspace) is not str or not chat_workspace or chat_workspace != chat_workspace.strip()):
                        raise ValueError('chat unavailable')
                cached=session_manager.sessions.get(host_chat_id)
                if getattr(cached,'incognito',False) or (workspace_id is not None and workspace_id!=chat_workspace):
                    raise ValueError('chat unavailable')
                if chat_workspace is None:
                    prepared.assert_account_chat()
                    binding=None
                else:
                    if repository is None: raise ValueError('workspace unavailable')
                    binding=_validated_session_workspace(request,owner,chat_workspace)
                privileges=auth.get_privileges(owner)
                privilege=privileges.get('can_manage_memory',True) if isinstance(privileges,dict) else False
                if type(privilege) is not bool or not privilege: raise ValueError('memory privilege unavailable')
                policy=STORE.effective_snapshot(owner,read_privilege=privilege,write_privilege=privilege,
                    incognito=False,no_memory=False,compare_mode=False)
                if policy.get('eligibility_state')!='available' or policy.get('read_allowed') is not True:
                    raise ValueError('memory read unavailable')
                if (type(policy.get('fingerprint')) is not str or not re.fullmatch(r'[0-9a-f]{64}',policy['fingerprint'])
                    or not qualification_current(profile.path,coverage=profile.coverage_generation,clock=profile.clock_generation)):
                    raise ValueError('memory qualification unavailable')
                return (chat_workspace,binding,authority.resolve(account),policy.get('fingerprint'))
            async def owned(call):
                task=asyncio.create_task(asyncio.to_thread(call))
                try: return await asyncio.shield(task)
                except asyncio.CancelledError:
                    while not task.done():
                        try: await asyncio.shield(task)
                        except asyncio.CancelledError: continue
                    task.result();raise
            async with barrier.admitted(owner):
                baseline=await owned(current)
                chat_workspace,binding,target,policy_fingerprint=baseline
                if target.source_account_id!=account:
                    raise ValueError('memory target changed')
                scope=hashlib.sha256(json.dumps([account,host_chat_id,chat_workspace,binding.path if binding is not None else None,target.fingerprint,policy_fingerprint,
                    prepared.database_id,profile.device,profile.inode,profile.coverage_generation,profile.clock_generation],separators=(',',':')).encode()).hexdigest()
                after=''
                if cursor:
                    if len(cursor)>1024: raise ValueError('cursor unavailable')
                    data=json.loads(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)))
                    if type(data) is not dict or set(data)!={'v','scope','after'} or type(data['v']) is not int or data['v']!=1 or data['scope']!=scope or type(data['after']) is not str:
                        raise ValueError('cursor unavailable')
                    after=data['after']
                mapping=OwnerSessionMap(pool._runtime_home(owner)/'session-map.json',owner)
                async with mapping.async_chat_admission_lock(host_chat_id,blocking=False,existing_only=True) as guard:
                    if guard is None or await owned(current)!=baseline: raise ValueError('source guard unavailable')
                    def read_and_verify():
                        result=source_job_page(profile.path,owner=target.memory_owner,account=account,
                            memory_workspace=target.memory_workspace,chat=host_chat_id,source_workspace=chat_workspace,
                            coverage=profile.coverage_generation,clock=profile.clock_generation,after=after,limit=limit,preparation=prepared)
                        for item in result['items']:
                            if verifier.verify_feedback_origin(item['source']).get('state')!='available':
                                raise ValueError('source unavailable')
                        return result
                    page=await owned(read_and_verify)
                    if await owned(current)!=baseline: raise ValueError('source changed')
                next_cursor=None
                if page['after']:
                    raw=json.dumps({'v':1,'scope':scope,'after':page['after']},separators=(',',':')).encode()
                    next_cursor=base64.urlsafe_b64encode(raw).decode().rstrip('=')
                return JSONResponse(project_memory_job_observations(page['items'],observed_at=int(time.time()*1000),
                    next_cursor=next_cursor),headers=headers)
        except Exception:
            return JSONResponse({'error':'memory job observation unavailable'},status_code=503,headers=headers)

    @router.get('/{host_chat_id}/background-jobs')
    async def bound_background_jobs(request: Request, host_chat_id: str, job_id: str | None = None,
                                    cursor: int = 0, limit: int = 32):
        from routes.session_routes import _verify_session_owner, _validated_session_workspace
        from src import bg_jobs
        owner=await asyncio.to_thread(_verify_session_owner,request,host_chat_id,session_manager)
        auth=getattr(request.app.state,'auth_manager',None)
        account=auth.account_id(owner) if auth else None
        session=session_manager.get_session(host_chat_id)
        workspace_id=str(getattr(session,'workspace_id','') or '')
        if (not account or auth.username_for_account_id(account)!=owner or not session or session.owner!=owner
                or getattr(session,'incognito',False) or getattr(session,'archived',False) or not workspace_id):
            raise HTTPException(404,'Background jobs unavailable')
        workspace=await asyncio.to_thread(_validated_session_workspace,request,owner,workspace_id)
        scope=dict(account_id=account,owner=owner,session_id=host_chat_id,workspace_id=workspace_id,workspace=workspace.path)
        try:
            if job_id is None:
                result={'items':await asyncio.to_thread(bg_jobs.bound_shell_page,**scope,limit=limit)}
            else:
                result=await asyncio.to_thread(bg_jobs.bound_shell_tail,job_id,**scope,cursor=cursor,limit=limit)
            await asyncio.to_thread(_verify_session_owner,request,host_chat_id,session_manager)
            current=await asyncio.to_thread(_validated_session_workspace,request,owner,workspace_id)
            if (auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
                    or session_manager.get_session(host_chat_id) is not session or session.owner!=owner
                    or getattr(session,'workspace_id',None)!=workspace_id or getattr(session,'incognito',False)
                    or getattr(session,'archived',False) or current.path!=workspace.path):
                raise ValueError('scope changed')
            from fastapi.responses import JSONResponse
            return JSONResponse(result,headers={'Cache-Control':'private, no-store'})
        except Exception as exc:
            if isinstance(exc, HTTPException): raise
            raise HTTPException(503,'Background jobs unavailable') from exc

    def get_binding(request: Request, host_chat_id: str, map_revision: int | None):
        return _get_observation_binding(request, host_chat_id, map_revision, session_manager, owner_verifier)

    def ensure_current(mapping, owner, host_chat_id, binding):
        return _ensure_observation_binding_current(mapping, owner, host_chat_id, binding)

    def retire_browser_policy_before_native_resume(owner, host_chat_id):
        """Close the normal-turn Browser capability before O08's native handoff."""
        try:
            from src.openclank.browser_control_service import configured_browser_control_service
            service = configured_browser_control_service()
            if service is not None:
                service.retire_chat_turn_policies(
                    owner=str(owner).strip().lower(), host_session_id=host_chat_id,
                )
        except Exception as exc:
            raise HTTPException(503, {
                "code": "BROWSER_POLICY_RETIREMENT_UNAVAILABLE",
                "message": "The prior Browser turn policy could not be retired before native resume",
            }) from exc

    async def native_request(supervisor, binding, owner, suffix: str, *, method: str = "GET", payload: dict | None = None):
        request_session = getattr(supervisor, "session_http_request", None)
        if not callable(request_session):
            raise HTTPException(503, {"code": "ENGINE_READ_UNAVAILABLE", "message": "Managed engine reads are unavailable"})
        try:
            response = await request_session(binding.host_chat_id, method, suffix, owner=owner, payload=payload)
        except Exception as exc:
            from src.openclank.agent_supervisor import AgentSupervisorAdmissionError
            if isinstance(exc, AgentSupervisorAdmissionError):
                raise HTTPException(exc.status, {"code": getattr(exc, "code", "ENGINE_UNAVAILABLE"), "message": "Managed engine is unavailable"}) from exc
            if isinstance(exc, (RuntimeError, httpx.HTTPError)):
                raise HTTPException(503, {"code": "ENGINE_UNAVAILABLE", "message": "Managed engine is unavailable"}) from exc
            raise
        try:
            body = response.json()
        except ValueError as exc:
            raise HTTPException(502, {"code": "ENGINE_INVALID_RESPONSE", "message": "Managed engine returned invalid JSON"}) from exc
        if response.status_code >= 400:
            raise HTTPException(response.status_code, {"code": "ENGINE_READ_FAILED" if method == "GET" else "ENGINE_INPUT_FAILED", "message": "Managed engine request failed"})
        return body, response

    async def child_native_request(supervisor, binding, owner, child_session_id: str, suffix: str, *, method: str = "GET", payload: dict | None = None):
        if method != "GET":
            mutate_child = getattr(supervisor, "session_child_input_request", None)
            if not callable(mutate_child):
                raise HTTPException(503, {"code": "CHILD_INPUT_UNAVAILABLE", "message": "Verified child-session input is unavailable"})
            try:
                response = await mutate_child(binding.host_chat_id, child_session_id, method, suffix, payload, owner=owner)
            except PermissionError as exc:
                raise HTTPException(404, {"code": "CHILD_SESSION_NOT_MAPPED", "message": "Child session is not a verified child of this chat"}) from exc
            except ValueError as exc:
                raise HTTPException(400, {"code": "UNSUPPORTED_CHILD_INPUT", "message": str(exc)}) from exc
            except Exception as exc:
                from src.openclank.agent_supervisor import AgentSupervisorAdmissionError
                if isinstance(exc, AgentSupervisorAdmissionError):
                    raise HTTPException(exc.status, {"code": getattr(exc, "code", "ENGINE_UNAVAILABLE"), "message": "Managed engine is unavailable"}) from exc
                if isinstance(exc, (RuntimeError, httpx.HTTPError)):
                    raise HTTPException(503, {"code": "ENGINE_UNAVAILABLE", "message": "Managed engine is unavailable"}) from exc
                raise
            try:
                body = response.json()
            except ValueError as exc:
                raise HTTPException(502, {"code": "ENGINE_INVALID_RESPONSE", "message": "Managed engine returned invalid JSON"}) from exc
            if response.status_code >= 400:
                raise HTTPException(response.status_code, {"code": "ENGINE_INPUT_FAILED", "message": "Managed engine input request failed"})
            return body, response
        read_child = getattr(supervisor, "session_child_http_request", None)
        if not callable(read_child):
            raise HTTPException(503, {"code": "CHILD_SESSION_READ_UNAVAILABLE", "message": "Verified child-session reads are unavailable"})
        try:
            response = await read_child(binding.host_chat_id, child_session_id, suffix, owner=owner)
        except PermissionError as exc:
            raise HTTPException(404, {"code": "CHILD_SESSION_NOT_MAPPED", "message": "Child session is not a verified child of this chat"}) from exc
        except ValueError as exc:
            raise HTTPException(400, {"code": "UNSUPPORTED_CHILD_READ", "message": str(exc)}) from exc
        except Exception as exc:
            from src.openclank.agent_supervisor import AgentSupervisorAdmissionError
            if isinstance(exc, AgentSupervisorAdmissionError):
                raise HTTPException(exc.status, {"code": getattr(exc, "code", "ENGINE_UNAVAILABLE"), "message": "Managed engine is unavailable"}) from exc
            if isinstance(exc, (RuntimeError, httpx.HTTPError)):
                raise HTTPException(503, {"code": "ENGINE_UNAVAILABLE", "message": "Managed engine is unavailable"}) from exc
            raise
        try:
            body = response.json()
        except ValueError as exc:
            raise HTTPException(502, {"code": "ENGINE_INVALID_RESPONSE", "message": "Managed engine returned invalid JSON"}) from exc
        if response.status_code >= 400:
            raise HTTPException(response.status_code, {"code": "ENGINE_READ_FAILED", "message": "Managed engine child read failed"})
        return body, response

    async def actor_list(supervisor, binding, owner, session_id):
        if session_id == binding.engine_session_id:
            value, _ = await native_request(supervisor, binding, owner, "actors")
        else:
            value, _ = await child_native_request(supervisor, binding, owner, session_id, "actors")
        try:
            return validate_actors(value, session_id)
        except ProjectionError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc

    async def actor_input_request(supervisor, binding, owner, session_id, method, suffix, payload=None):
        if session_id == binding.engine_session_id:
            return await native_request(supervisor, binding, owner, suffix, method=method, payload=payload)
        return await child_native_request(supervisor, binding, owner, session_id, suffix, method=method, payload=payload)

    async def ensure_direct_child(supervisor, binding, owner, session_id):
        children, _ = await native_request(supervisor, binding, owner, "children?visible=true")
        if not isinstance(children, list) or not any(
            isinstance(child, dict) and child.get("id") == session_id
            and child.get("parentID") == binding.engine_session_id for child in children
        ):
            raise HTTPException(404, {"code": "CHILD_SESSION_NOT_MAPPED", "message": "Child session is not a verified child of this chat"})

    async def input_receipts(supervisor, binding, owner, session_id, actor_id):
        suffix = f"actors/{quote(actor_id, safe='')}/input"
        value, _ = await actor_input_request(supervisor, binding, owner, session_id, "GET", suffix)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise HTTPException(502, {"code": "ENGINE_INVALID_RESPONSE", "message": "Managed engine returned invalid input receipt data"})
        return value

    def settle_upload_claims(owner, host_chat_id, receipt, *, committed):
        upload_ids = receipt.get("attachmentUploadIDs") if isinstance(receipt, dict) else None
        request_id = receipt.get("requestID") if isinstance(receipt, dict) else None
        if not isinstance(upload_ids, list) or not isinstance(request_id, str):
            return
        from src.tool_utils import get_upload_handler
        handler = get_upload_handler()
        if handler is None:
            return
        scope = _receipt_scope(owner, host_chat_id, request_id)
        attempt_prefix = _receipt_attempt_prefix(owner, host_chat_id, request_id)
        chat_scope = f"chat:{host_chat_id}"
        for identifier in upload_ids:
            try:
                info = handler.get_upload_info(identifier) if hasattr(handler, "get_upload_info") else None
                if isinstance(info, dict) and str(info.get("owner") or "").strip().lower() == str(owner).strip().lower():
                    claims = set(info.get("reference_claims") or [])
                    for claim in claims:
                        if claim == scope:
                            handler.settle_reference_claim(identifier, claim, committed=committed,
                                settled_scope=chat_scope if committed else chat_scope)
                        elif isinstance(claim, str) and claim.startswith(attempt_prefix):
                            handler.settle_reference_claim(identifier, claim, committed=committed,
                                settled_scope=chat_scope)
            except Exception:
                # Retain unresolved claims for explicit reconciliation.
                continue

    def _receipt_scope(owner, host_chat_id, request_id):
        return input_receipt_claim_scope(owner, host_chat_id, request_id)

    def _receipt_attempt_prefix(owner, host_chat_id, request_id):
        return input_receipt_attempt_prefix(owner, host_chat_id, request_id)

    def freeze_model_request_options(owner, host_chat_id, actor, value, receipts):
        from core.database import Session as DBSession, SessionLocal
        from src.openclank.chat_routing import request_option_route_id
        from src.openclank.provider_request_options import (
            ProviderRequestOptionError, project_request_options,
            restore_session_request_options, restore_child_receipt_options, validate_request_option_selection,
        )
        db = SessionLocal()
        try:
            host_session = db.query(DBSession).filter(
                DBSession.id == host_chat_id,
                DBSession.owner == str(owner).strip().lower(),
            ).first()
            if host_session is None:
                raise InputRequestError("owner-bound chat route is unavailable")
            route_id = str(getattr(host_session, "provider_model_route_id", None) or "").strip()
            model = actor.get("effectiveModel")
            if actor.get("mode") == "main":
                if not route_id:
                    raise InputRequestError("chat has no durable provider route")
                route = resolve_chat_route(
                    owner=owner, endpoint_id=getattr(host_session, "endpoint_id", None),
                    model_id=getattr(host_session, "model", None), model_route_id=route_id,
                )
            else:
                from src.openclank.chat_routing import shared_endpoint_id
                context = actor.get("hostContext") or {}
                if not isinstance(model, dict) or not model.get("providerID") or not model.get("modelID"):
                    raise InputRequestError("the actor model is unavailable")
                grant = context.get("grantID")
                route = resolve_chat_route(owner=owner,
                    endpoint_id=shared_endpoint_id(grant) if grant else model["providerID"],
                    model_id=model["modelID"])
            model = actor.get("effectiveModel")
            if not isinstance(model, dict) or model.get("providerID") != route.connection_id or model.get("modelID") != route.provider_model_id:
                raise InputRequestError("the actor model no longer matches the saved chat route")
            option_route_id = request_option_route_id(route)
            revision = max(1, int(route.catalog_revision or 1))
            descriptors = project_request_options(route.capabilities, catalog_revision=revision, source="engine-catalog")
            if "modelOptions" in value:
                selected = value["modelOptions"]
                options = validate_request_option_selection(
                    route_id=option_route_id,
                    catalog_revision=revision,
                    descriptors=descriptors,
                    selection=selected,
                )
            elif actor.get("mode") == "main":
                options = restore_session_request_options(
                    host_chat_id, owner,
                    route_id=route.model_route_id,
                    option_route_id=option_route_id,
                    catalog_revision=revision,
                    descriptors=descriptors,
                )
            else:
                options = restore_child_receipt_options(receipts, route_id=route.model_route_id,
                    option_route_id=option_route_id, catalog_revision=revision, descriptors=descriptors,
                    provider_id=route.connection_id, model_id=route.provider_model_id,
                    actor_snapshot=actor.get("modelRequestOptions"))
        except ProviderRequestOptionError as exc:
            raise InputRequestError(str(exc)) from exc
        except ChatRouteUnavailable as exc:
            raise InputRequestError("the saved chat route is unavailable for frozen input delivery") from exc
        finally:
            db.close()
        return {
            "present": "modelOptions" in value,
            "routeID": route.model_route_id,
            "optionRouteID": option_route_id,
            "catalogRevision": revision,
            "model": {"providerID": route.connection_id, "modelID": route.provider_model_id},
            "options": options,
        }

    @router.post("/{host_chat_id}/actors/{actor_id}/display-name")
    async def rename_actor_display(request: Request, host_chat_id: str, actor_id: str):
        """A named human changes one current, positively bound actor label."""
        from fastapi.responses import JSONResponse
        from src.auth_helpers import require_user
        from routes.session_routes import _validated_session_workspace, _verify_session_owner
        def response(state): return JSONResponse({'state':state},headers={'Cache-Control':'private, no-store'})
        if getattr(request.state,'api_token',None) or getattr(request.state,'internal_tool_authenticated',False):
            raise HTTPException(403, 'Interactive human authentication required')
        owner = require_user(request)
        auth=getattr(request.app.state,'auth_manager',None); barrier=getattr(request.app.state,'account_request_barrier',None)
        account=auth.account_id(owner) if auth else None
        if not account or not barrier or auth.username_for_account_id(account)!=owner: return response('unavailable')
        try:
            body=await request.json()
            if not isinstance(body,dict) or set(body)!={'label','executionRevision','displayRevision','actorSessionID','mapRevision'}: return response('unavailable')
            label=body['label']; execution=body['executionRevision']; revision=body['displayRevision']; native=body['actorSessionID']; map_revision=body['mapRevision']
            if (not isinstance(label,str) or not label.strip() or len(label.encode('utf-8'))>200
                    or any(ord(ch)<32 or 127<=ord(ch)<=159 for ch in label)
                    or any(type(v) is not int or v<0 or v>9007199254740991 for v in (execution,revision,map_revision))
                    or not isinstance(native,str) or not native or len(native)>256 or not actor_id or len(actor_id)>256): return response('unavailable')
            current_owner,supervisor,binding,mapping=get_binding(request,host_chat_id,map_revision)
            if current_owner!=owner: return response('unavailable')
            pool=supervisor.owner_lifecycle_pool(); worker=pool.worker_for_owner(owner); bridge=getattr(worker,'_bridge',None) if worker else None
            process=getattr(worker,'_proc',None) if worker else None; generation=getattr(worker,'installed_generation',None) if worker else None
            owner_key=pool._key(owner); runtime_state=pool._states.get(owner_key); owner_epoch=pool._owner_lifecycle_epoch(owner_key)
            holder=getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None) if worker else None
            def runtime_current():
                return (supervisor.owner_lifecycle_pool() is pool and pool._states.get(owner_key) is runtime_state
                    and runtime_state is not None and runtime_state.status=='ready' and runtime_state.active is worker
                    and runtime_state.generation==generation and pool._owner_lifecycle_epoch(owner_key)==owner_epoch
                    and pool.worker_for_owner(owner) is worker and worker._bridge is bridge and worker._proc is process
                    and worker.installed_generation==generation and worker.is_alive() and holder
                    and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)==holder
                    and getattr(bridge,'_managed_provider_generation',None)==holder)
            session=session_manager.get_session(host_chat_id); workspace_id=getattr(session,'workspace_id',None) if session else None
            if (not worker or not bridge or not process or not worker.is_alive() or not session or session.owner!=owner
                    or not runtime_current() or getattr(session,'incognito',False) or getattr(session,'archived',False) or not workspace_id): return response('unavailable')
            workspace=await asyncio.to_thread(_validated_session_workspace,request,owner,workspace_id)
            async with barrier.admitted(owner):
                async with mapping.async_chat_admission_lock(host_chat_id,blocking=False,existing_only=True):
                    if (auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
                            or not runtime_current()
                            or session_manager.get_session(host_chat_id) is not session or session.owner!=owner
                            or getattr(session,'workspace_id',None)!=workspace_id or getattr(session,'archived',False)
                            or getattr(session,'incognito',False)): return response('unavailable')
                    ensure_current(mapping,owner,host_chat_id,binding)
                    current=await asyncio.to_thread(_validated_session_workspace,request,owner,workspace_id)
                    if current.path!=workspace.path: return response('unavailable')
                    payload={'accountID':account,'chatID':host_chat_id,'workspaceID':workspace_id,
                        'executionRevision':execution,'displayRevision':revision,'label':label}
                    try:
                        result=await worker.session_actor_display_request(host_chat_id,native,workspace.path,actor_id,payload,owner=owner)
                        await asyncio.to_thread(_verify_session_owner,request,host_chat_id,session_manager)
                        ensure_current(mapping,owner,host_chat_id,binding)
                        current=await asyncio.to_thread(_validated_session_workspace,request,owner,workspace_id)
                        if (auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
                                or not runtime_current()
                                or session_manager.get_session(host_chat_id) is not session or current.path!=workspace.path): return response('unknown')
                        if result.status_code==409:
                            return response('unavailable' if result.json()=={'state':'owner_unknown'} else 'conflict')
                        if result.status_code==200 and result.json()=={'state':'applied'}: return response('applied')
                        return response('unknown')
                    except Exception:
                        return response('unknown')
        except Exception:
            return response('unavailable')

    @router.get("/{host_chat_id}/actors/{actor_id}/input")
    async def export_actor_input(request: Request, host_chat_id: str, actor_id: str, map_revision: int,
                                 actor_session_id: str | None = None):
        owner, supervisor, binding, mapping = get_binding(request, host_chat_id, map_revision)
        session_id = actor_session_id or binding.engine_session_id
        if session_id != binding.engine_session_id:
            await ensure_direct_child(supervisor, binding, owner, session_id)
        actor = find_session_actor(await actor_list(supervisor, binding, owner, session_id), actor_id, session_id)
        if actor is None or not isinstance(actor.get("hostContext"), dict) or actor["hostContext"].get("chatID") != host_chat_id:
            raise HTTPException(404, {"code": "ACTOR_NOT_FOUND", "message": "Actor is not registered for this chat"})
        receipts = await input_receipts(supervisor, binding, owner, session_id, actor_id)
        for receipt in receipts:
            if receipt.get("state") == "consumed":
                settle_upload_claims(owner, host_chat_id, receipt, committed=True)
            elif receipt.get("state") == "cancelled":
                settle_upload_claims(owner, host_chat_id, receipt, committed=False)
        ensure_current(mapping, owner, host_chat_id, binding)
        return {"actorSessionId": session_id, "receipts": receipts}

    @router.get("/{host_chat_id}/actors/{actor_id}/questions")
    async def read_actor_questions(request: Request, host_chat_id: str, actor_id: str, map_revision: int,
                                   actor_session_id: str | None = None, request_id: str | None = None,
                                   before: str | None = None, limit: int = 50):
        owner, supervisor, binding, mapping = get_binding(request, host_chat_id, map_revision)
        session_id = actor_session_id or binding.engine_session_id
        if session_id != binding.engine_session_id:
            await ensure_direct_child(supervisor, binding, owner, session_id)
        actor = find_session_actor(await actor_list(supervisor, binding, owner, session_id), actor_id, session_id)
        if actor is None or not isinstance(actor.get("hostContext"), dict) or actor["hostContext"].get("chatID") != host_chat_id:
            raise HTTPException(404, {"code": "ACTOR_NOT_FOUND", "message": "Actor is not registered for this chat"})
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise HTTPException(400, {"code": "INVALID_QUESTION_PAGE", "message": "Question page limit must be from 1 to 100"})
        suffix = f"actors/{quote(actor_id, safe='')}/questions?limit={limit}"
        if request_id:
            suffix += f"&requestID={quote(request_id, safe='')}"
        if before:
            suffix += f"&before={quote(before, safe='')}"
        page, _ = await actor_input_request(supervisor, binding, owner, session_id, "GET", suffix)
        if (not isinstance(page, dict) or not isinstance(page.get("items"), list)
                or any(not isinstance(item, dict) or item.get("actorID") != actor_id or item.get("sessionID") != session_id
                       or not isinstance(item.get("questionRef"), str)
                       or (item.get('tool') is not None and (type(item['tool']) is not dict
                           or set(item['tool']) != {'messageID','callID'}
                           or any(not isinstance(item['tool'][k], str) or not item['tool'][k] or len(item['tool'][k]) > 200 for k in ('messageID','callID'))))
                       for item in page["items"])
                or (page.get("nextCursor") is not None and not isinstance(page.get("nextCursor"), str))):
            raise HTTPException(502, {"code": "ENGINE_INVALID_RESPONSE", "message": "Managed engine returned invalid question projection data"})
        ensure_current(mapping, owner, host_chat_id, binding)
        return {"actorSessionId": session_id, **page}

    @router.delete("/{host_chat_id}/actors/{actor_id}/input/{request_id}")
    async def cancel_actor_input(request: Request, host_chat_id: str, actor_id: str, request_id: str,
                                 map_revision: int, actor_session_id: str | None = None):
        owner, supervisor, binding, mapping = get_binding(request, host_chat_id, map_revision)
        session_id = actor_session_id or binding.engine_session_id
        if session_id != binding.engine_session_id:
            await ensure_direct_child(supervisor, binding, owner, session_id)
        receipt = next((item for item in await input_receipts(supervisor, binding, owner, session_id, actor_id)
                        if item.get("requestID") == request_id), None)
        if receipt is None:
            raise HTTPException(404, {"code": "INPUT_RECEIPT_NOT_FOUND", "message": "Input receipt is unavailable"})
        suffix = f"actors/{quote(actor_id, safe='')}/input/{quote(request_id, safe='')}"
        result, _ = await actor_input_request(supervisor, binding, owner, session_id, "DELETE", suffix)
        cancelled = isinstance(result, dict) and result.get("cancelled") is True
        if cancelled:
            settle_upload_claims(owner, host_chat_id, receipt, committed=False)
            try:
                pool = supervisor.owner_lifecycle_pool()
                worker = pool.worker_for_owner(owner)
                bridge = getattr(worker, '_bridge', None) if worker else None
                if bridge is not None:
                    await bridge._forget_receipt_origin(owner=owner, chat=host_chat_id, root_session=binding.engine_session_id,
                        session=session_id, actor=actor_id, request_id=request_id)
            except Exception:
                pass
        ensure_current(mapping, owner, host_chat_id, binding)
        return {"requestID": request_id, "cancelled": cancelled}

    @router.post("/{host_chat_id}/actors/{actor_id}/input")
    async def submit_actor_input(request: Request, host_chat_id: str, actor_id: str, map_revision: int):
        from src.openclank.tui_interactions import TuiInputGuard
        tui_guard = getattr(request.state, 'tui_input_guard', None)
        if tui_guard is not None and (type(tui_guard) is not TuiInputGuard or not tui_guard.valid(request)):
            raise HTTPException(403, 'TUI input authority unavailable')
        connector_identity = getattr(request.state, "connector_session_identity", None)
        if connector_identity is not None:
            from src.openclank.agent_session_gateway import capture_identity
            fresh_identity = capture_identity(request, "sessions:write")
            if (fresh_identity.owner != connector_identity.owner or fresh_identity.account_id != connector_identity.account_id
                    or fresh_identity.credential_kind != connector_identity.credential_kind
                    or fresh_identity.scopes != connector_identity.scopes):
                raise HTTPException(401, "Connector credential changed")
        owner, supervisor, binding, mapping = get_binding(request, host_chat_id, map_revision)
        def connector_current():
            if connector_identity is not None:
                if owner != connector_identity.owner or not connector_identity.current():
                    raise HTTPException(401, "Connector credential changed")
                ensure_current(mapping, owner, host_chat_id, binding)
                if getattr(request.state, "connector_queued_input", False):
                    from routes.prefs_routes import _load_for_user
                    if _load_for_user(owner).get("input_queue_enabled") is not True:
                        raise HTTPException(409, "Queued input is disabled")
        connector_current()
        from src.openclank.account_request_barrier import _current_admission
        ingress_admission = _current_admission.get()
        ingress_barrier = getattr(request.app.state, 'account_request_barrier', None)
        # Freeze the authenticated subject before the first await. A missing
        # proof never blocks ordinary steering.
        ingress_account = None
        try:
            auth = getattr(request.app.state, 'auth_manager', None)
            if getattr(request.state, 'authenticated', False) and auth is not None:
                candidate = auth.account_id(owner)
                if candidate and auth.username_for_account_id(candidate) == owner:
                    ingress_account = candidate
        except Exception:
            pass
        ingress_auth = getattr(request.app.state, 'auth_manager', None)
        ingress_repository = getattr(request.app.state, 'files_policy_repository', None)
        credential_factory = getattr(request.app.state, 'capture_original_credential', None)
        ingress_credential = None
        try:
            if ingress_account and callable(credential_factory):
                candidate = credential_factory(request, owner, ingress_account)
                if callable(candidate) and candidate() is True:
                    ingress_credential = candidate
        except Exception:
            pass
        # Keep the original request bytes and auth classification across body parsing;
        # expensive token proof is only needed if this is feedback delivery.
        ingress_headers = tuple(request.scope.get('headers', ()))
        ingress_auth_state = tuple(getattr(request.state, name, None) for name in (
            'authenticated', 'internal_tool_authenticated', 'api_token', 'api_token_id',
            'api_token_owner', 'api_token_client_kind'))
        ingress_scopes = tuple(getattr(request.state, 'api_token_scopes', ()) or ())
        feedback_credential = None
        try:
            pool = supervisor.owner_lifecycle_pool()
            worker = pool.worker_for_owner(owner)
        except Exception:
            worker = None
        bridge = getattr(worker, "_bridge", None)
        origin_context = bridge._session_context.get(binding.engine_session_id) if bridge is not None else None
        origin_holder = bridge._managed_provider_generation if bridge is not None else None
        origin_process = getattr(worker, '_proc', None) if worker is not None else None
        origin_generation = getattr(worker, 'installed_generation', None) if worker is not None else None
        if bridge is not None:
            install_input_policy_resolver(request, bridge, session_manager)
        feedback_id = None
        feedback_intent = None
        feedback_current = None
        feedback_files_generation = None
        try:
            raw_value = await request.json()
            if isinstance(raw_value, dict) and 'planFeedbackID' in raw_value:
                if set(raw_value) not in ({'planFeedbackID', 'mode'}, {'planFeedbackID', 'mode', 'turnPolicy'}):
                    raise InputRequestError('feedback delivery scope unavailable')
                feedback_id = raw_value['planFeedbackID']
                if (request.app.state.auth_manager is not ingress_auth
                        or getattr(request.app.state, 'capture_original_credential', None) is not credential_factory
                        or tuple(request.scope.get('headers', ())) != ingress_headers
                        or tuple(getattr(request.state, name, None) for name in (
                            'authenticated', 'internal_tool_authenticated', 'api_token', 'api_token_id',
                            'api_token_owner', 'api_token_client_kind')) != ingress_auth_state
                        or tuple(getattr(request.state, 'api_token_scopes', ()) or ()) != ingress_scopes):
                    raise InputRequestError('feedback credential changed')
                if ingress_account and credential_factory:
                    feedback_credential = credential_factory(request, owner, ingress_account)
                if (not isinstance(feedback_id, str) or len(feedback_id) != 32 or not ingress_account
                        or not ingress_barrier or not ingress_admission or not feedback_credential
                        or not await ingress_barrier.is_live(owner, ingress_admission)):
                    raise InputRequestError('feedback account unavailable')
                auth = ingress_auth
                def feedback_facts_current():
                    try:
                        ensure_current(mapping, owner, host_chat_id, binding)
                        return (not ingress_admission.released and request.app.state.auth_manager is auth
                                and request.app.state.account_request_barrier is ingress_barrier
                                and getattr(request.app.state, 'files_policy_repository', None) is ingress_repository
                                and auth.account_id(owner) == ingress_account
                                and auth.username_for_account_id(ingress_account) == owner
                                and (feedback_files_generation is None or ingress_repository.generation()==feedback_files_generation))
                    except Exception:
                        return False
                def feedback_current():
                    return feedback_facts_current() and feedback_credential()
                from src.openclank.plan_feedback import claim_delivery, inspect as inspect_feedback
                snapshot=inspect_feedback(host_chat_id,owner,ingress_account,feedback_id=feedback_id,current_authority=feedback_current)
                feedback_policy=None
                if snapshot['intent'] is None:
                    from src.tool_policy import normalize_turn_policy_snapshot
                    feedback_policy=normalize_turn_policy_snapshot(raw_value.get('turnPolicy'))
                    reason=unsupported_receipt_policy(feedback_policy,legacy=True) or ('Research follow-ups are unavailable in this actor context.' if feedback_policy['useResearch'] else None)
                    if reason: raise InputRequestError(reason)
                saved_scope=snapshot['target'].get('scope') if snapshot['target'].get('kind') in ('resource','resource_batch') else None
                if saved_scope and saved_scope.get('version')==2:
                    if actor_id!=saved_scope['actorID']: raise InputRequestError('feedback actor changed')
                    feedback_session=saved_scope['actorSessionID']
                else:
                    if actor_id!='main': raise InputRequestError('feedback actor changed')
                    feedback_session=binding.engine_session_id
                verified_file=None; verified_design=None; verified_standard=None
                if snapshot['intent'] is None and snapshot['target'].get('kind') == 'resource_batch':
                    from src.openclank.plan_feedback_files import capture_feedback_resource
                    from src.openclank.plan_feedback import recapture_feedback_scope
                    feedback_files_generation=request.app.state.files_policy_repository.generation();target=snapshot['target']
                    if await recapture_feedback_scope(request,host_chat_id,owner,ingress_account,session_manager,target['scope'],current_authority=feedback_current)!=target['scope']:raise ValueError('feedback scope changed')
                    verified_file=[]
                    for item in target['items']:
                        frozen={k:item[k] for k in ('resource','anchor','anchorVerification') if k in item}
                        verified_file.append(await capture_feedback_resource(request,target['binding'],ingress_account,frozen=frozen))
                        if not feedback_current():raise ValueError('feedback account changed')
                    for index,item in enumerate(target['items']):
                        frozen={k:item[k] for k in ('resource','anchor','anchorVerification') if k in item}
                        if await capture_feedback_resource(request,target['binding'],ingress_account,frozen=frozen)!=verified_file[index] or not feedback_current():raise ValueError('feedback batch changed')
                    if await recapture_feedback_scope(request,host_chat_id,owner,ingress_account,session_manager,target['scope'],current_authority=feedback_current)!=target['scope']:raise ValueError('feedback scope changed')
                elif snapshot['intent'] is None and snapshot['target'].get('kind') == 'resource':
                    from src.openclank.plan_feedback_files import capture_feedback_resource
                    feedback_files_generation=request.app.state.files_policy_repository.generation()
                    from src.openclank.plan_feedback import recapture_feedback_scope
                    if await recapture_feedback_scope(request,host_chat_id,owner,ingress_account,session_manager,snapshot['target'].get('scope'),current_authority=feedback_current)!=snapshot['target'].get('scope'): raise ValueError('feedback scope changed')
                    verified_file=await capture_feedback_resource(request,snapshot['target']['binding'],ingress_account,frozen=snapshot['target'],require_write=('designContext' in snapshot['target'] and snapshot['target']['designContext']['output']['destination']=='source'))
                    if 'designContext' in snapshot['target']:
                        from src.openclank.plan_feedback_files import capture_design_context
                        verified_design,verified_standard=await capture_design_context(request,snapshot['target']['binding'],ingress_account,snapshot['target']['designContext'],verified_file['resource'],frozen=True,return_standard=True)
                    if await recapture_feedback_scope(request,host_chat_id,owner,ingress_account,session_manager,snapshot['target'].get('scope'),current_authority=feedback_current)!=snapshot['target'].get('scope'): raise ValueError('feedback scope changed')
                elif snapshot['intent'] is None and snapshot['target'].get('file') is not None:
                    from src.openclank.plan_feedback_files import capture_feedback_file
                    feedback_files_generation=request.app.state.files_policy_repository.generation()
                    verified_file=await capture_feedback_file(request,snapshot['target'],ingress_account,frozen=snapshot['target']['file'])
                feedback_intent = claim_delivery(host_chat_id, owner, ingress_account, feedback_id=feedback_id,
                    actor=actor_id, native_session=feedback_session, map_revision=map_revision,
                    mode=raw_value['mode'], current_authority=feedback_current,verified_file=verified_file,verified_design=verified_design,verified_standard=verified_standard,turn_policy=feedback_policy)
                raw_value = {'requestID': feedback_intent['request_id'], 'text': feedback_intent['text'],
                             'mode': feedback_intent['mode'], 'actorSessionId': feedback_intent['native_session'],
                             **({'turnPolicy':feedback_intent['turnPolicy']} if 'turnPolicy' in feedback_intent else {})}
            value = normalize_admission(raw_value)
        except InputRequestError as exc:
            raise HTTPException(400, {"code": "INVALID_INPUT_RECEIPT", "message": str(exc)}) from exc
        except Exception as exc:
            raise HTTPException(400, {"code": "INVALID_INPUT_RECEIPT", "message": "Input request must be valid JSON"}) from exc
        session_id = value.get("actorSessionId", binding.engine_session_id)
        if session_id != binding.engine_session_id:
            await ensure_direct_child(supervisor, binding, owner, session_id)
        actors = await actor_list(supervisor, binding, owner, session_id)
        actor = find_session_actor(actors, actor_id, session_id)
        if actor is None:
            raise HTTPException(404, {"code": "ACTOR_NOT_FOUND", "message": "Actor is not registered in this session"})
        host_context = actor.get("hostContext")
        if not isinstance(host_context, dict) or host_context.get("chatID") != host_chat_id:
            raise HTTPException(409, {"code": "ACTOR_CHAT_BINDING_MISMATCH", "message": "Actor is not bound to this host chat"})
        if actor.get("status") == "pending":
            raise HTTPException(409, {"code": "ACTOR_NOT_READY", "message": "Actor has not completed initial registration"})
        if connector_identity is not None and actor.get("executionRevision") != getattr(request.state, "connector_execution_revision", None):
            raise HTTPException(409, "Actor execution revision changed")
        suffix = f"actors/{quote(actor_id, safe='')}/input"
        receipts = await input_receipts(supervisor, binding, owner, session_id, actor_id)
        existing = next((item for item in receipts if item.get("requestID") == value["requestID"]), None)
        if feedback_intent is not None and not feedback_intent['fresh'] and 'turnPolicy' not in feedback_intent:
            return {'deliveryState':'unavailable','deliveryReason':'This older feedback lacks a recorded memory policy and cannot be safely delivered.','source':'managed_engine_receipt'}
        if feedback_intent is not None and not feedback_intent['fresh']:
            if (existing is None or existing.get('mode') != value['mode'] or not same_client_receipt(existing, value, session_id)
                    or existing.get('owner') != owner or existing.get('stableChatID') != host_chat_id
                    or existing.get('actorID') != actor_id):
                return {'deliveryState': 'unknown', 'source': 'managed_engine_receipt'}
            from src.openclank.plan_feedback import observe_delivery
            state = _feedback_receipt_state(existing)
            observed = observe_delivery(host_chat_id, owner, ingress_account, feedback_id=feedback_id,
                intent=feedback_intent, state=state, current_authority=feedback_current, queue_proof=_feedback_queue_proof(existing))
            return {'deliveryState': observed['delivery'], 'source': 'managed_engine_receipt'}
        if existing is not None:
            if feedback_intent is not None:
                return {'deliveryState': 'unknown', 'source': 'managed_engine_receipt'}
            if not same_client_receipt(existing, value, session_id):
                raise HTTPException(409, {"code": "INPUT_RECEIPT_CONFLICT", "message": "requestID is already bound to different client content"})
            receipt = existing
            if (existing.get("selectedExecutionRevision") is not None
                    and existing.get("mapRevision") != map_revision):
                return {"receipt": existing, "delivery": "persisted", "deliveryState": "unavailable",
                        "deliveryReason": "The original selected chat mapping is no longer current.",
                        "actorSessionId": session_id, "source": "managed_engine_receipt"}
            unavailable = unsupported_receipt_policy(existing.get("turnPolicy"), legacy=True)
            if unavailable:
                ensure_current(mapping, owner, host_chat_id, binding)
                if existing.get("state") in {"persisted", "pending-boundary"}:
                    return {"receipt": existing, "delivery": "persisted", "deliveryState": "unavailable",
                            "deliveryReason": unavailable, "actorSessionId": session_id, "source": "managed_engine_receipt"}
                return {"receipt": existing, "delivery": "acknowledged", "actorSessionId": session_id,
                        "source": "managed_engine_receipt"}
            if value["mode"] == "steer" and existing.get("mode") == "after-turn" and existing.get("state") in {"persisted", "pending-boundary"}:
                promotion = {
                    "requestID": existing["requestID"],
                    "executionRevision": existing["executionRevision"],
                    **({"originTurnID": existing["originTurnID"]} if existing.get("originTurnID") else {}),
                    "text": "" if existing.get("questionAnswers") else existing.get("text", ""),
                    "attachmentRefs": existing.get("attachmentRefs", []),
                    "attachmentUploadIDs": existing.get("attachmentUploadIDs", []),
                    "attachmentParts": [],
                    "questionRefs": existing.get("questionRefs", []),
                    "questionAnswers": existing.get("questionAnswers", []),
                    "modelRequestOptions": existing.get("modelRequestOptions", {"present": False}),
                    **({"turnPolicy": existing["turnPolicy"]} if existing.get("turnPolicy") is not None else {}),
                    **({"effectivePolicyCeiling": existing["effectivePolicyCeiling"]} if existing.get("effectivePolicyCeiling") is not None else {}),
                    "mode": "steer",
                }
                connector_current()
                promoted, _ = await actor_input_request(supervisor, binding, owner, session_id, "POST", suffix, promotion)
                if not isinstance(promoted, dict) or promoted.get("mode") not in {None, "steer"}:
                    raise HTTPException(502, {"code": "ENGINE_INVALID_RESPONSE", "message": "Managed engine did not confirm follow-up promotion"})
                receipt = {**existing, "mode": "steer", "state": promoted.get("state", existing["state"])}
        else:
            if ("expectedExecutionRevision" in value
                    and actor.get("executionRevision") != value["expectedExecutionRevision"]):
                raise HTTPException(409, {"code": "ACTOR_EXECUTION_CHANGED", "message": "The selected worker execution changed before admission."})
            unavailable = unsupported_receipt_policy(value.get("turnPolicy"))
            if unavailable:
                raise HTTPException(409, {"code": "INPUT_POLICY_UNAVAILABLE", "message": unavailable})
            claimed_uploads = []
            upload_handler = None
            try:
                if value["uploadIDs"]:
                    from src.tool_utils import get_upload_handler
                    upload_handler = get_upload_handler()
                    stable_scope = _receipt_scope(owner, host_chat_id, value["requestID"])
                    attempt_prefix = _receipt_attempt_prefix(owner, host_chat_id, value["requestID"])
                    scope = attempt_prefix + uuid4().hex
                    def claim_all():
                        claimed_ids = []
                        for identifier in value["uploadIDs"]:
                            claimed = upload_handler.claim_upload_reference(identifier, owner=owner, reference_scope=scope) if upload_handler else None
                            if not claimed:
                                for claimed_id in claimed_ids:
                                    upload_handler.settle_reference_claim(claimed_id, scope, committed=False)
                                raise InputRequestError("an uploaded attachment is unavailable for this owner")
                            claimed_ids.append(identifier)
                        return claimed_ids
                    claimed_uploads = await asyncio.to_thread(claim_all)
                attachment_parts = await asyncio.to_thread(
                    materialize_upload_content, value["uploadIDs"], owner=owner, upload_handler=upload_handler,
                )
                suffix = f"message?limit=1000&agent_id={quote(actor_id, safe='')}"
                if session_id == binding.engine_session_id:
                    messages, _ = await native_request(supervisor, binding, owner, suffix)
                else:
                    messages, _ = await child_native_request(supervisor, binding, owner, session_id, suffix)
                if not isinstance(messages, list):
                    raise HTTPException(502, {"code": "ENGINE_INVALID_RESPONSE", "message": "Managed engine returned an invalid origin transcript"})
                origin_turn_id = next((
                    info.get("id") for item in reversed(messages) if isinstance(item, dict)
                    and isinstance((info := item.get("info")), dict) and info.get("role") == "user"
                    and isinstance(info.get("id"), str)
                ), None)
                frozen_options = (option_resolver(owner, host_chat_id, actor, value) if option_resolver
                                  else freeze_model_request_options(owner, host_chat_id, actor, value, receipts))
                ceiling = effective_receipt_policy_ceiling(request, owner, host_chat_id, value, session_manager)
                queued_proof = None
                try:
                    choices = value.get('turnPolicy')
                    if (ingress_account and actor_id == 'main' and session_id == binding.engine_session_id
                        and type(choices) is dict and value['text'] and not value['attachmentRefs']
                        and not value['uploadIDs'] and not value['questionRefs'] and not value.get('questionAnswers')
                        and not attachment_parts and all(choices.get(k) is False for k in ('incognito','noMemory','compareMode'))):
                        from src.openclank.memory_policy import STORE
                        from src.openclank.queued_source import make_queued_source_proof
                        from src.openclank.transcript_projection import get_managed_binding
                        from hashlib import sha256
                        auth = request.app.state.auth_manager
                        ensure_current(mapping, owner, host_chat_id, binding)
                        frozen_binding = get_managed_binding(host_chat_id, owner=owner)
                        if (auth.account_id(owner) != ingress_account or auth.username_for_account_id(ingress_account) != owner
                            or frozen_binding.get('engineSessionID') != session_id or frozen_binding.get('stableChatID') != host_chat_id):
                            raise ValueError('queued ingress changed')
                        privileges = auth.get_privileges(owner) or {}
                        privilege = bool(privileges.get('can_manage_memory',True))
                        policy = STORE.effective_snapshot(owner,read_privilege=privilege,write_privilege=privilege,
                            incognito=choices['incognito'],no_memory=choices['noMemory'],compare_mode=choices['compareMode'])
                        if policy.get('eligibility_state') != 'available' or type(policy.get('capture_allowed')) is not bool:
                            raise ValueError('queued capture policy unavailable')
                        queued_proof = make_queued_source_proof({'version':1,'owner':owner,'accountID':ingress_account,
                            'stableChatID':host_chat_id,'sessionID':session_id,'actorID':actor_id,'requestID':value['requestID'],
                            'bodySHA256':sha256(value['text'].encode('utf-8')).hexdigest(),'choices':choices,
                            'captureEligible':policy['capture_allowed'],'policyFingerprint':policy.get('fingerprint'),
                            'workspaceID':frozen_binding.get('authorityWorkspaceID'),
                            'mappingRevision':frozen_binding.get('mappingRevision'),'workspaceRevision':frozen_binding.get('workspaceRevision'),
                            'executionRevision':actor.get('executionRevision')})
                except Exception:
                    queued_proof = None
                payload = native_admission(value, actor, origin_turn_id, attachment_parts=attachment_parts,
                                           model_request_options=frozen_options, effective_policy_ceiling=ceiling,
                                           queued_source_proof=queued_proof)
                # The opaque nonce is stored only by the native insert winner.
                # It carries no credential and does not affect idempotency.
                ingress_nonce = uuid4().hex if ingress_credential is not None and bridge is not None else None
                ingress_issued_at = time.monotonic() if ingress_nonce is not None else None
                if ingress_nonce is not None:
                    payload['firstIngressNonce'] = ingress_nonce
                suffix = f"actors/{quote(actor_id, safe='')}/input"
                if feedback_intent is not None:
                    from src.openclank.plan_feedback import claim_delivery, inspect as inspect_feedback
                    snapshot=inspect_feedback(host_chat_id,owner,ingress_account,feedback_id=feedback_id,current_authority=feedback_current)
                    verified_file=None; verified_design=None; verified_standard=None
                    if snapshot['target'].get('kind') == 'resource_batch':
                        from src.openclank.plan_feedback_files import capture_feedback_resource
                        from src.openclank.plan_feedback import recapture_feedback_scope
                        if feedback_files_generation is None:feedback_files_generation=request.app.state.files_policy_repository.generation()
                        target=snapshot['target']
                        if await recapture_feedback_scope(request,host_chat_id,owner,ingress_account,session_manager,target['scope'],current_authority=feedback_current)!=target['scope']:raise ValueError('feedback scope changed')
                        verified_file=[]
                        for item in target['items']:
                            frozen={k:item[k] for k in ('resource','anchor','anchorVerification') if k in item}
                            verified_file.append(await capture_feedback_resource(request,target['binding'],ingress_account,frozen=frozen))
                            if not feedback_current():raise ValueError('feedback account changed')
                        for index,item in enumerate(target['items']):
                            frozen={k:item[k] for k in ('resource','anchor','anchorVerification') if k in item}
                            if await capture_feedback_resource(request,target['binding'],ingress_account,frozen=frozen)!=verified_file[index] or not feedback_current():raise ValueError('feedback batch changed')
                        if await recapture_feedback_scope(request,host_chat_id,owner,ingress_account,session_manager,target['scope'],current_authority=feedback_current)!=target['scope']:raise ValueError('feedback scope changed')
                    elif snapshot['target'].get('kind') == 'resource':
                        from src.openclank.plan_feedback_files import capture_feedback_resource
                        if feedback_files_generation is None: feedback_files_generation=request.app.state.files_policy_repository.generation()
                        from src.openclank.plan_feedback import recapture_feedback_scope
                        if await recapture_feedback_scope(request,host_chat_id,owner,ingress_account,session_manager,snapshot['target'].get('scope'),current_authority=feedback_current)!=snapshot['target'].get('scope'): raise ValueError('feedback scope changed')
                        verified_file=await capture_feedback_resource(request,snapshot['target']['binding'],ingress_account,frozen=snapshot['target'],require_write=('designContext' in snapshot['target'] and snapshot['target']['designContext']['output']['destination']=='source'))
                        if 'designContext' in snapshot['target']:
                            from src.openclank.plan_feedback_files import capture_design_context
                            verified_design,verified_standard=await capture_design_context(request,snapshot['target']['binding'],ingress_account,snapshot['target']['designContext'],verified_file['resource'],frozen=True,return_standard=True)
                        if await recapture_feedback_scope(request,host_chat_id,owner,ingress_account,session_manager,snapshot['target'].get('scope'),current_authority=feedback_current)!=snapshot['target'].get('scope'): raise ValueError('feedback scope changed')
                    elif snapshot['target'].get('file') is not None:
                        from src.openclank.plan_feedback_files import capture_feedback_file
                        if feedback_files_generation is None:feedback_files_generation=request.app.state.files_policy_repository.generation()
                        verified_file=await capture_feedback_file(request,snapshot['target'],ingress_account,frozen=snapshot['target']['file'])
                    check = claim_delivery(host_chat_id, owner, ingress_account, feedback_id=feedback_id,
                        actor=actor_id, native_session=session_id, map_revision=map_revision,
                        mode=value['mode'], current_authority=feedback_current,verified_file=verified_file,verified_design=verified_design,verified_standard=verified_standard,turn_policy=feedback_intent.get('turnPolicy'),for_issue=True)
                    if check.get('fresh') or check['request_id'] != value['requestID']:
                        raise InputRequestError('feedback intent changed')
                if feedback_intent is not None and not feedback_current():
                    raise InputRequestError('feedback credential changed')
                connector_current()
                if tui_guard is not None and not tui_guard.valid(request):
                    raise HTTPException(403, 'TUI input authority changed')
                try:
                    receipt, _ = await actor_input_request(supervisor, binding, owner, session_id, "POST", suffix, payload)
                except HTTPException as exc:
                    if ("expectedExecutionRevision" in value and exc.status_code == 409
                            and isinstance(exc.detail, dict) and exc.detail.get("code") == "ENGINE_INPUT_FAILED"):
                        raise HTTPException(409, {"code": "INPUT_ADMISSION_REFUSED", "message": "The selected input was refused; inspect its saved receipt."}) from exc
                    raise
                if tui_guard is not None and not tui_guard.valid(request):
                    raise HTTPException(409, {'code':'TUI_INPUT_OUTCOME_UNKNOWN','message':'Input outcome is unknown; inspect its original receipt.'})
                if ingress_nonce is not None and isinstance(receipt, dict) and receipt.get('firstIngressNonce') == ingress_nonce:
                    try:
                        exact_origin = next((item for item in await input_receipts(supervisor, binding, owner, session_id, actor_id)
                            if item.get('requestID') == value['requestID']), None)
                        ensure_current(mapping, owner, host_chat_id, binding)
                        if (isinstance(exact_origin, dict) and same_client_receipt(exact_origin, value, session_id)
                                and ingress_credential() is True and pool.worker_for_owner(owner) is worker
                                and worker._bridge is bridge and worker.is_alive() and origin_context is not None
                                and bridge._session_context.get(binding.engine_session_id) is origin_context
                                and bridge._managed_provider_generation == origin_holder and origin_holder
                                and getattr(worker, '_proc', None) is origin_process
                                and getattr(worker, 'installed_generation', None) == origin_generation):
                            await bridge._retain_receipt_origin(owner=owner, account=ingress_account, chat=host_chat_id,
                                root_session=binding.engine_session_id, session=session_id, actor=actor_id,
                                request_id=value['requestID'], nonce=ingress_nonce, acknowledgement=receipt,
                                receipt=exact_origin, current=ingress_credential, issued_at=ingress_issued_at,
                                expected_context=origin_context, expected_holder=origin_holder)
                    except Exception:
                        pass  # Ordinary input does not depend on optional privileged witness.
                if upload_handler and claimed_uploads:
                    for identifier in claimed_uploads:
                        upload_handler.settle_reference_claim(identifier, scope, committed=True, settled_scope=stable_scope)
            except InputRequestError as exc:
                if upload_handler and claimed_uploads:
                    for identifier in claimed_uploads:
                        upload_handler.settle_reference_claim(identifier, scope, committed=False)
                raise HTTPException(409, {"code": "ACTOR_NOT_READY", "message": str(exc)}) from exc
            except HTTPException as exc:
                if exc.status_code < 500 and upload_handler and claimed_uploads:
                    for identifier in claimed_uploads:
                        upload_handler.settle_reference_claim(identifier, scope, committed=False)
                raise

        if feedback_intent is not None:
            exact = next((item for item in await input_receipts(supervisor, binding, owner, session_id, actor_id)
                          if item.get('requestID') == value['requestID']), None)
            if (isinstance(exact, dict) and exact.get('mode') == value['mode'] and same_client_receipt(exact, value, session_id)
                    and exact.get('owner') == owner and exact.get('stableChatID') == host_chat_id
                    and exact.get('actorID') == actor_id):
                from src.openclank.plan_feedback import observe_delivery
                observed_state = _feedback_receipt_state(exact)
                observe_delivery(host_chat_id, owner, ingress_account, feedback_id=feedback_id,
                    intent=feedback_intent, state=observed_state, current_authority=feedback_facts_current, queue_proof=_feedback_queue_proof(exact))
        # The actor may have settled while its receipt was being persisted. A
        # fresh actor read closes that boundary race with an explicit CAS resume.
        delivery = "persisted"
        current_actors = await actor_list(supervisor, binding, owner, session_id)
        current_actor = find_session_actor(current_actors, actor_id, session_id)
        if current_actor is None:
            raise HTTPException(502, {"code": "ACTOR_DISAPPEARED", "message": "Actor disappeared after receipt admission"})
        if (should_resume(current_actor.get("status")) or
            (current_actor.get("status") == "running" and current_actor.get("lastError") == "Managed execution requires reconciliation")):
            try:
                # POST returns only an acknowledgement; fetch the durable
                # receipt before deriving the fresh execution's ceiling.
                frozen_receipt = next((item for item in await input_receipts(supervisor, binding, owner, session_id, actor_id)
                    if item.get("requestID") == value["requestID"]), None)
                if (not isinstance(frozen_receipt, dict) or not same_client_receipt(frozen_receipt, value, session_id)
                    or frozen_receipt.get("owner") != owner or frozen_receipt.get("stableChatID") != host_chat_id
                    or frozen_receipt.get("actorID") != actor_id or not isinstance(frozen_receipt.get("effectivePolicyCeiling"), dict)):
                    raise InputRequestError("the frozen actor policy receipt is unavailable")
                root_operation_id = handoff_root_operation_id(owner, host_chat_id, value["requestID"], value["handoffID"])
                handoff_value = {**value, "rootOperationID": root_operation_id}
                handoff = native_handoff(handoff_value, current_actor)
                handoff_suffix = f"{suffix}/{quote(value['requestID'], safe='')}/resume"
                connector_current()
                retire_browser_policy_before_native_resume(owner, host_chat_id)
                needs_reconciliation = current_actor.get("lastError") == "Managed execution requires reconciliation"
                claim = {"kind": "route-required"} if needs_reconciliation else (await actor_input_request(
                    supervisor, binding, owner, session_id, "POST", handoff_suffix, handoff))[0]
                if isinstance(claim, dict) and claim.get("kind") == "existing-claim":
                    delivery = "acknowledged"
                    ensure_current(mapping, owner, host_chat_id, binding)
                    return {"receipt": receipt, "delivery": delivery, "actorSessionId": session_id,
                            "source": "managed_engine_receipt"}
                if not isinstance(claim, dict) or claim.get("kind") != "route-required":
                    ensure_current(mapping, owner, host_chat_id, binding)
                    return {"receipt": receipt, "delivery": "persisted", "deliveryState": "unavailable",
                            "deliveryReason": claim.get("reason", "ACTOR_RESUME_UNAVAILABLE") if isinstance(claim, dict) else "ACTOR_RESUME_UNAVAILABLE",
                            "actorSessionId": session_id, "source": "managed_engine_receipt"}
                from core.database import Session as DBSession, SessionLocal
                db = SessionLocal()
                try:
                    host_session = db.query(DBSession).filter(
                        DBSession.id == host_chat_id,
                        DBSession.owner == str(owner).strip().lower(),
                    ).first()
                    if host_session is None:
                        raise InputRequestError("owner-bound host session route is unavailable")
                    stored_route_id = str(getattr(host_session, "provider_model_route_id", None) or "").strip()
                    if current_actor.get("mode") == "main":
                        if not stored_route_id:
                            raise InputRequestError("the main chat has no durable provider route")
                        route = resolve_chat_route(
                            owner=owner, endpoint_id=getattr(host_session, "endpoint_id", None),
                            model_id=getattr(host_session, "model", None), model_route_id=stored_route_id,
                        )
                    else:
                        from src.openclank.chat_routing import shared_endpoint_id
                        model = current_actor.get("effectiveModel") or {}
                        actor_context = current_actor.get("hostContext") or {}
                        frozen_route = (frozen_receipt.get("modelRequestOptions") or {}).get("routeID")
                        if not model.get("providerID") or not model.get("modelID") or not frozen_route:
                            raise InputRequestError("the child actor frozen route is unavailable")
                        grant = actor_context.get("grantID")
                        route = resolve_chat_route(owner=owner,
                            endpoint_id=shared_endpoint_id(grant) if grant else model["providerID"],
                            model_id=model["modelID"], model_route_id=frozen_route)
                finally:
                    db.close()
                model = current_actor.get("effectiveModel")
                if not isinstance(model, dict) or model.get("providerID") != route.connection_id or model.get("modelID") != route.provider_model_id:
                    raise InputRequestError("the persisted main provider route no longer matches its effective model")
                actor_context = current_actor.get("hostContext")
                if not isinstance(actor_context, dict):
                    raise InputRequestError("the main actor host context is unavailable")
                actor_grant = actor_context.get("grantID")
                if actor_grant != route.provider_grant_id:
                    raise InputRequestError("the main provider grant changed")
                grant_revision = None
                if route.provider_grant_id:
                    from src.openclank.provider_store import ProviderStore
                    grant_revision = ProviderStore().resolve_share_scope(
                        recipient=owner, grant_id=route.provider_grant_id,
                    ).revision
                    if actor_context.get("grantRevision") != grant_revision:
                        raise InputRequestError("the main provider grant revision changed")
                account_id = actor_context.get("accountID")
                root_operation_id = handoff_root_operation_id(owner, host_chat_id, value["requestID"], value["handoffID"])
                managed_route = {
                    "rootOperationID": root_operation_id,
                    "connectionID": route.connection_id,
                    "providerID": route.connection_id,
                    "billingLane": route.billing_lane,
                    "modelID": route.provider_model_id,
                    "modelRouteID": route.model_route_id,
                    **({"grantID": route.provider_grant_id} if route.provider_grant_id else {}),
                    **({"grantRevision": grant_revision} if grant_revision is not None else {}),
                    **({"inheritedAccountID": account_id} if account_id else {}),
                }
                handoff = native_handoff(handoff_value, current_actor, managed_route)
            except ChatRouteUnavailable as exc:
                ensure_current(mapping, owner, host_chat_id, binding)
                return {"receipt": receipt, "delivery": "persisted", "deliveryState": "unavailable",
                        "deliveryReason": "ACTOR_ROUTE_UNAVAILABLE", "actorSessionId": session_id,
                        "source": "managed_engine_receipt"}
            except InputRequestError as exc:
                ensure_current(mapping, owner, host_chat_id, binding)
                return {"receipt": receipt, "delivery": "persisted", "deliveryState": "unavailable",
                        "deliveryReason": str(exc), "actorSessionId": session_id,
                        "source": "managed_engine_receipt"}
            except HTTPException:
                raise
            # Reserve host authority before the native handoff. A transport failure
            # leaves the reservation intact until the same identity is reconciled.
            prepare = getattr(supervisor, "prepare_managed_handoff", None)
            if not callable(prepare):
                raise HTTPException(503, {"code": "ACTOR_RESERVATION_UNAVAILABLE", "message": "Managed handoff authority is unavailable"})
            connector_current()
            await prepare(owner=owner, host_chat_id=host_chat_id, engine_session_id=session_id,
                root_session_id=binding.engine_session_id, root_operation_id=root_operation_id,
                request_id=value["requestID"], handoff_id=value["handoffID"], actor_id=actor_id,
                expected_execution_revision=handoff["expectedExecutionRevision"],
                admission_ceiling=frozen_receipt["effectivePolicyCeiling"], turn_policy=frozen_receipt.get("turnPolicy"),
                input_text=frozen_receipt.get("text", ""), ingress_account_id=ingress_account,
                human_ingress=(ingress_barrier, ingress_admission) if ingress_account and ingress_barrier and ingress_admission
                    and not getattr(request.state, 'api_token', None) and not getattr(request.state, 'internal_tool_authenticated', False) else None)
            connector_current()
            retire_browser_policy_before_native_resume(owner, host_chat_id)
            connector_current()
            claim, _ = await actor_input_request(supervisor, binding, owner, session_id, "POST", handoff_suffix, handoff)
            if isinstance(claim, dict) and claim.get("kind") == "fresh-claim":
                delivery = "started"
            elif isinstance(claim, dict) and claim.get("kind") == "existing-claim":
                delivery = "acknowledged"
            else:
                ensure_current(mapping, owner, host_chat_id, binding)
                if isinstance(claim, dict) and claim.get('kind') == 'unavailable':
                    clear = getattr(supervisor, 'clear_managed_handoff', None)
                    if callable(clear):
                        await clear(owner=owner, host_chat_id=host_chat_id, engine_session_id=session_id,
                            root_operation_id=root_operation_id, handoff_id=value['handoffID'],
                            expected_execution_revision=handoff['expectedExecutionRevision'], confirmed_unissued=True)
                return {"receipt": receipt, "delivery": "persisted", "deliveryState": "unavailable",
                        "deliveryReason": claim.get("reason", "ACTOR_RESUME_UNAVAILABLE") if isinstance(claim, dict) else "ACTOR_RESUME_UNAVAILABLE",
                        "actorSessionId": session_id, "source": "managed_engine_receipt"}
        ensure_current(mapping, owner, host_chat_id, binding)
        return {"receipt": receipt, "delivery": delivery,
                **({"deliveryState": "pending-boundary"} if delivery == "persisted" else {}),
                "actorSessionId": session_id, "source": "managed_engine_receipt"}

    async def workflow_availability(supervisor, binding, owner, native_session_id):
        read_workflows = getattr(supervisor, "workflow_availability_request", None)
        if not callable(read_workflows):
            raise HTTPException(503, {"code": "WORKFLOW_READ_UNAVAILABLE", "message": "Workflow availability reads are unavailable"})
        try:
            response = await read_workflows(binding.host_chat_id, native_session_id, owner=owner)
        except PermissionError as exc:
            raise HTTPException(404, {"code": "CHILD_SESSION_NOT_MAPPED", "message": "Child session is not a verified child of this chat"}) from exc
        except Exception as exc:
            from src.openclank.agent_supervisor import AgentSupervisorAdmissionError
            if isinstance(exc, AgentSupervisorAdmissionError):
                raise HTTPException(exc.status, {"code": getattr(exc, "code", "ENGINE_UNAVAILABLE"), "message": "Managed engine is unavailable"}) from exc
            if isinstance(exc, (RuntimeError, httpx.HTTPError)):
                raise HTTPException(503, {"code": "ENGINE_UNAVAILABLE", "message": "Managed engine is unavailable"}) from exc
            raise
        try:
            body = response.json()
        except ValueError as exc:
            raise HTTPException(502, {"code": "ENGINE_INVALID_RESPONSE", "message": "Workflow endpoint returned invalid JSON"}) from exc
        if response.status_code >= 400:
            raise HTTPException(response.status_code, {"code": "WORKFLOW_READ_FAILED", "message": "Workflow read failed"})
        if (not isinstance(body, dict) or type(body.get("available")) is not bool
                or (body["available"] and not isinstance(body.get("runs"), list))
                or (not body["available"] and body.get("runs") is not None)):
            raise HTTPException(502, {"code": "ENGINE_INVALID_RESPONSE", "message": "Workflow endpoint returned invalid availability data"})
        return body

    @router.get("/{host_chat_id}")
    async def inspect(request: Request, host_chat_id: str, map_revision: int | None = None):
        owner, supervisor, binding, mapping = get_binding(request, host_chat_id, map_revision)
        actors_value, _ = await native_request(supervisor, binding, owner, "actors")
        tasks_value, _ = await native_request(supervisor, binding, owner, "task")
        try:
            actors = validate_actors(actors_value, binding.engine_session_id)
            tasks = validate_tasks(tasks_value, binding.engine_session_id)
        except ProjectionError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc
        children_value, _ = await native_request(supervisor, binding, owner, "children?visible=true")
        if not isinstance(children_value, list) or any(
            not isinstance(child, dict) or not isinstance(child.get("id"), str)
            or child.get("parentID") != binding.engine_session_id for child in children_value
        ):
            raise HTTPException(502, {"code": "ENGINE_INVALID_RESPONSE", "message": "Managed engine returned invalid child-session membership"})
        session_bindings = [{"sessionId": binding.engine_session_id, "parentSessionId": None, "kind": "root"}]
        workflow_sessions = [{"sessionId": binding.engine_session_id, "kind": "root",
                              **(await workflow_availability(supervisor, binding, owner, binding.engine_session_id))}]
        for child in children_value:
            child_id = child["id"]
            session_bindings.append({"sessionId": child_id, "parentSessionId": child["parentID"], "kind": "peer_child"})
            child_actors_value, _ = await child_native_request(supervisor, binding, owner, child_id, "actors")
            child_tasks_value, _ = await child_native_request(supervisor, binding, owner, child_id, "task")
            try:
                child_actors = validate_actors(child_actors_value, child_id)
                child_tasks = validate_tasks(child_tasks_value, child_id)
            except ProjectionError as exc:
                raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc
            actors.extend({**actor, "sessionBinding": {
                "sessionId": child_id, "parentSessionId": child["parentID"], "kind": "peer_child",
            }} for actor in child_actors)
            tasks.extend(child_tasks)
            child_workflows = await workflow_availability(supervisor, binding, owner, child_id)
            workflow_sessions.append({"sessionId": child_id, "kind": "peer_child", **child_workflows})
        actors = [{**sanitize_actor(actor), "sessionBinding": actor.get("sessionBinding") or {
            "sessionId": actor["sessionID"], "parentSessionId": None, "kind": "root",
        }} for actor in actors]
        ensure_current(mapping, owner, host_chat_id, binding)
        root_workflows = workflow_sessions[0]
        result = build_projection(binding, actors, tasks, root_workflows["runs"],
                                  workflow_available=root_workflows["available"])
        result["sessionBindings"] = session_bindings
        result["workflows"]["sessions"] = workflow_sessions
        return result

    @router.post("/{host_chat_id}/context-selection")
    async def context_selection(request: Request, host_chat_id: str):
        owner, supervisor, binding, mapping = get_binding(request, host_chat_id, None)
        auth = getattr(request.app.state, "auth_manager", None)
        privileges = getattr(auth, "get_privileges", None)
        if not callable(privileges):
            raise HTTPException(503, "Current account authority is unavailable")
        if not (privileges(owner) or {}).get("can_use_agent", True):
            raise HTTPException(403, "Agent access is unavailable")
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {"action", "partIDs", "expected"} or body.get("action") not in {"omit", "trim", "restore"}:
            raise HTTPException(400, "Invalid context selection")
        ids = body.get("partIDs")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 16 or any(not isinstance(x, str) or not 0 < len(x) <= 512 for x in ids) or len(set(ids)) != len(ids):
            raise HTTPException(400, "Invalid context selection")
        if "expected" in body:
            expected = body["expected"]
            if (not isinstance(expected, dict) or set(expected) != {"version", "sessionID", "executionRevision", "watermark", "action", "parts"}
                    or expected.get("version") != 1 or type(expected.get("version")) is not int
                    or expected.get("sessionID") != binding.engine_session_id or expected.get("action") != body["action"]
                    or type(expected.get("executionRevision")) is not int or expected["executionRevision"] < 0
                    or not isinstance(expected.get("watermark"), str) or not 0 < len(expected["watermark"]) <= 1024
                    or not isinstance(expected.get("parts"), list) or len(expected["parts"]) != len(ids)):
                raise HTTPException(400, "Invalid context selection preview")
            for part, part_id in zip(expected["parts"], ids):
                if (not isinstance(part, dict) or set(part) != {"id", "messageID", "digest", "type"}
                        or part.get("id") != part_id or not isinstance(part.get("messageID"), str)
                        or not 0 < len(part["messageID"]) <= 512 or part.get("type") not in {"file", "reasoning", "tool"}
                        or not isinstance(part.get("digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", part["digest"])):
                    raise HTTPException(400, "Invalid context selection preview")
        call = getattr(supervisor, "session_context_selection_request", None)
        if not callable(call):
            raise HTTPException(503, "Managed context owner unavailable")
        from src.openclank.transcript_projection import get_managed_binding
        async with mapping.async_chat_admission_lock(host_chat_id):
            await asyncio.to_thread(ensure_current, mapping, owner, host_chat_id, binding)
            try:
                authority = get_managed_binding(host_chat_id, owner=owner)
                if (authority.get("owner") != owner or authority.get("stableChatID") != host_chat_id
                        or authority.get("engineSessionID") != binding.engine_session_id
                        or authority.get("mappingRevision") != binding.binding_revision or authority.get("transition") is not None
                        or not isinstance(authority.get("physicalCwd"), str) or not authority["physicalCwd"]):
                    raise ValueError("binding changed")
            except Exception as exc:
                raise HTTPException(409, "Managed context authority changed") from exc
            try:
                current_privileges = privileges(owner) or {}
            except Exception as exc:
                raise HTTPException(503, "Current account authority is unavailable") from exc
            if not current_privileges.get("can_use_agent", True):
                raise HTTPException(403, "Agent access is unavailable")
            payload = {"stableChatID": host_chat_id, "action": body["action"], "partIDs": ids,
                       **({"expected": body["expected"]} if "expected" in body else {})}
            task = asyncio.create_task(call(host_chat_id, binding.engine_session_id, authority["physicalCwd"], payload, owner=owner))
            cancelled = False
            try:
                while True:
                    try:
                        response = await asyncio.shield(task)
                        break
                    except asyncio.CancelledError:
                        cancelled = True
                        if task.done():
                            response = task.result()
                            break
            except Exception as exc:
                # The authorized fixed-target mutation may have committed; never retry or retarget.
                raise HTTPException(503, {"code": "CONTEXT_OUTCOME_UNKNOWN", "message": "Context operation outcome is unavailable; inspect again"}) from exc
            if cancelled:
                raise asyncio.CancelledError
            await asyncio.to_thread(ensure_current, mapping, owner, host_chat_id, binding)
            if response.status_code != 200:
                raise HTTPException(409 if response.status_code == 409 else 503, "Context selection is unavailable or stale")
            try:
                preview = response.json()
            except ValueError as exc:
                raise HTTPException(502, "Invalid context selection response") from exc
            if not isinstance(preview, dict) or preview.get("sessionID") != binding.engine_session_id or preview.get("action") != body["action"]:
                raise HTTPException(502, "Invalid context selection response")
            return {"preview": preview, "applied": "expected" in body}

    @router.get("/{host_chat_id}/context-observations")
    async def context_observations(request: Request, host_chat_id: str, limit: int = 5, cursor: str | None = None):
        if not 1 <= limit <= 20:
            raise HTTPException(400, "Invalid context page limit")
        owner, supervisor, binding, mapping = get_binding(request, host_chat_id, None)
        filter_key = "context:" + owner
        try:
            after = decode_cursor(cursor, binding, "main", filter_key, binding.engine_session_id)
        except ProjectionError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc
        read_page = getattr(supervisor, "session_observation_request", None)
        if not callable(read_page):
            return {"availability": "unavailable", "reason": "native_observation_unavailable", "items": []}
        try:
            response = await read_page(host_chat_id, binding.engine_session_id, "context", limit, after, owner=owner)
            body = response.json() if response.status_code == 200 else None
        except Exception:
            body = None
        ensure_current(mapping, owner, host_chat_id, binding)
        if not isinstance(body, dict):
            return {"availability": "unavailable", "reason": "native_observation_unavailable", "items": []}
        items = body.get("items")
        if not isinstance(items, list) or len(items) > limit or type(body.get("observedAt")) is not int:
            raise HTTPException(502, "Invalid context observation page")
        projected = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("messageID"), str) or len(item["messageID"]) > 512:
                raise HTTPException(502, "Invalid context observation row")
            receipt = item.get("receipt")
            frames = receipt.get("frames") if isinstance(receipt, dict) and receipt.get("version") == 1 else None
            clean = []
            omitted_frames = receipt.get("omittedFrameCount") if isinstance(receipt, dict) else None
            if isinstance(frames, list) and len(frames) <= 16 and type(omitted_frames) is int and omitted_frames >= 0:
                for frame in frames:
                    if not isinstance(frame, dict) or frame.get("version") != 1 or frame.get("stage") not in ("provider_frame_pre_dispatch", "model_view_dispatch_observed") or frame.get("sourceMapping") != "unknown" or frame.get("tokens") != {"basis": "unavailable"}:
                        clean = []; break
                    model, sources = frame.get("model"), frame.get("sources")
                    if not isinstance(model, dict) or not isinstance(model.get("window"), dict) or not isinstance(sources, list) or len(sources) > 128:
                        clean = []; break
                    window = model["window"]
                    if any(type(window.get(k)) is not int or window[k] < 0 for k in ("hard", "effective", "usable")) or window.get("source") not in ("model", "config"):
                        clean = []; break
                    if any(not isinstance(model.get(k), str) or not 0 < len(model[k]) <= 512 for k in ("providerID", "modelID")):
                        clean = []; break
                    refs = []
                    for ref in sources:
                        if isinstance(ref, dict) and ref.get("kind") == "media_input":
                            # A missing or unsupported association is not a claim
                            # about the rest of this otherwise valid frame.
                            allowed = {"kind", "id", "mediaStage", "providerDelivery", "mediaReason", "digest", "messageID", "partID", "derivedView"}
                            reason = ref.get("mediaReason")
                            stage = ref.get("mediaStage")
                            digest = ref.get("digest")
                            if (set(ref) - allowed or not isinstance(ref.get("id"), str) or not 0 < len(ref["id"]) <= 256
                                    or stage not in ("prepared", "omitted", "native_accepted") or ref.get("providerDelivery") != "unknown"
                                    or reason not in (None, "too_large_bytes", "too_many_pixels", "side_too_long", "unsupported_type", "type_mismatch", "unreadable_header", "route_without_vision")
                                    or (digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)))):
                                continue
                            if stage == "native_accepted":
                                if not digest or any(not isinstance(ref.get(k), str) or not 0 < len(ref[k]) <= 512 for k in ("messageID", "partID")):
                                    continue
                            elif "messageID" in ref or "partID" in ref:
                                continue
                            view = ref.get("derivedView")
                            if view is not None:
                                if (type(view) is not dict or set(view) != {"profile", "originalSHA256", "derivedSHA256", "derivedMime", "width", "height", "derivedLength", "stage", "providerDelivery"}
                                        or stage != "native_accepted" or view.get("profile") != "image-downscale-v1"
                                        or view.get("originalSHA256") != digest or not isinstance(view.get("derivedSHA256"), str)
                                        or not re.fullmatch(r"[0-9a-f]{64}", view["derivedSHA256"]) or view.get("derivedMime") != "image/png"
                                        or view.get("stage") != "model_view_pre_dispatch" or view.get("providerDelivery") != "unknown"
                                        or any(type(view.get(k)) is not int or not 1 <= view[k] <= 2048 for k in ("width", "height"))
                                        or type(view.get("derivedLength")) is not int or not 1 <= view["derivedLength"] <= 256 * 1024):
                                    continue
                            refs.append({"kind":"media_input", "id":ref["id"], "mediaStage":stage, "providerDelivery":"unknown", "mediaReason":reason,
                                **({"digest":digest} if digest else {}), **({k:ref[k] for k in ("messageID","partID")} if stage == "native_accepted" else {}),
                                **({"derivedView":dict(view)} if view is not None else {})})
                            continue
                        if not isinstance(ref, dict) or ref.get("kind") not in ("message", "part", "fork_snapshot", "generated_artifact") or not isinstance(ref.get("id"), str) or not 0 < len(ref["id"]) <= 512:
                            refs = None; break
                        revision, digest = ref.get("revision"), ref.get("digest")
                        if (revision is not None and (not isinstance(revision, str) or not 0 < len(revision) <= 512)) or (digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest))):
                            refs = None; break
                        extra={}
                        if ref['kind']=='generated_artifact':
                            if not revision or not digest or any(not isinstance(ref.get(k),str) or not re.fullmatch(r'[0-9a-f]{64}',ref[k]) for k in ('artifactSHA256','sourceReceiptSHA256')):
                                refs=None; break
                            extra={k:ref[k] for k in ('artifactSHA256','sourceReceiptSHA256')}
                        refs.append({"kind": ref["kind"], "id": ref["id"], **({"revision": revision} if revision else {}), **({"digest": digest} if digest else {}), **extra})
                    if refs is None or type(frame.get("observedAt")) is not int or type(frame.get("omittedSourceCount")) is not int or frame["omittedSourceCount"] < 0:
                        clean = []; break
                    identity, sequence, components = frame.get("identity"), frame.get("sequence"), frame.get("components")
                    if not isinstance(identity, dict) or identity.get("sessionID") != binding.engine_session_id or not isinstance(sequence, dict) or any(type(sequence.get(k)) is not int or sequence[k] < 0 for k in ("attempt", "transform")) or not isinstance(components, dict):
                        clean = []; break
                    safe_identity = {"sessionID": identity["sessionID"]}
                    for key in ("actorID", "rootOperationID", "innerTurnID"):
                        value = identity.get(key)
                        if value is not None:
                            if not isinstance(value, str) or not 0 < len(value) <= 512: safe_identity = None; break
                            safe_identity[key] = value
                    revision = identity.get("executionRevision")
                    if safe_identity is None or (revision is not None and (type(revision) is not int or revision < 0)):
                        clean = []; break
                    if revision is not None: safe_identity["executionRevision"] = revision
                    safe_components = {}
                    for key in ("prompt", "instructions", "tools"):
                        component = components.get(key)
                        if not isinstance(component, dict) or component.get("state") not in ("present", "absent", "unavailable"):
                            safe_components = None; break
                        digest = component.get("digest")
                        if component["state"] == "present" and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                            safe_components = None; break
                        safe_components[key] = {"state": component["state"], **({"digest": digest} if component["state"] == "present" else {})}
                    if safe_components is None: clean = []; break
                    if frame["stage"] == "model_view_dispatch_observed":
                        same = lambda value: {k:v for k,v in value.items() if k not in {"stage","observedAt"}}
                        if (not any(isinstance(item,dict) and item.get("kind")=="media_input" and item.get("derivedView") for item in frame.get("sources",[]))
                                or not any(isinstance(item,dict) and item.get("stage")=="provider_frame_pre_dispatch" and same(item)==same(frame) for item in frames[:frames.index(frame)])):
                            clean = []; break
                    clean.append({"stage": frame["stage"], "observedAt": frame["observedAt"], "identity": safe_identity,
                        "sequence": {k: sequence[k] for k in ("attempt", "transform")}, "components": safe_components,
                        "model": {"providerID": model["providerID"], "modelID": model["modelID"], "window": {k: window[k] for k in ("hard", "effective", "usable", "source")}}, "sources": refs, "omittedSourceCount": frame["omittedSourceCount"], "sourceMapping": "unknown", "tokens": {"basis": "unavailable"}})
            deliveries = receipt.get("nativeDeliveries") if isinstance(receipt, dict) else None
            safe_deliveries = []
            if deliveries is not None:
                if not clean or not isinstance(deliveries, list) or len(deliveries) > 16:
                    clean = []
                else:
                    for delivery in deliveries:
                        identity = delivery.get("identity") if isinstance(delivery, dict) else None
                        source = delivery.get("source") if isinstance(delivery, dict) else None
                        if (not isinstance(delivery, dict) or set(delivery) != {"version", "identity", "profile", "dispatchID", "bodyDigest", "source", "state", "coverage"}
                            or delivery.get("version") != 1 or delivery.get("profile") != "openai-chat-single-user-text-v1"
                            or delivery.get("state") != "acknowledged" or delivery.get("coverage") != "partial"
                            or not isinstance(delivery.get("dispatchID"), str) or not 0 < len(delivery["dispatchID"]) <= 128
                            or not isinstance(delivery.get("bodyDigest"), str) or not re.fullmatch(r"[0-9a-f]{64}", delivery["bodyDigest"])
                            or not isinstance(identity, dict) or set(identity) != {"sessionID", "rootOperationID", "actorID", "executionRevision", "innerTurnID"}
                            or identity.get("sessionID") != binding.engine_session_id or identity.get("actorID") != "main"
                            or any(not isinstance(identity.get(k), str) or not 0 < len(identity[k]) <= 512 for k in ("rootOperationID", "innerTurnID"))
                            or type(identity.get("executionRevision")) is not int or identity["executionRevision"] < 0
                            or not any(frame["identity"] == identity for frame in clean)
                            or not isinstance(source, dict) or set(source) != {"messageID", "partID", "digest", "sourceRevision"}
                            or any(not isinstance(source.get(k), str) or not 0 < len(source[k]) <= 512 for k in ("messageID", "partID"))
                            or source.get("sourceRevision") != "unavailable" or not isinstance(source.get("digest"), str)
                            or not re.fullmatch(r"[0-9a-f]{64}", source["digest"])):
                            clean = []; safe_deliveries = []; break
                        safe_deliveries.append({"version":1,"identity":dict(identity),"profile":delivery["profile"],"dispatchID":delivery["dispatchID"],"bodyDigest":delivery["bodyDigest"],"source":dict(source),"state":"acknowledged","coverage":"partial"})
            memberships = receipt.get('nativeMemberships') if isinstance(receipt, dict) else None
            safe_memberships = []
            if memberships is not None:
                if not clean or not isinstance(memberships, list) or len(memberships) > 16:
                    clean = []
                else:
                    for membership in memberships:
                        identity = membership.get('identity') if isinstance(membership, dict) else None
                        sources = membership.get('sources') if isinstance(membership, dict) else None
                        if (not isinstance(membership, dict) or set(membership) != {'version','identity','profile','dispatchID','bodyDigest','state','coverage','sources'}
                            or type(membership.get('version')) is not int or membership['version'] != 1 or membership.get('profile') != 'openai-chat-native-text-v1'
                            or membership.get('state') != 'acknowledged' or membership.get('coverage') != 'partial'
                            or not isinstance(membership.get('dispatchID'),str) or not 0 < len(membership['dispatchID'].encode('utf-8')) <= 256
                            or not isinstance(membership.get('bodyDigest'),str) or not re.fullmatch(r'[0-9a-f]{64}',membership['bodyDigest'])
                            or not isinstance(identity,dict) or set(identity) != {'sessionID','rootOperationID','actorID','executionRevision','innerTurnID'}
                            or identity.get('sessionID') != binding.engine_session_id or identity.get('actorID') != 'main'
                            or any(not isinstance(identity.get(k),str) or not 0 < len(identity[k].encode('utf-8')) <= 256 for k in ('rootOperationID','innerTurnID'))
                            or type(identity.get('executionRevision')) is not int or identity['executionRevision'] < 0
                            or not any(frame['identity'] == identity for frame in clean)
                            or not isinstance(sources,list) or not 1 <= len(sources) <= 32):
                            clean = []; safe_memberships = []; break
                        seen_ids, seen_positions, last_position = set(), set(), -1
                        for source in sources:
                            if (not isinstance(source,dict) or set(source) != {'messageID','partID','role','digest','sourceRevision','messageIndex','contentIndex'}
                                or any(not isinstance(source.get(k),str) or not 0 < len(source[k].encode('utf-8')) <= 256 for k in ('messageID','partID'))
                                or source.get('role') not in ('user','assistant') or source.get('sourceRevision') != 'unavailable'
                                or not isinstance(source.get('digest'),str) or not re.fullmatch(r'[0-9a-f]{64}',source['digest'])
                                or type(source.get('messageIndex')) is not int or source['messageIndex'] <= last_position or source['messageIndex'] >= 544 or source.get('contentIndex') != 0
                                or type(source.get('contentIndex')) is not int or source['messageID'] in seen_ids or source['partID'] in seen_ids
                                or source['messageID'] == source['partID'] or source['messageIndex'] in seen_positions):
                                clean = []; safe_memberships = []; break
                            seen_ids.update((source['messageID'],source['partID'])); seen_positions.add(source['messageIndex']); last_position = source['messageIndex']
                        if not clean: break
                        safe_memberships.append({**membership,'identity':dict(identity),'sources':[dict(source) for source in sources]})
            projected.append({"messageID": item["messageID"], "availability": "available" if clean else "unavailable", **({"frames": clean, "omittedFrameCount": omitted_frames, **({"nativeDeliveries":safe_deliveries} if deliveries is not None else {}), **({'nativeMemberships':safe_memberships} if memberships is not None else {})} if clean else {})})
        continuation = body.get("nextCursor")
        if continuation is not None and (not isinstance(continuation, str) or not continuation or len(continuation) > 512):
            raise HTTPException(502, "Invalid context continuation")
        return {"availability": "available", "observedAt": body["observedAt"], "items": projected,
                **({"nextCursor": encode_cursor(continuation, binding, "main", filter_key, binding.engine_session_id)} if continuation else {})}

    @router.get("/{host_chat_id}/observations/{source}")
    async def task_observations(request: Request, host_chat_id: str, source: str,
                                map_revision: int | None = None, target_session_id: str | None = None,
                                limit: int = 50, cursor: str | None = None):
        """Observe one live native page; this never admits a worker or reads peers in bulk."""
        if source not in {"native_task", "native_actor"} or not 1 <= limit <= 100:
            raise HTTPException(400, {"code": "INVALID_OBSERVATION_REQUEST", "message": "Invalid source or page limit"})
        if target_session_id is not None and (not target_session_id.strip() or target_session_id != target_session_id.strip() or len(target_session_id) > 256):
            raise HTTPException(400, {"code": "INVALID_OBSERVATION_REQUEST", "message": "Invalid selected session"})
        owner, supervisor, binding, mapping = get_binding(request, host_chat_id, map_revision)
        target = target_session_id or binding.engine_session_id
        sessions = (binding.engine_session_id,) if target == binding.engine_session_id else (binding.engine_session_id, target)
        scope = TaskObservationScope(owner, binding, sessions)
        try:
            after = decode_task_observation_cursor(scope, source, cursor)
        except ProjectionError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc
        read_page = getattr(supervisor, "session_observation_request", None)
        reason = None
        body = None
        if not callable(read_page):
            reason = "native_paging_unavailable"
        else:
            try:
                response = await read_page(binding.host_chat_id, target, "task" if source == "native_task" else "actor", limit, after, owner=owner)
                if response.status_code in {404, 405, 501, 503}:
                    reason = "native_paging_or_peer_unavailable"
                elif response.status_code >= 400:
                    raise HTTPException(502, {"code": "ENGINE_READ_FAILED", "message": "Managed observation read failed"})
                else:
                    body = response.json()
            except HTTPException:
                raise
            except (PermissionError, ValueError) as exc:
                raise HTTPException(404, {"code": "CHILD_SESSION_NOT_MAPPED", "message": "Selected session is not a verified direct peer"}) from exc
            except Exception:
                reason = "native_observation_unavailable"
        ensure_current(mapping, owner, host_chat_id, binding)
        if reason:
            page = {"availability": "unavailable", "reason": reason, "items": []}
        else:
            if (not isinstance(body, dict) or set(body) - {"items", "nextCursor", "observedAt"}
                    or not isinstance(body.get("items"), list) or len(body["items"]) > limit
                    or type(body.get("observedAt")) is not int or body["observedAt"] < 0
                    or (body.get("nextCursor") is not None and (not isinstance(body["nextCursor"], str) or not body["nextCursor"] or len(body["nextCursor"]) > 512))):
                raise HTTPException(502, {"code": "ENGINE_INVALID_RESPONSE", "message": "Managed observation page is invalid"})
            next_cursor = body.get("nextCursor")
            page = {"availability": "partial" if next_cursor else "available", "items": body["items"], "observedAt": body["observedAt"],
                    **({"reason": "more_pages", "nextCursor": next_cursor} if next_cursor else {})}
        try:
            return project_task_observations(scope, {source: page})
        except ProjectionError as exc:
            raise HTTPException(502, {"code": exc.code, "message": str(exc)}) from exc

    @router.get('/{host_chat_id}/task-contributions')
    async def task_contributions(request: Request, host_chat_id: str, workspace_id: str):
        """Bounded qualified workflow-report evidence for the selected chat."""
        from fastapi.responses import JSONResponse
        from src.auth_helpers import is_internal_tool_identity, require_user
        from src.openclank.files_facade import ProviderContext
        from src.openclank.files_principal import files_principal
        from src.openclank.workflow_contributions import qualified_progress
        from src.openclank.workspace_policy_service import resolve_owned_workspace
        from routes.session_routes import _verify_session_owner
        if (not workspace_id or len(workspace_id)>256 or getattr(request.state,'api_token',False)
                or getattr(request.state,'internal_tool_authenticated',False)):
            raise HTTPException(403,'Contribution history unavailable')
        user=require_user(request)
        if is_internal_tool_identity(user) or (effective_user(request) or user)!=user:
            raise HTTPException(403,'Contribution history unavailable')
        state=request.app.state;auth=getattr(state,'auth_manager',None);barrier=getattr(state,'account_request_barrier',None)
        store=getattr(state,'files_policy_repository',None);files_factory=getattr(state,'files_facade_factory',None)
        credential_factory=getattr(state,'capture_original_credential',None)
        try:
            if not auth or not barrier or not store or not callable(files_factory) or not callable(credential_factory):
                raise ValueError('authority unavailable')
            account,owner,admin=files_principal(request,repository=store)
            if owner!=user or auth.username_for_account_id(account)!=owner:
                raise ValueError('authority changed')
            credential=credential_factory(request,owner,account)
            async with barrier.admitted(owner):
                found_owner,supervisor,binding,mapping=get_binding(request,host_chat_id,None)
                if found_owner!=owner or getattr(state,'mimo_supervisor',None) is not supervisor:
                    raise ValueError('authority changed')
                session=await asyncio.to_thread(session_manager.get_session,host_chat_id)
                selected_workspace=getattr(session,'workspace_id',None) if session else None
                if (not session or session.owner!=owner or getattr(session,'incognito',False) or getattr(session,'archived',False)
                        or not selected_workspace):
                    raise ValueError('workspace changed')
                await asyncio.to_thread(_verify_session_owner,request,host_chat_id,session_manager)
                generation=store.generation()
                def workspace():
                    options={'owner_username':owner,'auth_manager':auth,'purpose':'app_folder','observation_only':True}
                    bound=resolve_owned_workspace(store,workspace_id=workspace_id,**options)
                    if bound.workspace.id!=workspace_id or resolve_owned_workspace(store,workspace_id=selected_workspace,**options)!=bound:
                        raise ValueError('workspace changed')
                    return bound
                bound=await asyncio.to_thread(workspace)
                context=ProviderContext(account,owner,generation,admin,workspace_id)
                facade=files_factory(request,context)
                def current_sync():
                    try:
                        ensure_current(mapping,owner,host_chat_id,binding)
                        return (credential() is True and require_user(request)==owner and (effective_user(request) or owner)==owner
                                and state.auth_manager is auth and state.account_request_barrier is barrier
                                and state.files_policy_repository is store and state.files_facade_factory is files_factory
                                and state.capture_original_credential is credential_factory and state.mimo_supervisor is supervisor
                                and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                                and session_manager.get_session(host_chat_id) is session and session.owner==owner
                                and not getattr(session,'incognito',False) and not getattr(session,'archived',False)
                                and getattr(session,'workspace_id',None)==selected_workspace and store.generation()==generation)
                    except Exception:
                        return False
                async def current():
                    return current_sync() and await asyncio.to_thread(workspace)==bound and current_sync()
                result=await qualified_progress(owner=owner,account=account,workspace=workspace_id,chat=host_chat_id,
                    repository=store,context=context,facade=facade,root=bound.path,current=current)
                await asyncio.to_thread(_verify_session_owner,request,host_chat_id,session_manager)
                if not await current():
                    raise ValueError('authority changed')
                return JSONResponse({**result,'workspaceID':workspace_id,'hostChatID':host_chat_id,'mapRevision':binding.map_revision},
                    headers={'Cache-Control':'private, no-store'})
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(409,'Contribution history unavailable or changed') from exc

    @router.get('/{host_chat_id}/companion-observation')
    async def companion_observation(request: Request, host_chat_id: str, activity_cursor: str | None = None):
        """Manually requested, partial current-work observation; no polling or content."""
        from fastapi.responses import JSONResponse
        from routes.prefs_routes import _load_for_user, PREFERENCE_LOCK
        from routes.session_routes import _validated_session_workspace, _verify_session_owner
        from src.openclank.companion_projection import project_companion, validate_achievement_ids
        from urllib.parse import quote
        import time
        from src.auth_helpers import is_internal_tool_identity, require_user
        if (getattr(request.state,'api_token',None) or getattr(request.state,'internal_tool_authenticated',False)
                or is_internal_tool_identity(require_user(request))): raise HTTPException(403,'Interactive human authentication required')
        owner,supervisor,binding,mapping=get_binding(request,host_chat_id,None)
        auth=request.app.state.auth_manager;barrier=request.app.state.account_request_barrier
        account=auth.account_id(owner);session=session_manager.get_session(host_chat_id)
        workspace_id=getattr(session,'workspace_id',None) if session else None
        def opted():
            with PREFERENCE_LOCK: return _load_for_user(owner).get('companion_enabled') is True
        def account_current():
            return (bool(account) and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                and session_manager.get_session(host_chat_id) is session and session is not None and session.owner==owner
                and not getattr(session,'incognito',False) and not getattr(session,'archived',False) and opted())
        if not account_current() or not workspace_id: raise HTTPException(404,'Companion observation unavailable')
        try:
            async with barrier.admitted(owner):
                async with mapping.async_chat_admission_lock(host_chat_id,blocking=False,existing_only=True):
                    workspace=await asyncio.to_thread(_validated_session_workspace,request,owner,workspace_id)
                    pool=supervisor.owner_lifecycle_pool();lease,state,bridge,holder,generation=await pool.acquire_rating_operation(owner=owner)
                    worker=lease.worker;process=getattr(worker,'_proc',None);key=pool._key(owner)
                    try:
                        def current():
                            try:
                                ensure_current(mapping,owner,host_chat_id,binding);pool._assert_owner_lifecycle(key,lease._owner_epoch)
                                return (account_current() and pool._states.get(key) is state and state.active is worker and state.status=='ready'
                                    and state.generation==generation and worker.is_alive() and worker._proc is process and worker._bridge is bridge
                                    and getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)==holder
                                    and getattr(bridge,'_managed_provider_generation',None)==holder
                                    and bridge.mapped_session_id(host_chat_id)==binding.engine_session_id and bridge.mapped_session_workspace(host_chat_id)==workspace.path)
                            except Exception:return False
                        async with asyncio.timeout(5):
                            if not current():raise ValueError('scope changed')
                            pages={}
                            for kind,source in (('native_actor','actor'),('native_task','task')):
                                response=await worker.session_observation_request(host_chat_id,binding.engine_session_id,source,32,None,owner=owner,timeout=5)
                                if not current() or response.status_code!=200 or len(response.content)>1048576:raise ValueError('observation unavailable')
                                page=response.json()
                                if type(page) is not dict or type(page.get('items')) is not list or len(page['items'])>32 or type(page.get('observedAt')) is not int:raise ValueError('observation unavailable')
                                pages[kind]={'availability':'partial','reason':'bounded_page','items':page['items'],'observedAt':page['observedAt']}
                            scope=TaskObservationScope(owner,binding,(binding.engine_session_id,),workspace_id)
                            projected=project_task_observations(scope,pages)
                            questions=[]
                            for actor in projected['sources']['native_actor']['items'][:8]:
                                ref=actor['sourceRef']; actor_id=ref['nativeId']
                                if not current():raise ValueError('scope changed')
                                response=await worker.session_http_request(host_chat_id,'GET',f"actors/{quote(actor_id,safe='')}/questions?limit=8",owner=owner,timeout=5)
                                if not current() or response.status_code!=200 or len(response.content)>65536:raise ValueError('question page unavailable')
                                page=response.json()
                                if type(page) is not dict or type(page.get('items')) is not list or len(page['items'])>8:raise ValueError('question page unavailable')
                                for item in page['items']:
                                    if type(item) is not dict or item.get('sessionID')!=binding.engine_session_id or item.get('actorID')!=actor_id:raise ValueError('question scope changed')
                                    questions.append({'sessionID':binding.engine_session_id,'actorID':actor_id,'requestID':item.get('requestID'),'questionRef':item.get('questionRef'),'questionRevision':item.get('questionRevision'),'originTurnID':item.get('originTurnID'),'state':item.get('state'),'observedAt':int(time.time()*1000)})
                            await asyncio.to_thread(_verify_session_owner,request,host_chat_id,session_manager)
                            latest=await asyncio.to_thread(_validated_session_workspace,request,owner,workspace_id)
                            if not current() or latest.path!=workspace.path:raise ValueError('scope changed')
                            repository=getattr(request.app.state,'treehouse_repository',None)
                            achievements=None
                            if repository is not None:
                                try:
                                    if not current(): raise ValueError('scope changed')
                                    achievements=validate_achievement_ids(await asyncio.to_thread(repository.bounded_earned_achievement_ids,account))
                                except Exception:
                                    achievements=None
                                if not current(): raise ValueError('scope changed')
                                if getattr(request.app.state,'treehouse_repository',None) is not repository: achievements=None
                            if not current(): raise ValueError('scope changed')
                            result=project_companion(scope,projected,connection='connected',questions=questions,achievement_ids=achievements)
                            # Archive owns the ordered, bounded body-free activity; preserve the live binding fence.
                            from services.logging.projection import clankshop_logging_projection
                            try:
                                activity=await asyncio.to_thread(clankshop_logging_projection().clankshop_activity,
                                    account,host_chat_id,workspace_id,binding.engine_session_id,binding.map_revision,
                                    binding.binding_revision,holder,generation,activity_cursor,deadline=time.monotonic()+1)
                            except Exception:
                                activity={'state':'unavailable','reason':'archive_read_unavailable','items':[],'cursor':None}
                            if not current(): raise ValueError('scope changed')
                            result['activity']=activity
                            from services.logging.projection import clankshop_usage
                            try:
                                actor_runs=tuple((a['sourceRef']['sessionId'],a['sourceRef']['nativeId'],a['sourceRef']['executionRevision']) for a in projected['sources']['native_actor']['items'])
                                result['usage']=await asyncio.to_thread(clankshop_usage,owner,account,host_chat_id,workspace_id,binding.engine_session_id,holder,actor_runs)
                            except Exception:
                                result['usage']={'state':'unavailable','reason':'stats_read_unavailable','items':[]}
                            if not current(): raise ValueError('scope changed')
                            return JSONResponse(result,headers={'Cache-Control':'private, no-store'})
                    finally: await lease.release()
        except HTTPException:raise
        except Exception as exc:raise HTTPException(503,'Companion observation unavailable') from exc

    @router.get('/{host_chat_id}/peer-observation')
    async def observe_direct_peers(request: Request, host_chat_id: str, cursor: str | None = None):
        from fastapi.responses import JSONResponse
        from src.auth_helpers import require_user
        from routes.session_routes import _validated_session_workspace, _verify_session_owner
        if getattr(request.state,'api_token',None) or getattr(request.state,'internal_tool_authenticated',False):
            raise HTTPException(403,'Interactive human authentication required')
        owner=require_user(request);state=request.app.state;auth=getattr(state,'auth_manager',None)
        barrier=getattr(state,'account_request_barrier',None);account=auth.account_id(owner) if auth else None
        if not account or not barrier or auth.username_for_account_id(account)!=owner:raise HTTPException(503,'Peer observation unavailable')
        current_owner,supervisor,binding,mapping=get_binding(request,host_chat_id,None)
        session=session_manager.get_session(host_chat_id);workspace_id=getattr(session,'workspace_id',None) if session else None
        if (current_owner!=owner or not session or session.owner!=owner or not workspace_id or getattr(session,'incognito',False)
            or getattr(session,'archived',False)):raise HTTPException(503,'Peer observation unavailable')
        workspace=await asyncio.to_thread(_validated_session_workspace,request,owner,workspace_id)
        try:
            async with barrier.admitted(owner):
                async with mapping.async_chat_admission_lock(host_chat_id,blocking=False,existing_only=True):
                    def current():
                        return (auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                            and session_manager.get_session(host_chat_id) is session and session.owner==owner
                            and getattr(session,'workspace_id',None)==workspace_id and not getattr(session,'incognito',False)
                            and not getattr(session,'archived',False))
                    if not current():raise ValueError('peer scope changed')
                    ensure_current(mapping,owner,host_chat_id,binding)
                    pool=supervisor.owner_lifecycle_pool()
                    async def finish():
                        await asyncio.to_thread(_verify_session_owner,request,host_chat_id,session_manager)
                        after=await asyncio.to_thread(_validated_session_workspace,request,owner,workspace_id)
                        ensure_current(mapping,owner,host_chat_id,binding)
                        if not current() or after!=workspace or supervisor.owner_lifecycle_pool() is not pool:raise ValueError('peer scope changed')
                    result=await pool.session_direct_peer_observation(host_chat_id,binding.engine_session_id,workspace.path,
                        owner=owner,account=account,workspace_id=workspace_id,mapping_revision=binding.binding_revision,finish=finish,cursor=cursor)
                    page=result['page'];peers=[]
                    for peer in page['items']:
                        child=peer['sessionId'];raw=peer['actors'];next_cursor=raw.get('nextCursor')
                        source={'availability':'partial' if next_cursor else 'available','items':raw['items'],'observedAt':raw['observedAt'],
                            **({'reason':'more_pages','nextCursor':next_cursor} if next_cursor else {})}
                        projection=project_task_observations(
                            TaskObservationScope(owner,binding,(binding.engine_session_id,child),workspace_id),
                            {'native_actor':source}, verified_direct_peer=(child,peer['parentSessionId']))
                        actors=projection['sources']['native_actor']
                        peers.append({'sessionId':child,'parentSessionId':binding.engine_session_id,'actors':actors})
                    body={'version':1,'scope':{'hostChatId':host_chat_id,'rootSessionId':binding.engine_session_id,
                        'mappingRevision':binding.binding_revision,'mapRevision':binding.map_revision,'workerGeneration':result['generation']},
                        'directPeers':{'availability':'partial','observedAt':page['observedAt'],'items':peers,
                            **({'nextCursor':page['nextCursor']} if page.get('nextCursor') else {})}}
                    return JSONResponse(body,headers={'Cache-Control':'private, no-store'})
        except HTTPException:raise
        except Exception:raise HTTPException(503,'Peer observation unavailable')

    @router.get('/{host_chat_id}/workflow-observation')
    async def observe_workflows(request: Request, host_chat_id: str, cursor: str | None = None,
                                run_id: str | None = None, map_revision: int | None = None):
        from fastapi.responses import JSONResponse
        from src.auth_helpers import require_user
        from routes.session_routes import _validated_session_workspace, _verify_session_owner
        if getattr(request.state,'api_token',None) or getattr(request.state,'internal_tool_authenticated',False):
            raise HTTPException(403,'Interactive human authentication required')
        owner=require_user(request);state=request.app.state;auth=getattr(state,'auth_manager',None)
        barrier=getattr(state,'account_request_barrier',None);account=auth.account_id(owner) if auth else None
        if not account or not barrier or auth.username_for_account_id(account)!=owner:raise HTTPException(503,'Workflow observation unavailable')
        if run_id is not None and (cursor is not None or map_revision is None or not re.fullmatch(r'wf_(?:[0-9A-Za-z-]{26}|[0-9a-f]{64})',run_id)):
            raise HTTPException(400,'Invalid workflow selection')
        current_owner,supervisor,binding,mapping=get_binding(request,host_chat_id,map_revision)
        session=session_manager.get_session(host_chat_id);workspace_id=getattr(session,'workspace_id',None) if session else None
        if (current_owner!=owner or not session or session.owner!=owner or not workspace_id or getattr(session,'incognito',False)
            or getattr(session,'archived',False)):raise HTTPException(503,'Workflow observation unavailable')
        workspace=await asyncio.to_thread(_validated_session_workspace,request,owner,workspace_id)
        try:
            async with barrier.admitted(owner):
                async with mapping.async_chat_admission_lock(host_chat_id,blocking=False,existing_only=True):
                    def current():
                        return (auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                            and session_manager.get_session(host_chat_id) is session and session.owner==owner
                            and getattr(session,'workspace_id',None)==workspace_id and not getattr(session,'incognito',False)
                            and not getattr(session,'archived',False))
                    if not current():raise ValueError('workflow scope changed')
                    ensure_current(mapping,owner,host_chat_id,binding)
                    pool=supervisor.owner_lifecycle_pool()
                    async def finish():
                        await asyncio.to_thread(_verify_session_owner,request,host_chat_id,session_manager)
                        after=await asyncio.to_thread(_validated_session_workspace,request,owner,workspace_id)
                        ensure_current(mapping,owner,host_chat_id,binding)
                        if not current() or after!=workspace or supervisor.owner_lifecycle_pool() is not pool:raise ValueError('workflow scope changed')
                    selected_run={'run_id':run_id} if run_id is not None else {}
                    result=await pool.session_workflow_observation(host_chat_id,binding.engine_session_id,workspace.path,
                        owner=owner,account=account,workspace_id=workspace_id,mapping_revision=binding.binding_revision,finish=finish,cursor=cursor,**selected_run)
                    page=result['page']
                    public_scope={'hostChatId':host_chat_id,'rootSessionId':binding.engine_session_id,
                        'mappingRevision':binding.binding_revision,'mapRevision':binding.map_revision,'workerGeneration':result['generation']}
                    if run_id is not None:
                        if page['workspaceWitness']!={'accountID':account,'workspaceID':workspace_id}:
                            raise HTTPException(404,'Workflow structure unavailable')
                        structure={key:page[key] for key in ('runId','sessionId','status','observedAt','coverage','nodes')}
                        return JSONResponse({'version':1,'scope':public_scope,'structure':structure},headers={'Cache-Control':'private, no-store'})
                    rows=[]
                    for row in page['items']:
                        witness=row['workspaceWitness']
                        if witness is not None and witness['accountID']!=account:
                            continue
                        matched=witness is not None and witness['workspaceID']==workspace_id
                        rows.append({'sourceRef':{'kind':'workflow','sessionId':binding.engine_session_id,'nativeId':row['id']},
                            'title':'Workflow','nativeStatus':row['status'],'displayCategory':'execution',
                            'workspaceAvailability':'available' if matched else 'unknown',
                            **({'workspaceId':workspace_id} if matched else {}),
                            'counts':{'running':row['running'],'succeeded':row['succeeded'],'failed':row['failed']},
                            'createdAt':row['created'],'updatedAt':row['updated']})
                    body={'version':1,'scope':public_scope,
                        'workflows':{'availability':'partial','reason':'session_workflows','observedAt':page['observedAt'],'items':rows,
                            **({'nextCursor':page['nextCursor']} if page.get('nextCursor') else {})}}
                    return JSONResponse(body,headers={'Cache-Control':'private, no-store'})
        except HTTPException:raise
        except Exception:raise HTTPException(503,'Workflow observation unavailable')

    @router.get("/{host_chat_id}/goal-observation")
    async def observe_goal(request: Request, host_chat_id: str):
        from fastapi.responses import JSONResponse
        from src.auth_helpers import require_user
        from routes.session_routes import _validated_session_workspace, _verify_session_owner
        if getattr(request.state, 'api_token', None) or getattr(request.state, 'internal_tool_authenticated', False):
            raise HTTPException(403, 'Interactive human authentication required')
        owner = require_user(request)
        auth = getattr(request.app.state, 'auth_manager', None)
        barrier = getattr(request.app.state, 'account_request_barrier', None)
        account = auth.account_id(owner) if auth else None
        if not account or not barrier or auth.username_for_account_id(account) != owner:
            raise HTTPException(503, 'Goal observation unavailable')
        current_owner, supervisor, binding, mapping = get_binding(request, host_chat_id, None)
        session = session_manager.get_session(host_chat_id)
        workspace_id = getattr(session, 'workspace_id', None) if session else None
        if current_owner != owner or not session or session.owner != owner or not workspace_id or getattr(session, 'incognito', False) or getattr(session, 'archived', False):
            raise HTTPException(503, 'Goal observation unavailable')
        workspace = await asyncio.to_thread(_validated_session_workspace, request, owner, workspace_id)
        async with barrier.admitted(owner):
            async with mapping.async_chat_admission_lock(host_chat_id, blocking=False, existing_only=True):
                def current():
                    return (auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                            and session_manager.get_session(host_chat_id) is session and session.owner == owner
                            and getattr(session, 'workspace_id', None) == workspace_id and not getattr(session, 'incognito', False)
                            and not getattr(session, 'archived', False))
                if not current(): raise HTTPException(409, 'Goal observation changed')
                ensure_current(mapping, owner, host_chat_id, binding)
                pool=supervisor.owner_lifecycle_pool()
                async def finish():
                    await asyncio.to_thread(_verify_session_owner, request, host_chat_id, session_manager)
                    after=await asyncio.to_thread(_validated_session_workspace, request, owner, workspace_id)
                    ensure_current(mapping, owner, host_chat_id, binding)
                    if not current() or after!=workspace or supervisor.owner_lifecycle_pool() is not pool:raise HTTPException(409,'Goal observation changed')
                result = await pool.session_goal_observation(host_chat_id, binding.engine_session_id, workspace.path, owner=owner,finish=finish)
                if result.status_code != 200 or len(result.content) > 65536: raise HTTPException(503, 'Goal observation unavailable')
                value = result.json()
                if (not isinstance(value, dict) or set(value) != {'state','stateRevision','active','queue'}
                        or value['state'] not in {'available','partial'} or type(value['stateRevision']) is not int
                        or not 0 <= value['stateRevision'] <= 9007199254740991 or not isinstance(value['queue'], list)
                        or len(value['queue']) > 32): raise HTTPException(502, 'Invalid goal observation')
                statuses = {'queued','active','awaiting_verification','paused','blocked','verification_degraded','completed','cancelled','expired'}
                seen = set()
                for item in ([value['active']] if value['active'] is not None else []) + value['queue']:
                    if (not isinstance(item, dict) or set(item) != {'id','revision','status'} or not isinstance(item['id'], str)
                            or not item['id'] or len(item['id'].encode('utf-8')) > 256 or item['id'] in seen
                            or type(item['revision']) is not int or not 1 <= item['revision'] <= 9007199254740991
                            or item['status'] not in statuses): raise HTTPException(502, 'Invalid goal observation')
                    seen.add(item['id'])
                import time
                return JSONResponse({**value,'observedAt':int(time.time()*1000)}, headers={'Cache-Control':'private, no-store'})

    @router.get("/{host_chat_id}/actors/{actor_id}/inspection")
    async def inspect_actor(request: Request, host_chat_id: str, actor_id: str, map_revision: int,
                            execution_revision: int, actor_session_id: str, message_cursor: str | None = None,
                            receipt_cursor: str | None = None):
        from fastapi.responses import JSONResponse
        from src.auth_helpers import require_user
        from routes.session_routes import _validated_session_workspace, _verify_session_owner
        if getattr(request.state, 'api_token', None) or getattr(request.state, 'internal_tool_authenticated', False):
            raise HTTPException(403, 'Interactive human authentication required')
        owner = require_user(request)
        auth = getattr(request.app.state, 'auth_manager', None)
        barrier = getattr(request.app.state, 'account_request_barrier', None)
        account = auth.account_id(owner) if auth else None
        if (not account or not barrier or auth.username_for_account_id(account) != owner or
                not actor_id or len(actor_id) > 200 or not actor_session_id or len(actor_session_id) > 256 or
                execution_revision < 0 or execution_revision > 9007199254740991):
            raise HTTPException(503, 'Inspection unavailable')
        current_owner, supervisor, binding, mapping = get_binding(request, host_chat_id, map_revision)
        if any(v is not None and (not isinstance(v, str) or len(v) > 2048) for v in (message_cursor, receipt_cursor)):
            raise HTTPException(400, 'Invalid inspection continuation')
        try:
            message_before = decode_cursor(message_cursor, binding, actor_id, f'inspect-message:{execution_revision}', actor_session_id)
            receipt_after = decode_cursor(receipt_cursor, binding, actor_id, f'inspect-receipt:{execution_revision}', actor_session_id)
        except ProjectionError as exc:
            raise HTTPException(exc.status, {'code': exc.code, 'message': str(exc)}) from exc
        session = session_manager.get_session(host_chat_id)
        workspace_id = getattr(session, 'workspace_id', None) if session else None
        if current_owner != owner or not session or session.owner != owner or not workspace_id or getattr(session, 'incognito', False) or getattr(session, 'archived', False):
            raise HTTPException(503, 'Inspection unavailable')
        workspace = await asyncio.to_thread(_validated_session_workspace, request, owner, workspace_id)
        async with barrier.admitted(owner):
            async with mapping.async_chat_admission_lock(host_chat_id, blocking=False, existing_only=True):
                def current():
                    return (auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                            and session_manager.get_session(host_chat_id) is session and session.owner == owner
                            and getattr(session, 'workspace_id', None) == workspace_id and not getattr(session, 'incognito', False)
                            and not getattr(session, 'archived', False))
                if not current(): raise HTTPException(409, 'Inspection changed')
                ensure_current(mapping, owner, host_chat_id, binding)
                result = await supervisor.session_actor_inspection(host_chat_id, actor_session_id, actor_id, execution_revision,
                    owner=owner, message_before=message_before, receipt_after=receipt_after)
                await asyncio.to_thread(_verify_session_owner, request, host_chat_id, session_manager)
                new_workspace = await asyncio.to_thread(_validated_session_workspace, request, owner, workspace_id)
                ensure_current(mapping, owner, host_chat_id, binding)
                if not current() or new_workspace.path != workspace.path: raise HTTPException(409, 'Inspection changed')
                if result.status_code != 200 or len(result.content) > 48000: raise HTTPException(503, 'Inspection unavailable')
                value = result.json()
                if (not isinstance(value, dict) or value.get('actorID') != actor_id or value.get('sessionID') != actor_session_id
                        or value.get('executionRevision') != execution_revision or value.get('coverage') != 'partial' or type(value.get('transcriptVersion')) is not int or value.get('transcriptVersion') != 1
                        or value.get('status') not in {'pending', 'running', 'idle'}
                        or not isinstance(value.get('messages'), list) or len(value['messages']) > 20
                        or not isinstance(value.get('receipts'), dict) or not isinstance(value['receipts'].get('items'), list)
                        or len(value['receipts']['items']) > 20): raise HTTPException(502, 'Invalid inspection')
                messages = []
                for item in value['messages']:
                    if (not isinstance(item, dict) or set(item) != {'id','messageID','role','time','kind','text','tool','state','omission'}
                            or any(not isinstance(item[k], str) or not item[k] or len(item[k].encode('utf-8')) > 256 for k in ('id','messageID'))
                            or item['role'] not in {'user','assistant','unknown'}
                            or (item['time'] is not None and (type(item['time']) is not int or not 0 <= item['time'] <= 9007199254740991))
                            or item['kind'] not in {'text','tool','omitted'}
                            or (item['text'] is not None and (not isinstance(item['text'], str) or len(item['text'].encode('utf-8')) > 2048))
                            or (item['tool'] is not None and (not isinstance(item['tool'], str) or not 1 <= len(item['tool'].encode('utf-8')) <= 128
                                or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-' for c in item['tool'])))
                            or item['state'] not in {None,'pending','running','completed','error'}
                            or item['omission'] not in {None,'hidden','attachment','oversize','unsupported'}
                            or (item['kind'] == 'text' and (item['text'] is None or item['tool'] is not None or item['state'] is not None or item['omission'] is not None))
                            or (item['kind'] == 'tool' and (item['text'] is not None or item['omission'] is not None))
                            or (item['kind'] == 'omitted' and (item['text'] is not None or item['tool'] is not None or item['state'] is not None or item['omission'] is None))):
                        raise HTTPException(502, 'Invalid inspection')
                    messages.append(item)
                receipts = []
                for item in value['receipts']['items']:
                    if (not isinstance(item, dict) or set(item) != {'requestID', 'state', 'mode', 'executionRevision', 'claimedExecutionRevision'}
                            or not isinstance(item['requestID'], str) or len(item['requestID'].encode('utf-8')) > 256
                            or item['state'] not in {'persisted', 'pending-boundary', 'consumed', 'cancelled', 'failed'}
                            or item['mode'] not in {'steer', 'after-turn'} or type(item['executionRevision']) is not int
                            or (item['claimedExecutionRevision'] is not None and type(item['claimedExecutionRevision']) is not int)):
                        raise HTTPException(502, 'Invalid inspection')
                    receipts.append(item)
                communication = value.get('communication', {'coverage': 'unavailable', 'items': []})
                if (not isinstance(communication, dict) or set(communication) != {'coverage', 'items'}
                        or communication['coverage'] not in {'partial', 'unavailable'} or not isinstance(communication['items'], list)
                        or len(communication['items']) > 12 or (communication['coverage'] == 'unavailable' and communication['items'])):
                    raise HTTPException(502, 'Invalid communication observation')
                def valid_receipt_id(identifier):
                    return (isinstance(identifier, str) and 1 <= len(identifier) <= 120
                            and all(ch in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-' for ch in identifier))
                for item in communication['items']:
                    if (not isinstance(item, dict) or set(item) != {'id', 'senderSessionID', 'senderActorID', 'inputMessageID',
                            'deliveryMessageID', 'time', 'state', 'kind'} or not valid_receipt_id(item['id'])
                            or not valid_receipt_id(item['senderActorID']) or not valid_receipt_id(item['senderSessionID'])
                            or item['senderSessionID'] not in {binding.engine_session_id, actor_session_id}
                            or type(item['time']) is not int or not 0 <= item['time'] <= 9007199254740991
                            or item['state'] not in {'accepted', 'claimed', 'settled', 'cancelled', 'rejected'}
                            or item['kind'] not in {'message', 'notification'}
                            or any(item[key] is not None and not valid_receipt_id(item[key])
                                   for key in ('inputMessageID', 'deliveryMessageID'))):
                        raise HTTPException(502, 'Invalid communication observation')
                message_next = value.get('messageNext')
                receipt_next = value['receipts'].get('next')
                if any(v is not None and (not isinstance(v, str) or not v or len(v.encode('utf-8')) > 256)
                       for v in (message_next, receipt_next)):
                    raise HTTPException(502, 'Invalid inspection continuation')
                response = JSONResponse({'actorID': actor_id, 'sessionID': actor_session_id, 'executionRevision': execution_revision,
                    'status': value.get('status'), 'transcriptVersion': 1, 'messages': messages, 'receipts': {'items': receipts}, 'coverage': 'partial',
                    'communication': communication,
                    'messageCursor': encode_cursor(message_next, binding, actor_id, f'inspect-message:{execution_revision}', actor_session_id),
                    'receiptCursor': encode_cursor(receipt_next, binding, actor_id, f'inspect-receipt:{execution_revision}', actor_session_id)},
                    headers={'Cache-Control': 'private, no-store'})
                if len(response.body) > 48000: raise HTTPException(502, 'Inspection exceeds response budget')
                return response

    @router.get("/{host_chat_id}/actors/{actor_id}/messages")
    async def actor_messages(request: Request, host_chat_id: str, actor_id: str,
                             map_revision: int, limit: int = 100, before: str | None = None,
                             tool_filter: str = "all", actor_session_id: str | None = None):
        if limit < 1 or limit > 1000:
            raise HTTPException(400, "limit must be between 1 and 1000")
        if tool_filter not in {"all", "tools", "no_tools"}:
            raise HTTPException(400, "tool_filter is invalid")
        owner, supervisor, binding, mapping = get_binding(request, host_chat_id, map_revision)
        session_id = actor_session_id or binding.engine_session_id
        if session_id == binding.engine_session_id:
            actors_value, _ = await native_request(supervisor, binding, owner, "actors")
        else:
            children_value, _ = await native_request(supervisor, binding, owner, "children?visible=true")
            if not isinstance(children_value, list) or not any(
                isinstance(child, dict) and child.get("id") == session_id
                and child.get("parentID") == binding.engine_session_id for child in children_value
            ):
                raise HTTPException(404, {"code": "CHILD_SESSION_NOT_MAPPED", "message": "Child session is not a verified child of this chat"})
            actors_value, _ = await child_native_request(supervisor, binding, owner, session_id, "actors")
        try:
            actors = validate_actors(actors_value, session_id)
        except ProjectionError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc
        actor = find_session_actor(actors, actor_id, session_id)
        if actor is None:
            raise HTTPException(404, {"code": "ACTOR_NOT_FOUND", "message": "Actor is not registered in this session"})
        try:
            native_before = decode_cursor(before, binding, actor_id, tool_filter, session_id)
        except ProjectionError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc
        query = f"message?limit={limit}&agent_id={quote(actor_id, safe='')}"
        if native_before:
            query += "&before=" + quote(native_before, safe="")
        if session_id == binding.engine_session_id:
            messages, response = await native_request(supervisor, binding, owner, query)
        else:
            messages, response = await child_native_request(supervisor, binding, owner, session_id, query)
        if not isinstance(messages, list):
            raise HTTPException(502, {"code": "ENGINE_INVALID_RESPONSE", "message": "Managed engine returned an invalid message page"})
        cursor = response.headers.get("X-Next-Cursor")
        if tool_filter == "tools":
            messages = [m for m in messages if isinstance(m, dict) and _has_tool(m)]
        elif tool_filter == "no_tools":
            messages = [m for m in messages if isinstance(m, dict) and not _has_tool(m)]
        ensure_current(mapping, owner, host_chat_id, binding)
        return {"hostChatId": host_chat_id, "engineSessionId": binding.engine_session_id,
                "mapRevision": binding.map_revision, "actorId": actor_id, "actorSessionId": session_id,
                "messages": messages, "nextCursor": encode_cursor(cursor, binding, actor_id, tool_filter, session_id),
                "source": "managed_engine"}

    # Private app assembly handoff: the same actor-scoped owners also serve
    # freshly authenticated, scope-limited TUI requests.
    async def tui_shell_view(request, host_chat_id, job_id, actor_id, actor_session_id, execution_revision, map_revision, cursor, limit, current):
        """Read one exact background producer under the current actor and account."""
        from src import bg_jobs
        from routes.session_routes import _validated_session_workspace
        if (not callable(current) or current() is not True or not all(type(v) is str and 0 < len(v) <= 256 for v in (job_id, actor_id, actor_session_id))
                or type(execution_revision) is not int or execution_revision < 0
                or type(map_revision) is not int or map_revision < 0
                or type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 65536):
            raise HTTPException(400, 'Background shell selection unavailable')
        owner, supervisor, binding, mapping = get_binding(request, host_chat_id, map_revision)
        auth = getattr(request.app.state, 'auth_manager', None)
        account = auth.account_id(owner) if auth else None
        session = session_manager.get_session(host_chat_id)
        workspace_id = getattr(session, 'workspace_id', None) if session else None
        if (not account or auth.username_for_account_id(account) != owner or not session or session.owner != owner
                or not workspace_id or getattr(session, 'incognito', False) or getattr(session, 'archived', False)):
            raise HTTPException(503, 'Background shell unavailable')
        workspace = await asyncio.to_thread(_validated_session_workspace, request, owner, workspace_id)
        pool = supervisor.owner_lifecycle_pool()
        worker = pool.worker_for_owner(owner)
        generation = getattr(worker, 'installed_generation', None)
        holder = getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None)
        if not worker or not worker.is_alive() or type(generation) is not int or not isinstance(holder, str) or not holder:
            raise HTTPException(503, 'Background shell runtime unavailable')
        scope = dict(account_id=account, owner=owner, session_id=host_chat_id, workspace_id=workspace_id, workspace=workspace.path)
        async def check():
            if (current() is not True or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                    or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker or not worker.is_alive()
                    or getattr(worker, 'installed_generation', None) != generation
                    or getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) != holder
                    or session_manager.get_session(host_chat_id) is not session or session.owner != owner
                    or getattr(session, 'workspace_id', None) != workspace_id or getattr(session, 'incognito', False)
                    or getattr(session, 'archived', False)):
                raise HTTPException(409, 'Background shell scope changed')
            ensure_current(mapping, owner, host_chat_id, binding)
            now = await asyncio.to_thread(_validated_session_workspace, request, owner, workspace_id)
            if now.path != workspace.path: raise HTTPException(409, 'Background shell workspace changed')
            response = await supervisor.session_actor_inspection(host_chat_id, actor_session_id, actor_id, execution_revision, owner=owner)
            if response.status_code != 200 or len(response.content) > 48000: raise HTTPException(503, 'Actor unavailable')
            actor = response.json()
            if (type(actor) is not dict or actor.get('actorID') != actor_id or actor.get('sessionID') != actor_session_id
                    or actor.get('executionRevision') != execution_revision): raise HTTPException(409, 'Actor changed')
            ensure_current(mapping, owner, host_chat_id, binding)
            after_workspace = await asyncio.to_thread(_validated_session_workspace, request, owner, workspace_id)
            if after_workspace.path != workspace.path: raise HTTPException(409, 'Background shell workspace changed')
            if (current() is not True or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker
                    or not worker.is_alive() or getattr(worker, 'installed_generation', None) != generation
                    or getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) != holder
                    or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                    or session_manager.get_session(host_chat_id) is not session or session.owner != owner
                    or getattr(session, 'workspace_id', None) != workspace_id or getattr(session, 'incognito', False)
                    or getattr(session, 'archived', False)):
                raise HTTPException(409, 'Background shell scope changed')
        try:
            async with mapping.async_chat_admission_lock(host_chat_id, blocking=False, existing_only=True) as guard:
                if guard is None: raise HTTPException(409, 'Background shell source guard unavailable')
                await check()
                before = await asyncio.to_thread(bg_jobs.bound_shell_origin, job_id, **scope)
                origin = before['origin']
                if (origin['actor_id'] != actor_id or origin['engine_session_id'] != actor_session_id
                        or origin['execution_revision'] != execution_revision or origin['generation'] != generation or origin['holder'] != holder):
                    raise HTTPException(409, 'Background shell origin changed')
                frame = await asyncio.to_thread(bg_jobs.bound_shell_tail, job_id, **scope, cursor=cursor, limit=limit)
                after = await asyncio.to_thread(bg_jobs.bound_shell_origin, job_id, **scope)
                await check()
                if after['origin'] != origin: raise HTTPException(409, 'Background shell origin changed')
                frame['status'] = after['status']
                return {'jobID': job_id, 'origin': {k:v for k,v in origin.items() if k not in ('holder','generation')},
                        'status': after['status'], 'settledExit': after['settledExit'], 'frame': frame,
                        'control': 'unavailable', 'controlReason': 'Human process control is not available',
                        'foreground': 'unavailable'}
        except HTTPException: raise
        except Exception as exc: raise HTTPException(503, 'Background shell unavailable') from exc

    async def tui_shell_discover(request, host_chat_id, current):
        """Discover bounded v2 jobs only after checking their real current actors."""
        from src import bg_jobs
        from routes.session_routes import _validated_session_workspace
        if not callable(current) or current() is not True: raise HTTPException(403, 'TUI credential changed')
        owner, supervisor, binding, mapping = get_binding(request, host_chat_id, None)
        auth = getattr(request.app.state, 'auth_manager', None)
        account = auth.account_id(owner) if auth else None
        session = session_manager.get_session(host_chat_id)
        workspace_id = getattr(session, 'workspace_id', None) if session else None
        if (not account or auth.username_for_account_id(account) != owner or not session or session.owner != owner
                or not workspace_id or getattr(session, 'incognito', False) or getattr(session, 'archived', False)):
            raise HTTPException(503, 'Background shell unavailable')
        workspace = await asyncio.to_thread(_validated_session_workspace, request, owner, workspace_id)
        pool = supervisor.owner_lifecycle_pool(); worker = pool.worker_for_owner(owner)
        generation = getattr(worker, 'installed_generation', None)
        holder = getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None)
        if not worker or not worker.is_alive() or type(generation) is not int or not isinstance(holder, str) or not holder:
            raise HTTPException(503, 'Background shell runtime unavailable')
        scope = dict(account_id=account, owner=owner, session_id=host_chat_id, workspace_id=workspace_id, workspace=workspace.path)
        async def check():
            ensure_current(mapping, owner, host_chat_id, binding)
            fresh = await asyncio.to_thread(_validated_session_workspace, request, owner, workspace_id)
            if (current() is not True or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                    or supervisor.owner_lifecycle_pool() is not pool or pool.worker_for_owner(owner) is not worker
                    or not worker.is_alive() or getattr(worker, 'installed_generation', None) != generation
                    or getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) != holder
                    or session_manager.get_session(host_chat_id) is not session or session.owner != owner
                    or getattr(session, 'workspace_id', None) != workspace_id or getattr(session, 'incognito', False)
                    or getattr(session, 'archived', False) or fresh.path != workspace.path):
                raise HTTPException(409, 'Background shell scope changed')
        try:
            async with mapping.async_chat_admission_lock(host_chat_id, blocking=False, existing_only=True) as guard:
                if guard is None: raise HTTPException(409, 'Background shell source guard unavailable')
                await check()
                page = await asyncio.to_thread(bg_jobs.bound_shell_page, **scope, limit=32)
                items = []; omitted = False
                for candidate in page:
                    await check()
                    try:
                        fact = await asyncio.to_thread(bg_jobs.bound_shell_origin, candidate['jobID'], **scope)
                        origin = fact['origin']
                        if origin['generation'] != generation or origin['holder'] != holder: omitted = True; continue
                        response = await supervisor.session_actor_inspection(host_chat_id, origin['engine_session_id'], origin['actor_id'], origin['execution_revision'], owner=owner)
                        if response.status_code != 200 or len(response.content) > 48000: omitted = True; continue
                        actor = response.json()
                        if (type(actor) is not dict or actor.get('actorID') != origin['actor_id']
                                or actor.get('sessionID') != origin['engine_session_id']
                                or actor.get('executionRevision') != origin['execution_revision']): omitted = True; continue
                        after = await asyncio.to_thread(bg_jobs.bound_shell_origin, candidate['jobID'], **scope)
                        if after['origin'] != origin: omitted = True; continue
                        items.append({'jobID':fact['jobID'], 'actorID':origin['actor_id'], 'actorSessionID':origin['engine_session_id'],
                                      'executionRevision':origin['execution_revision'], 'mapRevision':binding.map_revision, 'status':after['status']})
                    except HTTPException: raise
                    except Exception: omitted = True
                    await check()
                await check()
                return {'items': items, 'complete': not omitted and len(page) < 32}
        except HTTPException: raise
        except Exception as exc: raise HTTPException(503, 'Background shell unavailable') from exc

    router.tui_shell_discover = tui_shell_discover
    router.tui_shell_view = tui_shell_view
    router.tui_question_read = read_actor_questions
    router.tui_question_input = submit_actor_input
    return router


def _has_tool(message: dict) -> bool:
    parts = message.get("parts", [])
    return isinstance(parts, list) and any(isinstance(part, dict) and part.get("type") in {"tool", "tool-invocation"} for part in parts)
