"""App DTO/accessor tests; fake transport is not native qualification."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.openclank import code_visual as cv

GOLDENS = Path(__file__).resolve().parents[1] / "mcp_servers/frankenmemory/crates/fm-core/tests/fixtures"


def golden(name="published"):
    value = json.loads((GOLDENS / f"code_map_{name}.json").read_text())
    return value


def request(mode="map", **changes):
    body = {"schema": cv.SCHEMA, "operation": "overview"} if mode == "map" else {
        "schema": cv.SCHEMA, "authority": golden("selection")["authority"],
        "selection_id": golden("selection")["selection_id"]}
    return cv.validate_visual_request({"mode": mode, "request": {**body, **changes}})


def binding():
    return dict(workspace_id="fixture-workspace", workspace_revision=1, policy_generation=1)


def wire(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


@pytest.mark.parametrize("name,mode", [("preview", "map"), ("published", "map"), ("selection", "selection")])
def test_golden_projection_preserves_receipt_and_remeasures_provider_additions(name, mode):
    original = golden(name)
    original["cost"]["native_rpc_calls_completed"] = 300
    before = copy.deepcopy(original)
    result = cv.project_visual_result(original, request(mode), binding())
    assert result["authority"] == original["authority"]
    assert result["cost"]["serialized_bytes"] == len(wire(result)) > 0
    assert result["cost"]["native_rpc_calls_completed"] == 300
    assert original == before  # Golden cost isn't credited as actual runtime evidence.
    if name == "preview":
        assert "receipt" not in result and "receipt" not in result["authority"]
        assert result["selection"] is None


@pytest.mark.parametrize("patch", [
    {"owner": "other"}, {"root": "/private/root"}, {"operation": "admit"},
    {"operation": "pipeline"}, {"schema": "old"}, {"limits": {"tokens": 10000}},
    {"limits": {"nodes": True}}, {"limits": {"bytes": 511}}, {"limits": {"nodes": 201}},
    {"limits": {"work": float("inf")}}, {"cursor": ""}, {"cursor": "a" * 4097},
    {"cursor": "next"}, {"relations": []}, {"relations": ["call", "call"]},
    {"relations": ["secret"]}, {"direction": "sideways"}, {"operation": "neighbors"},
    {"root": {"kind": "visual", "id": "x"}},
])
def test_invalid_map_request(patch):
    with pytest.raises(cv.CodeVisualError):
        request(**patch)


@pytest.mark.parametrize("patch", [{"mode": "agent"}, {"query": {}}, {"mode": True}, {"request": []}])
def test_visual_wrapper_strict(patch):
    with pytest.raises(cv.CodeVisualError):
        cv.validate_visual_request({**request(), **patch})


def test_defaults_copy_and_canonical_resource_root():
    authority = golden()["authority"]
    raw = {"mode": "map", "request": {"schema": cv.SCHEMA, "operation": "neighbors",
        "expected_authority": authority, "root": {"kind": "resource", "resource_id": "decl:Ab01", "receipt": authority["receipt"]}}}
    normalized = cv.validate_visual_request(raw)
    raw["request"]["expected_authority"]["summary_id"] = "mutated"
    assert normalized["request"]["expected_authority"]["summary_id"] != "mutated"
    assert normalized["request"]["limits"] == cv.LIMITS
    for resource in ("decl:fixture", "/root", "vm1:geometry", "occ:"):
        changed = copy.deepcopy(normalized)
        changed["request"]["root"]["resource_id"] = resource
        with pytest.raises(cv.CodeVisualError):
            cv.validate_visual_request(changed)


@pytest.mark.parametrize("patch", [{"authority": golden("preview")["authority"]}, {"max_refs": True},
    {"max_refs": 0}, {"max_refs": 51}, {"selection_id": ""}, {"cursor": "x" * 4097}, {"root": "x"}])
def test_selection_request_is_published_only_and_bounded(patch):
    with pytest.raises(cv.CodeVisualError):
        request("selection", **patch)


@pytest.mark.parametrize("raw", [b'{"workspace_id":"a","workspace_id":"b","visual_request":{}}',
    b'{"workspace_id":"a","visual_request":{"mode":"map","request":{"schema":"open-clank.code-map/v1","operation":"overview","limits":{"bytes":NaN}}}}',
    b'[' * 40 + b']' * 40, b'{"workspace_id":true,"visual_request":{}}', bytes([255]),
    b'{"workspace_id":"a","visual_request":{},"owner":"bob"}', b' ' * (cv.REQUEST_LIMIT + 1)])
def test_raw_duplicates_nonfinite_depth_types_bytes_and_privacy(raw):
    with pytest.raises(cv.CodeVisualError):
        cv.parse_body(raw)


def mutate(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement


@pytest.mark.parametrize("path,replacement", [
    (("source",), "/private/body"), (("authority", "root"), "/private/root"),
    (("authority", "receipt", "owner"), "secret"), (("operation",), "search"),
    (("direction",), "incoming"), (("relations",), ["call"]),
    (("nodes", 0, "label"), "x" * 161), (("nodes", 0, "source_files"), True),
    (("nodes", 0, "source_files"), cv.MAX_INT + 1), (("nodes", 0, "signature"), "secret"),
    (("nodes", 0, "languages"), [{"language": "python", "count": 3}]),
    (("nodes", 1, "parent"), "vm1:n:published-caller"),
    (("edges", 0, "from"), "absent"), (("edges", 0, "state"), "exact"),
    (("edges", 0, "selection_id"), "forbidden"), (("edges", 2, "selection_id"), None),
    (("edges", 2, "count"), 0), (("edges", 2, "state"), "structure"),
    (("coverage", "inventory_complete"), 1), (("coverage", "total_entries"), 0),
    (("coverage", "dependencies"), "indexing"), (("coverage", "unavailable_entries"), 1),
    (("page", "returned_nodes"), 1), (("page", "complete"), "true"),
    (("page", "remaining_edges"), 1), (("page", "next_cursor"), "next"),
    (("page", "truncated"), True), (("root",), "absent"),
    (("cost", "source_bytes_read"), 1), (("cost", "native_rpc_calls_completed"), True),
    (("cost", "path"), "/private"), (("cost", "sql_vm_steps"), float("nan")),
])
def test_map_malformed_private_backend_data(path, replacement):
    result = golden()
    mutate(result, path, replacement)
    with pytest.raises(cv.CodeVisualError) as error:
        cv.project_visual_result(result, request(), binding())
    assert error.value.code == "invalid_evidence"


@pytest.mark.parametrize("field", ["indexed_files", "parse_failed_files"])
def test_preview_never_promotes_counts(field):
    value = golden("preview")
    value["coverage"][field] = 0
    with pytest.raises(cv.CodeVisualError):
        cv.project_visual_result(value, request(), binding())


def test_selected_root_endpoints_duplicates_cycle_cursor_and_final_bytes():
    result = golden()
    root = result["nodes"][1]["id"]
    req = request(operation="neighbors", root={"kind": "visual", "id": root}, expected_authority=result["authority"])
    result.update(operation="neighbors", root=root)
    result["edges"] = [edge for edge in result["edges"] if root in (edge["from"], edge["to"])]
    result["page"]["returned_edges"] = len(result["edges"])
    result["page"].update(complete=False, next_cursor="next", remaining_edges=2)
    assert cv.project_visual_result(result, req, binding())["root"] == root
    for variant in ("wrong_root", "duplicate", "cycle", "repeated_cursor", "missing_endpoint"):
        changed, changed_req = copy.deepcopy(result), copy.deepcopy(req)
        if variant == "wrong_root":
            changed["root"] = changed["nodes"][2]["id"]
        elif variant == "duplicate":
            changed["nodes"][2]["id"] = root
        elif variant == "cycle":
            changed["nodes"][0]["parent"] = root
        elif variant == "repeated_cursor":
            changed_req["request"]["cursor"] = "next"
        else:
            changed["nodes"].pop()
            changed["page"]["returned_nodes"] -= 1
        with pytest.raises(cv.CodeVisualError):
            cv.project_visual_result(changed, changed_req, binding())
    with pytest.raises(cv.CodeVisualError) as error:
        cv.project_visual_result(golden(), request(limits={"bytes": 512}), binding())
    assert error.value.code == "budget_exceeded"


@pytest.mark.parametrize("path,replacement", [
    (("references", 0, "occurrence_id"), "occ:fixture-call"),
    (("references", 0, "caller_id"), "file:abc"),
    (("references", 0, "target_id"), "vm1:something"),
    (("references", 0, "state"), "structure"),
    (("references", 0, "relation"), "containment"),
    (("references", 0, "source"), "/private"),
    (("receipt", "head_generation"), "other"),
    (("selection_id",), "other"), (("remaining",), 2), (("next_cursor",), "next"),
])
def test_selection_does_not_invent_evidence_or_hide_remaining(path, replacement):
    value = golden("selection")
    mutate(value, path, replacement)
    with pytest.raises(cv.CodeVisualError):
        cv.project_visual_result(value, request("selection"), binding())


def test_selection_paging_and_file_selection_binding():
    value = golden("selection")
    value.update(remaining=1, complete=False, next_cursor="next")
    assert cv.project_visual_result(value, request("selection"), binding())["remaining"] == 1
    value["references"].append(copy.deepcopy(value["references"][0]))
    with pytest.raises(cv.CodeVisualError):
        cv.project_visual_result(value, request("selection"), binding())
    value = golden()
    root = value["nodes"][1]["id"]
    req = request(operation="neighbors", expected_authority=value["authority"],
        root={"kind":"resource", "resource_id":"decl:abcdef", "receipt":value["authority"]["receipt"]})
    value.update(operation="neighbors", root=root,
        selection={"visual_id":root, "resource_id":"file:abcdef", "receipt":value["authority"]["receipt"]})
    value["edges"] = [edge for edge in value["edges"] if root in (edge["from"], edge["to"])]
    value["page"]["returned_edges"] = len(value["edges"])
    assert cv.project_visual_result(value, req, binding())["selection"]["resource_id"] == "file:abcdef"
    value["selection"]["resource_id"] = root
    with pytest.raises(cv.CodeVisualError):
        cv.project_visual_result(value, req, binding())


@pytest.fixture
def accessor(monkeypatch):
    current, calls = ["stable"], []
    native = {**binding(), "origin":"app", "root":"/synthetic/private"}
    def resolve(**arguments):
        assert arguments["purpose"] == "app_folder" and arguments["session"] is None
        calls.append(arguments)
        if current[0] == "denied":
            raise ValueError("private authority detail")
        return dict(native), (current[0],)
    monkeypatch.setattr(cv, "resolve_query_binding", resolve)
    return current, calls, dict(repository=object(), auth_manager=object(), owner="alice", workspace_id="fixture-workspace")


@pytest.mark.parametrize("native_error", [False, True, "raises"])
@pytest.mark.parametrize("change", ["rebound", "denied"])
async def test_postawait_change_rejects_success_error_and_exception(accessor, native_error, change):
    state, calls, arguments = accessor
    async def invoke(**kwargs):
        state[0] = change
        await asyncio.sleep(0)
        if native_error == "raises":
            raise RuntimeError("secret body")
        return {"error":{"code":"missing", "message":"secret"}, "cost":{}} if native_error else golden()
    result = await cv.query_visual(**arguments, provider=SimpleNamespace(code_visual=invoke), visual_request=request())
    assert result["error"]["code"] == "denied" and len(calls) == 2
    assert "secret" not in wire(result).decode() and "authority" not in result and "nodes" not in result


async def test_accessor_errors_cost_whitelist_mutation_and_cancellation(accessor):
    _, calls, arguments = accessor
    async def invoke(**kwargs):
        kwargs["visual_request"]["request"]["operation"] = "admit"
        kwargs["binding"]["workspace_id"] = "other"
        return {"error":{"code":"erased", "message":"/private/root"},
            "cost":{"native_rpc_calls_completed":4, "metadata_bytes":12, "root":"secret"}}
    result = await cv.query_visual(**arguments, provider=SimpleNamespace(code_visual=invoke), visual_request=request())
    assert result["error"]["code"] == "erased" and result["cost"]["native_rpc_calls_completed"] == 4
    assert result["cost"]["metadata_bytes"] == 12 and "private" not in wire(result).decode()
    assert result["cost"]["serialized_bytes"] == len(wire(result)) and len(calls) == 2
    async def cancelled(**kwargs):
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await cv.query_visual(**arguments, provider=SimpleNamespace(code_visual=cancelled), visual_request=request())


@pytest.fixture
def real_scope(tmp_path, monkeypatch):
    from src import secret_storage
    from src.openclank.file_policy import FilePolicyRepository
    monkeypatch.setenv("OPEN_CLANK_AUTHORITY_DB_PATH", str(tmp_path / "authority.db"))
    monkeypatch.setenv("OPEN_CLANK_AUTHORITY_AUTH_PATH", str(tmp_path / "auth.json"))
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    users = {name:dict(account_id="test-" + name, is_admin=name == "alice") for name in ("alice", "bob")}
    auth = SimpleNamespace(account_id=lambda name: users.get(name, {}).get("account_id"),
        is_admin=lambda name: users.get(name, {}).get("is_admin", False))
    repository = FilePolicyRepository(tmp_path / "authority.db")
    repository.sync_subjects(users)
    workspaces, grants = {}, {}
    for name in users:
        root = tmp_path / name
        root.mkdir()
        location = repository.create_location(actor_subject_id="test-admin", path=str(root), kind="directory", capabilities=("read",))
        grants[name] = repository.create_binding(actor_subject_id="test-admin", binding_class="people",
            subject_id="test-" + name, location_id=location.id, capabilities=("read",))
        workspaces[name] = repository.create_workspace(actor_subject_id="test-" + name,
            owner_subject_id="test-" + name, location_id=location.id, name=name)
    return repository, auth, workspaces, grants


@pytest.mark.parametrize("code", ["busy", "denied", "cancelled", "unknown-private-code"])
async def test_typed_provider_rejection_code_is_sanitized_and_rechecks(accessor, code):
    from src.memory_provider import MemoryRequestRejectedError
    _, calls, arguments = accessor
    async def rejected(**kwargs):
        raise MemoryRequestRejectedError("/private/provider-detail", code=code)
    result = await cv.query_visual(**arguments, provider=SimpleNamespace(code_visual=rejected), visual_request=request())
    assert result["error"]["code"] == (code if code in cv.ERROR_CODES else "unavailable")
    assert len(calls) == 2 and "private" not in wire(result).decode()


async def test_real_registry_ownership_even_admin_and_postawait_revoke(real_scope):
    repository, auth, workspaces, grants = real_scope
    calls = []
    async def invoke(**kwargs):
        calls.append(kwargs)
        value = golden()
        value["authority"]["receipt"].update(workspace_id=kwargs["binding"]["workspace_id"],
            policy_generation=kwargs["binding"]["policy_generation"])
        return value
    args = dict(repository=repository, auth_manager=auth, owner="alice", provider=SimpleNamespace(code_visual=invoke), visual_request=request())
    denied = await cv.query_visual(**args, workspace_id=workspaces["bob"].id)
    assert denied["error"]["code"] == "denied" and not calls
    result = await cv.query_visual(**args, workspace_id=workspaces["alice"].id)
    assert "error" not in result and calls[-1]["binding"]["origin"] == "app"
    async def revoked(**kwargs):
        result = await invoke(**kwargs)
        repository.revoke_binding(grants["alice"].id, actor_subject_id="test-admin")
        await asyncio.sleep(0)
        return result
    args["provider"] = SimpleNamespace(code_visual=revoked)
    assert (await cv.query_visual(**args, workspace_id=workspaces["alice"].id))["error"]["code"] == "denied"


@pytest.mark.parametrize("name", ["preview", "published"])
def test_native_shape_overview_repository_anchor_is_returned_and_not_arbitrary(name):
    value = golden(name)
    repository = {"id":"vn1:0:fixture-mac", "label":"Repository", "kind":"repository", "parent":None,
        "source_files":2, "asset_files":1, "languages":[{"language":"python", "count":2}]}
    value["nodes"][0]["parent"] = repository["id"]
    value["nodes"].insert(0, repository)
    value["edges"].insert(0, {"id":"ve1:repository:fixture-mac", "from":repository["id"],
        "to":value["nodes"][1]["id"], "relation":"containment", "state":"structure", "count":1, "selection_id":None})
    value["root"] = repository["id"]
    value["page"].update(returned_nodes=len(value["nodes"]), returned_edges=len(value["edges"]))
    projected = cv.project_visual_result(value, request(), binding())
    assert projected["root"] == repository["id"] and projected["cost"]["serialized_bytes"] == len(wire(projected))
    for invalid in ("absent", value["nodes"][1]["id"], value["nodes"][2]["id"]):
        changed = copy.deepcopy(value)
        changed["root"] = invalid
        with pytest.raises(cv.CodeVisualError):
            cv.project_visual_result(changed, request(), binding())


def test_selection_incomplete_requires_nonempty_advancing_references():
    value = golden("selection")
    value.update(references=[], remaining=3, complete=False, next_cursor="next")
    with pytest.raises(cv.CodeVisualError):
        cv.project_visual_result(value, request("selection"), binding())
    # A genuinely terminal empty aggregate remains representable.
    value.update(remaining=0, complete=True, next_cursor=None)
    assert cv.project_visual_result(value, request("selection"), binding())["references"] == []


@pytest.mark.parametrize("direction,root_index,accepted", [
    ("incoming", 1, True), ("outgoing", 0, True), ("both", 0, True), ("both", 1, True),
    ("outgoing", 1, False), ("incoming", 0, False), ("both", 2, False),
])
def test_neighbors_containment_obeys_selected_root_and_direction(direction, root_index, accepted):
    value = golden()
    root = value["nodes"][root_index]["id"]
    req = request(operation="neighbors", root={"kind":"visual", "id":root},
        expected_authority=value["authority"], direction=direction, relations=["containment"])
    value.update(operation="neighbors", root=root, direction=direction, relations=["containment"])
    value["edges"] = [value["edges"][0]]
    value["page"]["returned_edges"] = 1
    if accepted:
        assert cv.project_visual_result(value, req, binding())["edges"][0]["relation"] == "containment"
    else:
        with pytest.raises(cv.CodeVisualError):
            cv.project_visual_result(value, req, binding())

