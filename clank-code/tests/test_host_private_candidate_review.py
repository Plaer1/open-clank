"""Host-private operation cannot enter generic memory dispatch."""
import asyncio

import pytest

from src.memery_provider import MemeryProvider, MemoryRequestRejectedError, BROKER_MEMORY_TOOLS


def test_private_review_denied_by_generic_dispatch():
    provider = object.__new__(MemeryProvider)
    async def check():
        assert "_host_candidate_review" not in BROKER_MEMORY_TOOLS
        with pytest.raises(MemoryRequestRejectedError):
            await provider._invoke_scoped_tool("_host_candidate_review", {})
        with pytest.raises(MemoryRequestRejectedError):
            await provider._call_tool_transport("_host_candidate_review", {})
        from src.openclank import lifetools_server
        response = await lifetools_server.call_tool("_host_candidate_review", {})
        assert response.isError
    asyncio.run(check())


def test_private_preview_uses_native_record_and_read_only_head(tmp_path):
    from types import SimpleNamespace
    from tests.test_memery_v2_bridge import _db
    from src.memery_provider import _HostCandidatePreview
    path = str(tmp_path / "preview.db")
    _db(path)
    provider = object.__new__(MemeryProvider)
    provider._fm_db_path = path
    candidate = {"id":"candidate-1", "content":"A fact", "kind":"episodic", "confidence_score":0.6, "importance_score":0.5, "owner":"alice", "workspace_id":"global", "workspace_path":None, "session_id":"", "turn_id":"t", "raw_evidence_ids":[], "evidence_role":"assertion", "source":"ai_agent", "source_event_id":"event", "dedup_key":"key", "source_uri":"", "source_revision":1, "content_hash":"b"*64, "source_message_ids":[], "status":"pending", "reason":"pending", "accepted_curated_id":None, "created_at":"now", "updated_at":"now"}
    record = {"id":"m_native", "content":"A fact", "owner":"alice", "workspace_id":"global", "kind":"episodic", "source":"ai_agent", "source_type":"human", "confidence_score":0.6, "importance_score":0.5, "session_id":"", "source_message_ids":[], "metadata":{"manually_reviewed":True, "candidate_id":"candidate-1", "admission_reason":"approved"}}
    async def fake_request(owner, workspace, payload):
        assert (owner, workspace, payload["phase"], payload["candidate_id"]) == ("alice", "global", "preview", "candidate-1")
        return {"snapshot_version":"s03.candidate-snapshot.v1", "snapshot_digest":"a"*64, "candidate":candidate, "curated_record":record}
    provider._host_candidate_request = fake_request
    result = asyncio.run(provider._host_candidate_preview(_HostCandidatePreview("alice", "global", "candidate-1", "approved")))
    assert result["curated_id"] == "m_native" and result["expected_v2_head"] == 0
    assert result["candidate_digest"] == "a"*64 and result["projection"]["record_id"] == "m_native"
    assert result["projection"]["value"] == {"type":"string", "value":"A fact"}


def test_private_replay_returns_historical_without_preview():
    from src.memery_provider import _HostCandidateReview
    provider = object.__new__(MemeryProvider)
    calls = []
    async def fake_request(owner, workspace, payload):
        calls.append((owner, workspace, payload))
        return {"state":"applied", "candidate_id":"candidate-1", "curated_id":"m_native", "applied_revision":1, "applied":{"revision":1}, "field_decision":{"review_paths":[]}}
    async def no_preview(_):
        raise AssertionError("replay must not preview or mutate")
    provider._host_candidate_request = fake_request
    provider._host_candidate_preview = no_preview
    request = _HostCandidateReview("alice", "global", "candidate-1", "a"*64, 0, ("/value",), "request-1", "approved")
    result = asyncio.run(provider._host_candidate_review(request))
    assert result["applied_revision"] == 1
    assert len(calls) == 1 and calls[0][2] == {"phase":"replay", "candidate_id":"candidate-1", "candidate_digest":"a"*64, "expected_v2_head":0, "selected_paths":["/value"], "request_key":"request-1", "reason":"approved"}


def test_private_mirror_rechecks_sealed_preview_before_apply():
    import hashlib
    import json
    from src.memery_provider import _HostMirrorApply

    provider = object.__new__(MemeryProvider)
    operation = {"version":"s03.published-update.v1", "owner":"alice", "workspace":"global", "project":"", "block_id":"block-1", "expected_revision":1, "payload":{"confidence":0.5}, "request_key":"req", "actor_type":"user", "actor_id":"alice", "reason":"review", "source_event_id":None, "approved_paths":["/confidence"], "producer":None}
    sealed = json.dumps(operation, separators=(",", ":"))
    digest = hashlib.sha256(sealed.encode()).hexdigest()
    calls = []
    async def fake(owner, workspace, payload):
        calls.append(payload["phase"])
        if payload["phase"] == "mirror_replay":
            return {"state":"needs_preview"}
        if payload["phase"] == "mirror_preview":
            return {"version":"s03.mirror-preview.v1", "binding":{"owner":owner,"workspace":workspace,"project":"","curated_id":"memory-1","block_id":"block-1","revision":1}, "expected_revision":1,"selected_paths":["/confidence"],"changes":[],"normalized_payload":{"confidence":0.5},"operation_json":sealed,"operation_sha256":digest}
        return {"state":"applied","receipt_id":"receipt-2","binding":{"version":"s03.curated-mirror.v1","owner":"alice","workspace":"global","project":"","curated_id":"memory-1","block_id":"block-1","revision":2,"guarded_change_set_id":"change-2","semantic_sha256":"a"*64,"field_map":{"content":"/value"}},"applied":{"revision":2,"change_set_id":"change-2"},"field_decision":{}}
    provider._host_candidate_request = fake
    request = _HostMirrorApply("alice","global","memory-1","candidate","origin","prior",1,"req","review",("/confidence",),sealed,digest)
    assert asyncio.run(provider._host_mirror_apply(request))["state"] == "applied"
    assert calls == ["mirror_replay", "mirror_preview", "mirror_apply"]
    calls.clear()
    wrong = _HostMirrorApply("alice","global","other","candidate","origin","prior",1,"req","review",("/confidence",),sealed,digest)
    with pytest.raises(MemoryRequestRejectedError):
        asyncio.run(provider._host_mirror_apply(wrong))
    assert calls == ["mirror_replay", "mirror_preview"]


def test_private_mirror_context_requires_exact_curated_identity():
    from src.memery_provider import _HostMirrorContext
    provider = object.__new__(MemeryProvider)
    async def fake(owner, workspace, payload):
        assert payload == {"phase":"mirror_context", "curated_id":"memory-1"}
        return {"mode":"guarded", "candidate_id":"candidate", "origin_request_key":"origin", "prior_receipt_id":"receipt", "expected_revision":2,
                "binding":{"version":"s03.curated-mirror.v1", "owner":owner, "workspace":workspace, "project":"", "curated_id":"different", "revision":2}}
    provider._host_candidate_request = fake
    with pytest.raises(MemoryRequestRejectedError):
        asyncio.run(provider._host_mirror_context(_HostMirrorContext("alice", "global", "memory-1")))


def test_host_candidate_split_uses_frozen_target_and_prepared_embedding():
    from src.memery_provider import _HostCandidateReview, _admit_host_candidate_embedding
    from src.memery_external_sources import SourceRef
    from src.openclank.chat_source_bridge import _HostChatFenceCapability
    from types import SimpleNamespace
    provider = object.__new__(MemeryProvider)
    calls = []
    request = _HostCandidateReview('alice','global','candidate','a'*64,0,('/value',),'request','approved_by_user')
    async def fake(owner, workspace, payload):
        calls.append((owner,workspace,payload))
        if payload['phase'] == 'candidate_embedding':
            return {'state':'prepared','candidate_id':'candidate','candidate_digest':'a'*64,'embedding':[0.25,0.5]}
        return {'state':'applied','candidate_id':'candidate','curated_id':'curated','applied_revision':1,'applied':{'revision':1},'field_decision':{}}
    provider._host_candidate_request = fake
    source = SourceRef(1,'host_user_body','account','chat','main','message','body',1,'b'*64,'files',None)
    binding = SimpleNamespace(memory_owner='alice',memory_workspace='global')
    mapping = SimpleNamespace(validate_chat_guard=lambda chat, guard: None)
    authority = SimpleNamespace(resolve=lambda account: binding)
    guard = _HostChatFenceCapability(mapping,object(),provider,authority,binding,'account','alice','chat',False,())
    async def run():
        admission = _admit_host_candidate_embedding(provider,request,source,guard)
        vector = await provider._host_candidate_prepare_embedding(request,admission=admission)
        with pytest.raises(MemoryRequestRejectedError):
            await provider._host_candidate_prepare_embedding(request,admission=admission)
        outcome = await provider._host_candidate_apply_prepared(request,{'frozen':'projection'},vector,source=source,guard=guard)
        assert outcome['state'] == 'applied'
    asyncio.run(run())
    assert [call[2]['phase'] for call in calls] == ['candidate_embedding','candidate_apply_prepared']
    assert calls[1][2]['candidate_digest'] == 'a'*64 and calls[1][2]['projection'] == {'frozen':'projection'}
    assert calls[1][2]['embedding'] == [0.25,0.5]
