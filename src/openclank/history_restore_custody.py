"""Owned History restore; a claim is custody, never a Files grant."""
import asyncio
import os
from fastapi import HTTPException
from src.openclank import file_policy as file_policy_module
from src.openclank.history_io import (bounded_history_io as _bounded_history_io, HISTORY_IO_EXECUTOR as _HISTORY_IO_EXECUTOR, HISTORY_IO_SLOTS as _HISTORY_IO_SLOTS)

async def owned_host_restore(*, owner, account_id, auth, barrier, client, requested_id,
                             private_id, path, restore_id, action_id, version_id, version_ref, call,
                             provider, repository, context, generation, revalidate_source, original_current,
                             claim_factory=None, settle_claim=None, refuse_unclaimed=None,
                             destination_authorizer=None, terminal_parser=None):
    """Own the original History IPC and Host claim beyond HTTP cancellation."""
    from src.openclank.files_host_provider import host_origin_for_path
    if auth is None or barrier is None:
        raise HTTPException(503, "History restore authority unavailable")
    if not os.path.isabs(path) or os.path.realpath(path) != path:
        raise HTTPException(503, "History restore destination is unavailable")
    if (provider is None or repository is not getattr(provider, "operation_store", None)
            or repository.generation() != generation or not callable(original_current) or not callable(revalidate_source)):
        raise HTTPException(503, "Files authority changed")

    def current_file_policy(resource):
        candidates = []
        locations = repository.list_locations()
        if len(locations) > 512:
            return False
        for location in locations:
            if (not location.enabled or location.availability != "available" or not location.canonical_path
                    or location.kind not in {"exact_file", "directory"}):
                continue
            root = location.canonical_path
            try:
                inside = path == root if location.kind == "exact_file" else os.path.commonpath((root, path)) == root
            except ValueError:
                inside = False
            if inside:
                candidates.append(location)
        if resource.location_id is not None:
            candidates = [location for location in candidates if location.id == resource.location_id]
        elif candidates:
            longest = max(len(location.canonical_path) for location in candidates)
            candidates = [location for location in candidates if len(location.canonical_path) == longest]
        if len(candidates) != 1:
            return False
        location = candidates[0]
        relative = "" if path == location.canonical_path else os.path.relpath(path, location.canonical_path)
        workspace = resource.workspace_id
        if workspace is not None:
            selected = repository.get_workspace(workspace)
            if selected.archived or selected.owner_subject_id != account_id or selected.location_id != location.id:
                return False
        for capability in ("read", "write"):
            effective = repository.resolve(subject_id=account_id, is_admin=context.is_admin, origin="app",
                location_id=location.id, workspace_id=workspace, relative_resource=relative,
                capability=capability, observation_only=True)
            if not effective.allowed or effective.policy_generation != generation:
                return False
        return (location.id, location.revision, location.canonical_path, workspace)

    def account_current():
        try:
            return (auth.account_id(owner) == account_id and auth.username_for_account_id(account_id) == owner
                and original_current() is True
                and repository.generation() == generation)
        except Exception:
            return False

    async def run():
        from dataclasses import replace
        claim = None
        issued = False
        release = False
        async with barrier.admitted(owner):
            try:
                if not account_current():
                    raise HTTPException(503, "History restore authority changed")
                if destination_authorizer is not None:
                    current=await destination_authorizer();working_context=replace(context,workspace_id=current['workspace_id']);policy=current
                    if not account_current():raise HTTPException(403,"History restore destination is unavailable")
                else:
                    current = await provider.resource_for_path(context, path=path)
                    working_context = replace(context, workspace_id=current.workspace_id)
                    policy = current_file_policy(current)
                    if (current.origin_id != host_origin_for_path(path) or current.kind != "file"
                            or not provider._path_writable(working_context, path) or not policy or not account_current()):
                        raise HTTPException(403, "History restore destination is unavailable")
                try:
                    claim = (claim_factory(working_context.workspace_id) if claim_factory is not None else
                        repository.claim_host_mutation(owner_subject_id=account_id,
                            workspace_id=current.workspace_id, generation=generation, paths=[path]))
                except file_policy_module.FilePolicyError as exc:
                    raise HTTPException(409, "History restore destination is busy or unavailable") from exc
                source_current = await revalidate_source()
                if version_ref is not None:
                    selected = await _bounded_history_io(lambda: client.resolve_resource_version(private_id, version_ref))
                    version = selected.get("version") or {}
                    if version.get("action_id") != action_id or version.get("version_id") != version_id:
                        raise HTTPException(409, "History restore source changed")
                if destination_authorizer is not None:
                    current_again=await destination_authorizer()
                    if source_current is not True or current_again!=current or not account_current():raise HTTPException(409,"History restore destination changed")
                else:
                    current_again = await provider.resource_for_path(working_context, path=path)
                    if (source_current is not True or current_again.origin_id != current.origin_id
                            or current_again.kind != "file" or not provider._path_writable(working_context, path)
                            or current_file_policy(current_again) != policy or not account_current()):
                        raise HTTPException(409, "History restore destination changed")
                try:
                    await asyncio.wait_for(_HISTORY_IO_SLOTS.acquire(), timeout=0.25)
                except asyncio.TimeoutError as exc:
                    raise HTTPException(503, "History is busy; retry shortly") from exc
                future = None
                try:
                    if destination_authorizer is not None and (await destination_authorizer()!=current or await revalidate_source() is not True):
                        raise HTTPException(409,"History restore authority changed")
                    if not account_current() or (destination_authorizer is None and current_file_policy(current_again) != policy):
                        raise HTTPException(409, "History restore authority changed")
                    future = asyncio.get_running_loop().run_in_executor(_HISTORY_IO_EXECUTOR, call)
                    issued = True
                    cancelled = False
                    while True:
                        try:
                            response = await asyncio.shield(future)
                            break
                        except asyncio.CancelledError:
                            cancelled = True
                            if future.done():
                                response = future.result()
                                break
                finally:
                    _HISTORY_IO_SLOTS.release()
                receipt = terminal_parser(response) if terminal_parser is not None else response.get("Restore") if isinstance(response, dict) else None
                resources = receipt.get("resources") if isinstance(receipt, dict) else None
                verification = receipt.get("verification") if isinstance(receipt, dict) else None
                terminal = {"Complete", "Conflict", "Partial", "Missing", "Corrupt", "Expired", "MetadataExpired", "UnknownVersion", "Unauthorized"}
                release = bool(terminal_parser is not None and isinstance(receipt,dict) and receipt.get('terminal') is True) if terminal_parser is not None else bool(isinstance(receipt, dict) and receipt.get("source_action_id") == action_id
                    and receipt.get("source_version_id") == version_id and receipt.get("outcome") in terminal
                    and isinstance(resources, list) and len(resources) == 1 and isinstance(resources[0], dict)
                    and resources[0].get("resource_id") == private_id and resources[0].get("outcome") in terminal
                    and (verification is None or (isinstance(verification, dict)
                        and verification.get("restore_id") == restore_id and verification.get("resource_id") == private_id
                        and verification.get("version_id") == version_id)))
                if not release:
                    raise HTTPException(503, "History restore outcome is unknown")
                if cancelled:
                    raise asyncio.CancelledError
                return response
            finally:
                if claim is not None and (not issued or release):
                    if settle_claim is not None:
                        settle_claim(claim, receipt if issued and release else None)
                    else:
                        repository.release_host_mutation(owner_subject_id=account_id, **claim)
                elif claim is None and not issued and refuse_unclaimed is not None:
                    refuse_unclaimed()

    task = asyncio.create_task(run())
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=13.0)
    except asyncio.TimeoutError as exc:
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        raise HTTPException(504, "History restore outcome is pending") from exc
    except asyncio.CancelledError:
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        raise

