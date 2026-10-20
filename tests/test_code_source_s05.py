"""Bounded Files-byte composition plumbing; real native gates are separate."""
import copy
from types import SimpleNamespace

import pytest

from src.openclank import code_query as cq


class Files:
    def __init__(self, body):
        self.body = body
        self.calls = []
        self.tag = "private-object-tag"
        self.second = None

    async def open_handle(self, path):
        self.calls.append(("open", path))
        return dict(handle={"token": "private-handle"}, object_tag=self.tag, size=len(self.body), modified_unix_ms=1)

    async def request_handle(self, operation, handle, payload):
        assert operation == "read_range" and payload["include_fingerprint"] is False
        self.calls.append(("read", copy.deepcopy(payload)))
        offset, length = payload["offset"], payload["length"]
        body = self.second if self.second is not None and sum(c[0] == "read" for c in self.calls) > 1 else self.body
        return {"data": {"bytes": list(body[offset:offset + length]), "offset": offset}}


class Provider:
    def __init__(self):
        self.calls = []
        self.state = "current"
        self.fence = "synthetic-fence"
        self.before_fence = None

    async def code_read_fence(self, **kwargs):
        if self.before_fence:
            self.before_fence()
        return self.fence

    async def code_query(self, **kwargs):
        self.calls.append(kwargs)
        return dict(receipt=kwargs["query"]["expected_receipt"], source_validation={"state": self.state}, cost={"validation_bytes_read": 42})


def setup(monkeypatch, body=b"# literal * outside a symbol\ndef target(): pass\n", *, receipt=True, fields=None, start=0, length=22):
    files, provider = Files(body), Provider()
    monkeypatch.setattr(cq, "_files_client", lambda **_: files)
    binding = dict(owner="alice", owner_subject_id="subject-alice", workspace_id="workspace-a", location_id="loc-a", workspace_revision=1, policy_generation=3, root="/private/fixture", origin="agent", subscriber_id="query:session-a")
    r = dict(workspace_id="workspace-a", registration_id="reg-a", source_snapshot_id="snapshot-a", checkout_id="checkout-a", head_generation="head-a", admission_epoch=0, policy_generation=3, config_fingerprint="producer")
    result = cq.error_result({}, "missing", "No index")
    result.update(error=None, receipt=r if receipt else None, hits=[{"id": "file:" + "a" * 64, "kind": "file", "properties": {"source_hash": "b" * 64}}] if receipt else [])
    query = cq.validate_query(dict(operation="read", fields=fields if fields is not None else ["label", "source", "excerpt", "properties"], read=dict(rel_path="source.py", start_byte=start, length=length)))
    from src.openclank import workspace_policy_service
    monkeypatch.setattr(workspace_policy_service, "resolve_owned_workspace", lambda *a, **kw: SimpleNamespace(workspace=SimpleNamespace(id="workspace-a", owner_subject_id="subject-alice", location_id="loc-a", revision=1), path="/private/fixture"))
    args = dict(result=result, query=query, binding=binding, repository=SimpleNamespace(generation=lambda: 3), provider=provider, auth_manager=None, session=SimpleNamespace(id="session-a", owner="alice", workspace_id="workspace-a"), purpose="agent_workspace")
    return files, provider, args


async def test_actual_window_text_and_utf8_line_locator_are_validated(monkeypatch):
    body = "# αβ\ndef target():\n    return 'a b'\n".encode()
    files, provider, args = setup(monkeypatch, body, start=8, length=20)
    result = await cq._read_source(**args)
    hit = result["hits"][0]
    claim = provider.calls[0]["source_claim"]
    assert claim["predicate"]["text"] == hit["excerpt"]
    assert body[claim["source"]["span"]["start_byte"]:claim["source"]["span"]["end_byte"]].decode() == hit["excerpt"]
    assert claim["source"]["span"]["start_line"] == 2
    assert hit["current_source_verified"] is True
    assert result["cost"]["files_bytes_read"] == 2 * len(body)
    assert result["cost"]["validation_bytes_read"] == 42
    assert all("private" not in str(v) for v in hit.values())
    assert len([c for c in files.calls if c[0] == "read"]) == 2


async def test_large_source_has_range_hash_not_full_source_identity(monkeypatch):
    body = b"x" * (1024 * 1024 + 1)
    files, provider, args = setup(monkeypatch, body, start=100, length=20)
    result = await cq._read_source(**args)
    hit = result["hits"][0]
    assert hit["raw_source"]["hash_scope"] == "byte_range"
    assert hit["evidence_state"] == "observed_range" and hit["current_source_verified"] is False
    assert "source" not in hit and provider.calls == []
    assert result["cost"]["files_bytes_read"] == 40
    assert all(c[1]["length"] == 20 for c in files.calls if c[0] == "read")


async def test_source_mutation_fails_without_text_or_false_currentness(monkeypatch):
    files, provider, args = setup(monkeypatch)
    files.second = b"z" * len(files.body)
    with pytest.raises(cq.CodeQueryError, match="changed") as exc:
        await cq._read_source(**args)
    assert exc.value.code == "stale_cursor" and provider.calls == []


async def test_missing_canonical_index_raw_is_distinct_and_omissions_apply(monkeypatch):
    files, provider, args = setup(monkeypatch, receipt=False, fields=[])
    result = await cq._read_source(**args)
    hit = result["hits"][0]
    assert hit["id"].startswith("raw:") and result["receipt"] is None
    assert set(hit) == {"id", "kind", "matches", "evidence_state", "current_source_verified"}
    assert provider.calls == []


async def test_raw_literal_star_is_real_window_match_not_identifier_alias(monkeypatch):
    _, _, args = setup(monkeypatch, receipt=False)
    args["query"].update(operation="search", query="literal *", channels=["raw"])
    result = await cq._read_source(**args)
    assert len(result["hits"]) == 1 and result["hits"][0]["matches"][0]["channel"] == "raw"
    args["query"]["query"] = "literal  outside"
    assert (await cq._read_source(**args))["hits"] == []


async def test_continuation_binds_owner_fields_budget_and_next_offset(monkeypatch):
    _, _, args = setup(monkeypatch, receipt=False, length=10)
    result = await cq._read_source(**args)
    observation = result["hits"][0]["raw_source"]
    continuation = observation["continuation"]
    args["query"]["read"].update(start_byte=observation["end_byte"], continuation=continuation)
    args["binding"]["owner"] = "bob"
    with pytest.raises(cq.CodeQueryError) as exc:
        await cq._read_source(**args)
    assert exc.value.code == "stale_cursor"


@pytest.mark.parametrize("query", [{"owner": "bob"}, {"root": "/private"}, {"stages": [{"operation": "find"}]}, {"operation": "pipeline"}, {"budgets": {"work": True}}, {"fields": ["private_root"]}, {"read": {"rel_path": "../secret", "start_byte": 0, "length": 1}}, {"filter": {"states": ["x"] * 65}}])
def test_public_validator_rejects_scope_optional_pipeline_and_unbounded_input(query):
    with pytest.raises(cq.CodeQueryError):
        cq.validate_query(query)


def test_code_binding_token_is_purpose_session_workspace_revision_specific():
    base = dict(owner="alice", session_id="chat-a", workspace_id="workspace-a", binding_revision=1)
    token = cq.code_binding_token("synthetic-secret", **base)
    for key, value in [("owner", "bob"), ("session_id", "chat-b"), ("workspace_id", "workspace-b"), ("binding_revision", 2)]:
        assert cq.code_binding_token("synthetic-secret", **{**base, key: value}) != token


@pytest.mark.parametrize("bad", [{"operation": []}, {"operation": {}}, {"direction": []}, {"direction": {}}, [], None, 7, {"read": []}, {"channels": [None]}])
async def test_malformed_json_is_typed_invalid_without_authority_or_provider(bad):
    result = await cq.query_code(repository=None, provider=None, auth_manager=None, owner="alice", workspace_id="a", purpose="app_folder", query=bad)
    assert result["error"]["code"] == "invalid"
    assert result["receipt"] is None
    assert result["cost"]["serialized_bytes"] > 0


@pytest.mark.parametrize("receipt,large", [(False, False), (False, True), (True, True)])
async def test_head_or_epoch_change_blocks_second_acquisition(monkeypatch, receipt, large):
    files, provider, args = setup(monkeypatch, b"x" * (1048577 if large else 30), receipt=receipt)
    def drift():
        if any(c[0] == "read" for c in files.calls):
            provider.fence = "changed-epoch-or-head"
    provider.before_fence = drift
    with pytest.raises(cq.CodeQueryError) as exc:
        await cq._read_source(**args)
    assert exc.value.code == "stale_cursor"
    assert sum(c[0] == "read" for c in files.calls) == 1
    assert args["result"]["cost"]["files_bytes_read"] > 0


async def test_grant_expiry_between_reads_blocks_second_acquisition(monkeypatch):
    from src.openclank import workspace_policy_service as policy
    files, _, args = setup(monkeypatch)
    def resolve(*a, **kw):
        if any(c[0] == "read" for c in files.calls):
            raise policy.WorkspacePolicyServiceError("fixture expiry", code="denied")
        return SimpleNamespace(workspace=SimpleNamespace(owner_subject_id="subject-alice", location_id="loc-a", revision=1), path="/private/fixture")
    monkeypatch.setattr(policy, "resolve_owned_workspace", resolve)
    with pytest.raises(cq.CodeQueryError) as exc:
        await cq._read_source(**args)
    assert exc.value.code == "denied"
    assert sum(c[0] == "read" for c in files.calls) == 1


async def test_missing_index_continuation_rejects_erase_readmit_before_files_open(monkeypatch):
    files, provider, args = setup(monkeypatch, receipt=False, length=10)
    first = await cq._read_source(**args)
    args["query"]["read"].update(start_byte=first["source_window"]["end_byte"], continuation=first["source_window"]["continuation"])
    provider.fence = "new-admitted-epoch"
    calls = len(files.calls)
    with pytest.raises(cq.CodeQueryError) as exc:
        await cq._read_source(**args)
    assert exc.value.code == "stale_cursor" and len(files.calls) == calls


async def test_terminal_empty_window_has_progress_and_truthful_capabilities(monkeypatch):
    _, _, args = setup(monkeypatch, b"plain text", receipt=False, length=100)
    args["query"].update(operation="search", query="absent", channels=["raw", "exact"])
    result = await cq._read_source(**args)
    assert result["hits"] == [] and result["source_window"]["continuation"] is None
    assert result["source_window"]["end_byte"] == 10
    assert next(c for c in result["operation_capabilities"] if c["operation"] == "search")["available"]
    assert next(c for c in result["channels"] if c["channel"] == "exact")["executed"] is False


def test_compact_error_keeps_measured_cost_under_original_512_budget():
    result = cq.finalize_error({"budgets": {"bytes": 512, "tokens": 512}}, "denied", "Source unavailable", observed_cost={"files_bytes_read": 1048576, "source_fence_checks_completed": 3})
    assert result["cost"]["files_bytes_read"] == 1048576
    assert result["cost"]["source_fence_checks_completed"] == 3
    assert result["cost"]["serialized_bytes"] <= 512
