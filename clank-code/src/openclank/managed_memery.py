"""Authenticated model-facing Memery management and replay receipts."""
import asyncio
import hashlib
import json

from sqlalchemy.exc import IntegrityError

from src.openclank.operation_journal import OperationJournalStore, canonical_request_hash

_READS = frozenset({"list", "search"})


def _reply(state, text, receipt=None):
    return {"state": state, "text": text[:131072], **({"operationID": receipt} if receipt else {})}


def _guidance(provider, owner, account, workspace, boundary, arguments, current):
    """The model may inspect its applied rules and draft a change for owner review."""
    from src.general_hex_guidance import GeneralHexGuidance
    from src.general_hex_library import GeneralHexConflict, GeneralHexError
    from src.general_hex_runtime import active_hex_db_path

    path = active_hex_db_path(provider)
    if not path or not workspace:
        return _reply("unavailable", "The turn has no scoped guidance store.")
    service = GeneralHexGuidance(path)
    try:
        # Do not create a new snapshot when the original turn had none.
        with service.repo._connection() as conn:
            row = conn.execute(
                "SELECT snapshot_json FROM fm_general_hex_snapshots WHERE owner_id=? AND boundary_id=?",
                (owner, boundary),
            ).fetchone()
        if not row:
            return _reply("unavailable", "The turn has no saved guidance snapshot.")
        rules = [{key: item[key] for key in ("hex_id", "revision", "title")}
                 for item in json.loads(row[0])["entries"] if item["state"] == "included"]
        if arguments["action"] == "guidance_context":
            evidence = service.evidence(owner, workspace, limit=20, search=arguments.get("query", "")) if rules else {"items":[]}
            return _reply("complete", json.dumps({"rules": rules, "evidence": evidence["items"]}, ensure_ascii=False))
        if not any(r["hex_id"] == arguments["hex_id"] and r["revision"] == arguments["expected_revision"] for r in rules):
            raise GeneralHexConflict("Choose a rule and revision from this turn’s guidance context.")
        draft = service.propose(owner, account_id=account, workspace_id=workspace,
            hex_id=arguments["hex_id"], expected_revision=arguments["expected_revision"], action=arguments["change"],
            reason=arguments["reason"], evidence_ids=arguments["evidence_ids"], body=arguments.get("body"),
            actor="assistant", current=current)
        return _reply("complete", json.dumps({"draft_id": draft["draft_id"], "pending_review": True}))
    except GeneralHexError as exc:
        return _reply("rejected", str(exc))


async def _perform(store, *, identity, account, chat, owner, model_route, arguments, current, execute):
    """A pending durable reservation is never reissued after cancellation or restart."""
    action = arguments["action"]
    if not await current():
        return _reply("unavailable", "Memery authority changed.")
    if action in _READS:
        result = await execute()
        if not await current():
            return _reply("unavailable", "Memery authority changed.")
        return _reply("rejected" if "error" in result else "complete", json.dumps(result, ensure_ascii=False))

    # Provider retries can allocate new engine message and tool-call IDs. Bind
    # identical writes to the same owner/chat/root/actor and exact arguments.
    key = canonical_request_hash({"domain": "memery.manage.v1", "account": account, "chat": chat,
        "root": identity["rootOperationID"], "actor": identity["actorID"], "arguments": arguments})
    begin = dict(owner=owner, root_operation_id=identity["rootOperationID"], operation="memery." + action,
        idempotency_key=key, request={"key": key}, connection_id=model_route.connection_id,
        billing_lane=model_route.billing_lane, model_route_id=model_route.model_route_id)
    try:
        row, replay = await asyncio.to_thread(store.begin, **begin)
    except IntegrityError:
        # Another execution inserted this unique receipt between lookup and insert.
        row, replay = await asyncio.to_thread(store.begin, **begin)
    if not await current():
        return _reply("unavailable", "Memery authority changed.")
    if replay:
        if row.state == "complete" and row.attempts:
            return _reply("complete", json.dumps(row.attempts[-1]), row.id)
        return _reply("outcome_unknown", "This request was reserved previously. Its outcome is unconfirmed.", row.id)

    # The reservation commits before the mutation. Interruption or uncertain
    # transport leaves it pending, preventing duplicate application.
    async def issue():
        try:
            result = await execute()
            if "error" in result:
                return _reply("outcome_unknown", str(result["error"]), row.id)
            public = {k: v for k, v in result.items() if k in ("action", "memory_id", "candidate_id", "pending_review")}
            await asyncio.to_thread(store.cas, owner=owner, operation_id=row.id, expected_revision=row.revision,
                state="complete", commit_reason="memery_management", attempt=public)
            return _reply("complete", json.dumps(result, ensure_ascii=False), row.id)
        except Exception:
            return _reply("outcome_unknown", "Memery did not confirm the reserved write.", row.id)

    task = asyncio.create_task(issue())
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        # Keep the account barrier until the issued write and its receipt settle.
        await task
        raise
    if not await current():
        return _reply("unavailable", "Memery authority changed.")
    return result


async def manage(bridge, request):
    from core.models import get_session_manager_instance
    from routes.prefs_routes import _load_for_user
    from src import ai_interaction
    from src.memory_gate import memory_mode, write_allowed
    from src.openclank.chat_routing import resolve_chat_route

    unavailable = _reply("unavailable", "This turn has no current Memery management authority.")
    identity, arguments = request["identity"], request["arguments"]
    key = bridge._managed_turn_key(identity)
    try:
        manager = get_session_manager_instance()
        state = manager.lifecycle_context
        auth = state.auth_manager
        entry = bridge._managed_actor_turns[key]
        owner, account, chat = entry["owner"], entry["ownerAccountID"], entry["stableChatID"]
        root_id = entry["rootSessionID"]
        root = bridge._turns[root_id]
        credential = root.original_credential_current
        session = manager.get_session(chat)
        revision = entry["policyRevision"]
        worker = state.mimo_supervisor.worker_for_owner(owner)
        provider = bridge._memory_provider

        binding = await bridge._handle_session_binding_read({"sessionID": root_id})
        digest = hashlib.sha256(json.dumps(["openclank.memery.manage.v1", sorted(arguments.items())],
            ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        proof_request = {**{k: v for k, v in identity.items() if k != "sessionID"},
            "stableChatID": chat, "messageID": request["messageID"], "toolCallID": request["toolCallID"],
            "toolName": "memory", "argsSHA256": digest}
        if identity["sessionID"] != root_id:
            proof_request["sourceSessionID"] = identity["sessionID"]

        async def current():
            admitted = await bridge.resolve_managed_control_turn(identity, "manage_memory")
            context = bridge._session_context.get(root_id, {})
            if (binding.get("authority") or binding.get("transition") or binding.get("memoryEnabled") is not True
                    or binding.get("owner") != owner or binding.get("stableChatID") != chat or worker is None
                    or binding != await bridge._handle_session_binding_read({"sessionID": root_id})):
                return False
            source = await worker.session_memory_inferred_source_request(chat, root_id, binding["canonicalCwd"],
                proof_request, owner=owner)
            return (source == {"state": "current", "argsSHA256": digest} and admitted == {"owner": owner, "ownerAccountID": account} and bool(account)
                and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                and auth.get_privileges(owner).get("can_manage_memory") is True
                and manager.get_session(chat) is session and session.owner == owner and not session.incognito
                and not getattr(session, "archived", False) and (session.workspace_id or "") == (entry.get("authorityWorkspaceID") or "")
                and bridge._managed_actor_turns.get(key) is entry and entry.get("policyRevision") == revision
                and bridge._turns.get(root_id) is root and root.root_operation_id == identity["rootOperationID"]
                and not root.provider_root_closing and callable(credential) and root.original_credential_current is credential
                and credential() is True and context.get("memory_read_allowed") is True and not context.get("auxiliary")
                and not context.get("incognito") and not context.get("private_authority") and not root.privacy_capture_veto
                and provider is not None and ai_interaction._memory_provider is provider and state.memory_provider is provider
                and worker is not None and state.mimo_supervisor.worker_for_owner(owner) is worker
                and worker.bridge is bridge and worker.is_alive())

        async with state.account_request_barrier.admitted(owner):
            if not await current():
                return unavailable
            prefs = _load_for_user(owner) or {}
            action = arguments["action"]
            if prefs.get("memory_enabled") is False or memory_mode(prefs) == "off":
                return unavailable
            if action in {"guidance_context", "propose_guidance"}:
                from src.openclank.memery_preferences import owner_preferences
                workspace = binding.get("memoryWorkspaceID")
                if not workspace or not owner_preferences(owner)["memery_agent_search_enabled"]:
                    return unavailable

                # Recheck the live turn inside the draft transaction, immediately before
                # commit. The model cannot supply workspace, account, or actor authority.
                loop = asyncio.get_running_loop()
                def still_current():
                    proof = asyncio.run_coroutine_threadsafe(current(), loop)
                    try:
                        return proof.result(timeout=5) and owner_preferences(owner)["memery_agent_search_enabled"]
                    finally:
                        proof.cancel()

                work = asyncio.create_task(asyncio.to_thread(_guidance, provider, owner, account, workspace,
                    identity["rootOperationID"], arguments, still_current))
                try:
                    result = await asyncio.shield(work)
                except asyncio.CancelledError:
                    await work
                    raise
                return result if await current() and owner_preferences(owner)["memery_agent_search_enabled"] else unavailable
            if action in {"edit", "resolve", "delete"}:
                allowed, reason = write_allowed(prefs)
                if not allowed:
                    return _reply("rejected", reason)
            if action not in _READS and getattr(provider, "_guarded_v17", False):
                return _reply("review_required", "Use the Memery review surface for this guarded mutation. Use save for an inferred proposal.")
            route = resolve_chat_route(owner=owner, endpoint_id=session.endpoint_id, model_id=session.model,
                model_route_id=session.provider_model_route_id)
            return await _perform(OperationJournalStore(), identity=identity, owner=owner, account=account, chat=chat,
                model_route=route, arguments=arguments, current=current,
                execute=lambda: ai_interaction.do_manage_memory(action, session_id=chat, owner=owner, management_args=arguments))
    except Exception:
        return unavailable
