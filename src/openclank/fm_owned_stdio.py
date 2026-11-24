"""Private framed Memery pipe owned by one server process generation."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import sys
from contextlib import asynccontextmanager


MAX_FRAME = 32 * 1024 * 1024


class WorkerRejected(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class OwnedStdio:
    """One process and one opaque owner generation; never reusable."""

    def __init__(self, generation: object):
        self.generation = generation
        self.process = None
        self.poisoned = False
        self.initialized = False
        self._stop_lock = asyncio.Lock()
        self._pending = {}
        self._sequence = 0
        self._reader = None
        self._write_ticket = None
        self._read_ticket = None
        self._exit_confirmed = asyncio.get_running_loop().create_future()

    @property
    def disconnected(self):
        return self._reader

    def current(self, generation: object) -> bool:
        return (generation is self.generation and self.initialized and not self.poisoned
                and self.process is not None and self.process.returncode is None)

    def exit_confirmation(self, generation: object):
        if generation is not self.generation:
            raise RuntimeError('owned memory transport generation changed')
        return self._exit_confirmed

    def _confirm_exit(self, process, result):
        if process is not self.process or type(result) is not int or process.returncode != result:
            raise RuntimeError('owned memory child stop unconfirmed')
        if not self._exit_confirmed.done():
            self._exit_confirmed.set_result(result)

    @staticmethod
    def _argument_digest(arguments, *, document_write=False):
        if type(arguments) is not dict:
            raise ValueError('owned memory request unavailable')
        encoded=json.dumps(arguments,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
        if arguments.get("phase") == "document_segments":
            texts = arguments.get("texts")
            if (document_write or set(arguments) != {"phase", "owner", "workspace_id", "texts"}
                    or any(type(arguments[key]) is not str or not 1 <= len(arguments[key].encode()) <= 512
                           for key in ("owner", "workspace_id"))
                    or type(texts) is not list or not 1 <= len(texts) <= 16
                    or any(type(text) is not str for text in texts)
                    or sum(len(text.encode()) for text in texts) > 256 * 1024
                    or len(encoded) > 2 * 1024 * 1024):
                raise ValueError('owned document structure request unavailable')
            return hashlib.sha256(encoded).hexdigest()
        request=arguments.get("request")
        if arguments.get("phase") == "document_structure_write":
            if (not document_write or set(arguments) != {"phase","owner","workspace_id","request"}
                    or type(request) is not dict or request.get("action") not in ("advance","revoke")
                    or (request["action"] == "advance" and (type(request.get("text")) is not str
                        or len(request["text"].encode()) > 256 * 1024))
                    or len(encoded) > 2 * 1024 * 1024):
                raise ValueError('owned document structure write unavailable')
            return hashlib.sha256(encoded).hexdigest()
        original=request.get("original") if type(request) is dict else None
        rows=request.get("rows") if type(request) is dict else None
        dimension=original.get("dimension") if type(original) is dict else None
        envelope=set(arguments)=={"phase","owner","workspace_id","request"}
        original_keys={"version","owner","account_id","operation_id","provider_ref","model","endpoint_class","dimension","source_watermark","row_count","expected_pointer"}
        bounded=lambda value: type(value) is str and 0<len(value)<=512
        def valid_pointer(value):
            return value is None or (type(value) is dict and set(value)=={"generation_id","publication_id","publication_watermark"}
                and all(bounded(item) for item in value.values()))
        original_valid=(type(original) is dict and set(original)==original_keys and original.get("version")==1
            and all(bounded(original.get(k)) for k in ("owner","account_id","operation_id","provider_ref","model","endpoint_class","source_watermark"))
            and type(original.get("row_count")) is int and 0<=original["row_count"]<=1000000
            and valid_pointer(original.get("expected_pointer")) and type(dimension) is int and 0<dimension<=65536)
        single_vector=(document_write and envelope and arguments.get("phase") in ("document_vector_append","document_vector_intent_append","document_vector_batch_complete")
            and type(request) is dict and set(request)=={"original","offset","rows"} and original_valid
            and type(request.get("offset")) is int and request["offset"]>=0 and type(rows) is list and len(rows)==1 and type(rows[0]) is dict
            and set(rows[0])=={"chunk_id","content_hash","source_revision","values"}
            and bounded(rows[0].get("chunk_id")) and bounded(rows[0].get("content_hash"))
            and type(rows[0].get("source_revision")) is int and rows[0]["source_revision"]>0
            and type(rows[0].get("values")) is list and len(rows[0]["values"])==dimension)
        search_keys={"version","owner","account_id","expected_pointer","provider_ref","model","endpoint_class","dimension","query","limit"}
        query_vector=(not document_write and envelope and arguments.get("phase")=="document_vector_search"
            and type(request) is dict and set(request)==search_keys
            and all(bounded(request.get(k)) for k in ("owner","account_id","provider_ref","model","endpoint_class"))
            and request.get("version")==1 and valid_pointer(request.get("expected_pointer"))
            and type(request.get("limit")) is int and 0<request["limit"]<=10000
            and type(request.get("query")) is list and type(request.get("dimension")) is int
            and 0<request["dimension"]<=65536 and len(request["query"])==request["dimension"])
        phase=arguments.get("phase")
        policy=request.get("policy") if type(request) is dict else None
        selected_original=request.get("original") if type(request) is dict else None
        selected_base=(envelope and phase in ("document_vector_selected_start","document_vector_selected_status","document_vector_selected_commit","document_vector_selected_search")
            and type(policy) is dict and set(policy)=={"workspace_id","policy_projection","policy_input_json"}
            and type(policy.get("workspace_id")) is str and len(policy["workspace_id"])<=512
            and type(policy.get("policy_projection")) is dict
            and len(json.dumps(policy["policy_projection"],separators=(",",":"),allow_nan=False).encode())<=8192
            and type(policy.get("policy_input_json")) is str and len(policy["policy_input_json"].encode())<=65536
            and type(selected_original) is dict and set(selected_original)=={"version","owner","account_id","expected_pointer","choice"}
            and selected_original.get("version")==1 and bounded(selected_original.get("owner")) and bounded(selected_original.get("account_id"))
            and valid_pointer(selected_original.get("expected_pointer")) and type(selected_original.get("choice")) is dict
            and len(json.dumps(selected_original["choice"],separators=(",",":"),allow_nan=False).encode())<=4096)
        selected_small=(selected_base and ((phase=="document_vector_selected_start" and set(request)=={"original","policy"})
            or (phase in ("document_vector_selected_status","document_vector_selected_commit") and set(request)=={"original","policy","handle"} and bounded(request.get("handle")))))
        selected_query=(selected_base and phase=="document_vector_selected_search"
            and set(request)=={"original","policy","profile_id","query","limit","effort","visit_budget","candidate_limit","selector"}
            and request.get("profile_id")=="" and request.get("selector") is None
            and type(request.get("query")) is list and 0<len(request["query"])<=65536
            and all(type(request.get(k)) is int and 0<request[k]<=10000 for k in ("limit","effort","visit_budget","candidate_limit")))
        if len(encoded) > (4 * 1024 * 1024 if (document_write and (arguments.get("phase")=="document_upsert" or single_vector)) or query_vector or selected_query else (131072 if selected_small else 65536)):
            raise ValueError('owned memory request unavailable')
        return hashlib.sha256(encoded).hexdigest()

    def arm_write(self, generation, name, arguments, ticket):
        """Arm one exact allowlisted request without retaining its arguments."""
        if (not self.current(generation) or self._write_ticket is not None or self._read_ticket is not None or name!='_host_candidate_review'
            or type(arguments) is not dict or arguments.get('phase') not in ('mirror_apply','document_vector_intent_prepare','document_vector_intent_claim','document_vector_intent_cancel','document_vector_intent_begin','document_vector_intent_append','document_vector_intent_finalize','document_vector_batch_begin','document_vector_batch_claim','document_vector_batch_complete','document_vector_batch_finalize','document_vector_selected_commit','document_vector_begin','document_vector_append','document_vector_finalize','document_remove','document_upsert','document_structure_write','question_seed_bootstrap','account_candidate_apply','account_value_successor_apply','analysis_snapshot_publish','domain_definition_review','metadata_assignment_apply','s05_reserve_prepare','s05_reserve','skill_draft_retire','skill_draft_prepare','skill_draft_reserve','skill_draft_dispatch','skill_draft_terminal','skill_draft_revoke','skill_draft_prepared_cancel','s08_model_prepare','s08_model_reserve','s08_model_dispatch','s08_policy_commit','s08_policy_terminal','s08_policy_prepared_cancel','connection_review_apply','s08_policy_review_apply','s02_importance_prepare','s02_importance_apply','s08_rules_once','queued_origin_fence','source_reset_fence','observation_reset_fence','guarded_reset_commit','guarded_reset_cleanup','helped_observation','derived_compaction','standing_guidance_apply','explicit_add','explicit_add_review_edit','explicit_add_review_apply','import_review_apply','import_staged_review_edit','import_staged_review_apply','confirmed_question_apply','published_lifecycle_apply','paired_review_apply','paired_reset_fence','owner_transition_cache_cleanup','owner_purge_cache_cleanup','paired_rename_fence','paired_rename_renew','source_candidate','paired_source_candidate','pending_pair_fence','terminal_pair_fence','erased_pair_fence','pending_pair_rebind','phase_root_fence','phase_root_renew','phase_root_observation_fence','phase_root_observation_renew','native_memory_ops_batch_apply','preextracted_stage','preextracted_host_anchor_stage','preextracted_review','knowledge_update_batch_apply','guarded_batch_forget','guarded_batch_forget_restore')):
            raise RuntimeError('owned memory write unavailable')
        ticket['document_write'] = arguments.get('phase') in ('document_upsert','document_structure_write','document_vector_append','document_vector_intent_append','document_vector_batch_complete')
        ticket['frame_digest']=self._argument_digest(arguments, document_write=ticket['document_write'])
        ticket['frame_id']=None;ticket['frame_invalid']=False;ticket['terminal_error']=False
        self._write_ticket=ticket

    def disarm_write(self, ticket):
        if self._write_ticket is ticket:
            # The owner writes directly to stdin. Once call() returns there
            # is no intermediate transport queue that could issue this frame.
            ticket['may_issue'] = False
            self._write_ticket = None

    def arm_read(self, generation, name, arguments, ticket):
        """Track an allowlisted private read on this process generation."""
        if (not self.current(generation) or self._write_ticket is not None or self._read_ticket is not None
            or name!='_host_candidate_review' or type(arguments) is not dict
            or arguments.get('phase') not in ('question_digest','owner_purge_stats','paired_erased_fact','paired_reset_fence_read','paired_review_original','paired_original_page','paired_review_page','code_library_observe','code_manifest_compare','document_segments','document_structure_read','document_vector_intent_status','document_vector_batch_status','document_vector_intent_text','document_vector_selected_start','document_vector_selected_status','document_vector_selected_search','document_vector_search','document_vector_head','document_vector_text','document_remove_original','document_source_lookup','document_vector_snapshot','document_upsert_original','prepared_knowledge_read','mirror_context','explicit_add_status','explicit_add_review_preview','explicit_add_review_status','selective_ingest_partition','guarded_full_memory_preview','guarded_selective_ingest_preview','explicit_add_review_original','explicit_add_missing_original_page','explicit_add_missing_terminal_page','import_review_status','import_staged_review_preview','import_staged_review_status','import_staged_review_original','import_missing_original_page','import_missing_terminal_page','explicit_add_capability','pending_pair_original','source_publication_original','paired_publication_original','s02_rename_assessment','s02_importance_capability','s02_importance_context','s02_importance_resolve','s02_importance_review_context','s02_importance_target_manifest','s02_importance_original','s08_policy_capability','skill_draft_capability','s08_last_retained_run','m3_rank','connection_predicate_options','connection_review_manifest','connection_review_original','s08_policy_review_manifest','s08_policy_review_reject_preview','s08_policy_review_original','s08_review_kind','s08_original_scoped','s05_reserve_original','s05_reserve_readiness','skill_draft_retire_original','skill_draft_prepare_original','skill_draft_reserve_original','skill_draft_original','skill_draft_result_original','s08_model_prepare_original','s08_model_reserve_original','s08_model_original','s08_policy_result_original','native_memory_ops_capability','native_memory_ops_batch_preview','native_memory_ops_batch_status','preextracted_list','preextracted_review_preview','preextracted_host_anchor_status','knowledge_update_batch_preview','knowledge_update_batch_status','journal_delta_status','guarded_batch_forget_preview','guarded_batch_forget_status')):
            raise RuntimeError('owned memory read unavailable')
        ticket['frame_digest']=self._argument_digest(arguments)
        ticket['frame_id']=None;ticket['frame_invalid']=False;ticket['frame_seen']=False;ticket['terminal_error']=False
        ticket['terminal_event']=asyncio.Event()
        self._read_ticket=ticket

    def disarm_read(self, ticket):
        if self._read_ticket is ticket:self._read_ticket=None

    def _observe_outgoing(self, frame):
        ticket=self._write_ticket or self._read_ticket
        if ticket is None:
            return True, None
        try:
            request_id=frame['id']
            valid_id=(type(request_id) is int and request_id>=0) or (type(request_id) is str and 0<len(request_id)<=128)
            if (ticket['frame_invalid'] or not valid_id or frame.get('operation')!='_host_candidate_review'
                or ticket['frame_id'] is not None or self._argument_digest(frame.get('arguments'), document_write=ticket is self._write_ticket and ticket.get('document_write') is True)!=ticket['frame_digest']):
                ticket['frame_invalid']=True
                return (False,request_id if valid_id else None) if ticket is self._write_ticket or ticket.get('strict_frame') is True else (True,None)
            ticket['frame_id']=request_id
            if ticket is self._read_ticket and ticket.get('strict_frame') is True:
                try:
                    current=(self.current(ticket['generation']) and asyncio.get_running_loop().time()<ticket['deadline']
                        and ticket['frame_check']() is True)
                except Exception:
                    current=False
                if not current:
                    ticket['positive_unsent']=True
                    return False,request_id
            if ticket is self._write_ticket:
                try:
                    current=(ticket.get('may_issue') is True and self.current(ticket['generation'])
                        and asyncio.get_running_loop().time()<ticket['deadline']
                        and ticket['frame_check']() is True
                        and (ticket.get('before_issue') is None or ticket['before_issue']() is True))
                except Exception:
                    current=False
                # Synchronous authority checks may themselves consume the
                # deadline; recheck the exact generation immediately before send.
                if current:
                    current=(self._write_ticket is ticket and ticket.get('may_issue') is True
                        and self.current(ticket['generation']) and asyncio.get_running_loop().time()<ticket['deadline'])
                if not current:
                    ticket['may_issue']=False
                    ticket['positive_unsent']=True
                    callback=ticket.get('on_unsent')
                    if callback is not None: callback()
                    ticket['on_unsent']=None
                    return False,request_id
                # From this point onward even a partial/failed send is unknown.
                ticket['issued']=True
            return True,None
        except Exception:
            ticket['frame_invalid']=True
            return (False,None) if ticket is self._write_ticket or ticket.get('strict_frame') is True else (True,None)

    def _observe_incoming(self, frame):
        ticket=self._write_ticket or self._read_ticket
        if ticket is None or ticket['frame_invalid'] or ticket['frame_id'] is None:
            return
        try:
            if type(frame) is not dict or frame.get('id')!=ticket['frame_id']: return
            if (type(frame.get('id')) is not type(ticket['frame_id']) or ticket.get('frame_seen') is True
                or frame.get('type')!='reply' or ('error' in frame)==('result' in frame)):
                ticket['frame_invalid']=True;ticket['terminal_error']=False;return
            ticket['frame_seen']=True
            if ticket is self._read_ticket:
                ticket['terminal_event'].set()
            error=frame.get('error')
            ticket['terminal_error']=(type(error) is dict and type(error.get('code')) is str and error['code'] not in ('internal', 'cancelled', 'deadline') and type(error.get('message')) is str)
        except Exception:
            ticket['frame_invalid']=True;ticket['terminal_error']=False

    async def terminate_and_wait(self, generation: object) -> int:
        """Poison first; return only after this exact child has been waited."""
        if generation is not self.generation:
            raise RuntimeError('owned memory transport generation changed')
        self.poisoned = True
        async with self._stop_lock:
            process = self.process
            if process is None:
                raise RuntimeError('owned memory child unavailable')
            waiter = asyncio.create_task(process.wait())
            if process.returncode is None:
                try:
                    process.terminate()
                except (ProcessLookupError, OSError):
                    pass
            done, _ = await asyncio.wait((waiter,), timeout=2.0)
            if waiter not in done and process.returncode is None:
                try:
                    process.kill()
                except (ProcessLookupError, OSError):
                    pass
            # No timeout can turn an unconfirmed stop into a settled write.
            result = await waiter
            self._confirm_exit(process,result)
            return result

    async def _receive(self):
        stream = self.process.stdout
        size = int.from_bytes(await stream.readexactly(4), "big")
        if not 0 < size <= MAX_FRAME:
            raise ConnectionError("invalid Memery frame length")
        raw = await stream.readexactly(size)
        frame = await asyncio.to_thread(json.loads, raw) if size > 65536 else json.loads(raw)
        if type(frame) is not dict or type(frame.get("version")) is not int or frame["version"] != 1:
            raise ConnectionError("invalid Memery frame version")
        return frame, size

    async def _receive_replies(self):
        try:
            while True:
                frame, size = await self._receive()
                request_id = frame.get("id")
                if (frame.get("type") != "reply" or type(request_id) is not str
                        or ("result" in frame) == ("error" in frame)):
                    raise ConnectionError("invalid Memery reply")
                error = frame.get("error")
                if "error" in frame and (type(error) is not dict or type(error.get("code")) is not str
                        or type(error.get("message")) is not str):
                    raise ConnectionError("invalid Memery error")
                self._observe_incoming(frame)
                waiter = self._pending.get(request_id)
                if waiter is None or waiter.done():
                    continue
                if error is not None:
                    exc = (ConnectionError(error["message"]) if error["code"] == "internal"
                           else WorkerRejected(error["code"], error["message"]))
                    waiter.set_exception(exc)
                else:
                    waiter.set_result((frame["result"], size))
        finally:
            self.initialized = False
            for waiter in self._pending.values():
                if not waiter.done():
                    waiter.set_exception(ConnectionError("Memery worker disconnected"))

    async def call(self, operation, arguments, *, timeout=120.0, caller=None):
        if self._reader.done() or self.poisoned:
            raise ConnectionError("Memery worker unavailable")
        self._sequence += 1
        request_id = str(self._sequence)
        frame = {"version": 1, "type": "call", "id": request_id, "operation": operation,
                 "arguments": arguments, "deadline_ms": int((time.time() + timeout) * 1000)}
        packet = self._encode(frame)
        waiter = asyncio.get_running_loop().create_future()
        self._pending[request_id] = waiter
        def cancel_abandoned(done):
            if done.cancelled() and not waiter.done():
                if not self.process.stdin.is_closing():
                    self.process.stdin.write(self._encode({"version": 1, "type": "cancel", "id": request_id}))
                waiter.set_exception(WorkerRejected("cancelled", "Memery read cancelled"))

        try:
            allowed, _ = self._observe_outgoing(frame)
            if not allowed:
                raise WorkerRejected("denied", "owned Memery request not issued")
            self.process.stdin.write(packet)
            if caller is not None:
                caller.add_done_callback(cancel_abandoned)
            async with asyncio.timeout(timeout):
                await self.process.stdin.drain()
                return await waiter
        except (asyncio.CancelledError, TimeoutError):
            if not self.process.stdin.is_closing():
                self.process.stdin.write(self._encode({"version": 1, "type": "cancel", "id": request_id}))
            raise
        finally:
            if caller is not None:
                caller.remove_done_callback(cancel_abandoned)
            self._pending.pop(request_id, None)
            if not waiter.done():
                waiter.cancel()
            elif not waiter.cancelled():
                waiter.exception()

    @staticmethod
    def _encode(frame):
        body = json.dumps(frame, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        if len(body) > MAX_FRAME:
            raise ValueError("Memery frame too large")
        return len(body).to_bytes(4, "big") + body

    @asynccontextmanager
    async def open(self, command, env, *, errlog=sys.stderr):
        if self.process is not None:
            raise RuntimeError("Memery worker generation already started")
        self.process = await asyncio.create_subprocess_exec(
            command, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=errlog, start_new_session=os.name == "posix")
        try:
            async with asyncio.timeout(60):
                frame, _ = await self._receive()
            if frame.get("type") != "ready" or type(frame.get("profile")) is not dict:
                raise ConnectionError("Memery worker readiness unavailable")
            self._reader = asyncio.create_task(self._receive_replies(), name="memery-replies")
            yield frame["profile"]
        finally:
            async def close():
                self.initialized = False
                if self._reader is not None:
                    self._reader.cancel()
                    await asyncio.gather(self._reader, return_exceptions=True)
                # A malformed or oversized frame may leave stdout paused with
                # unread bytes. Drain it while waiting for exact child exit.
                async def discard_stdout():
                    while await self.process.stdout.read(65536):
                        pass
                discard = asyncio.create_task(discard_stdout())
                self.process.stdin.close()
                try:
                    await asyncio.wait_for(self.process.wait(), 2)
                except TimeoutError:
                    await self.terminate_and_wait(self.generation)
                self._confirm_exit(self.process, self.process.returncode)
                await discard
            cleanup = asyncio.create_task(close())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
