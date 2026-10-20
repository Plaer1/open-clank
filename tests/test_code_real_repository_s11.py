"""S11 actual first-party repository, copied bytes only; no corpus execution.

Requires explicit isolated startup and current non-feature native/Files pins.
The allowlist is also the sibling real-browser fixture's corpus authority.
"""
import ast
import asyncio
import hashlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

REPOSITORY = Path(__file__).resolve().parents[1]
PYTHON_NAMES = (
    "code_query code_context code_graph_projection code_index_lifecycle code_resource_bridge "
    "file_policy file_policy_compat workspace_policy_service permission_grants request_auth "
    "resource_refs filesystem_registry files_service_client files_facade files_host_provider "
    "files_image_store session_map session_operations session_deletion sql_owner_lifecycle "
    "operation_journal operation_router operation_approvals"
).split()
RUST_PATHS = (
    "bin/odysseus-files-service.rs", "bin/odysseus-quicklook-helper.rs",
    "bin/odysseus-shell-thumbnail-helper.rs", "engine.rs", "grpc_transport.rs",
    "history_capture.rs", "lib.rs", "local_ipc.rs", "quicklook_protocol.rs",
    "service.rs", "thumbnail.rs", "windows_fs.rs",
)
CORPUS_PATHS = tuple([f"src/openclank/{name}.py" for name in PYTHON_NAMES] +
                     [f"packages/odysseus-files/src/{name}" for name in RUST_PATHS])


def corpus_manifest():
    """Explicit regular UTF8 source only, never glob secrets/private/vendor data."""
    records = []
    for relative in CORPUS_PATHS:
        path = REPOSITORY / relative
        assert stat.S_ISREG(path.lstat().st_mode) and not path.is_symlink()
        assert path.resolve().is_relative_to(REPOSITORY)
        data = path.read_bytes()
        data.decode("utf8")
        records.append(dict(path=relative, bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
    return records


def manifest_digest(records):
    return hashlib.sha256(json.dumps(records, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def copy_corpus(destination):
    """Writes only a newly created canonical test workspace, not original source."""
    destination = Path(destination)
    assert destination.is_dir() and not list(destination.iterdir())
    records = corpus_manifest()
    for record in records:
        original = REPOSITORY / record["path"]
        target = destination / record["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        data = original.read_bytes()
        assert hashlib.sha256(data).hexdigest() == record["sha256"]
        target.write_bytes(data)
    return records


from tests.test_code_graph_native import _assert_startup, mounted, post
_assert_startup()  # canonical temporary paths and exact binary BEFORE app imports
from tests import test_code_index_lifecycle as lifecycle
from src.openclank.code_query import query_code
from src.openclank.code_resource_bridge import resolve_resource
from src.openclank.files_service_client import close_all_clients

scope = lifecycle.scope


async def repository_provider(scope, tmp_path, monkeypatch):
    expected = os.environ["OPEN_CLANK_TEST_DEFAULT_FM_SHA256"]
    binary = Path(os.environ["OPEN_CLANK_TEST_FM_MCP_BIN"])
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == expected
    assert hashlib.sha256(Path(os.environ["ODYSSEUS_FILES_SERVICE_BIN"]).read_bytes()).hexdigest() == "4844112f850113499b687e5212fe04e21259e43483e4d7ef25cbd6dd879ec478"
    return await lifecycle.native_provider(scope, tmp_path, monkeypatch)


async def publish(scope, provider, previous=None, action="ensure", owner="alice"):
    admitted=lifecycle.native_request(scope,action=action,owner=owner)
    status_request=dict(admitted,action="status")
    start = time.perf_counter()
    await provider.code_index_lifecycle(**admitted)
    # Liveness polling is not a job-timeout policy; finite fixture teardown only.
    async with asyncio.timeout(120):
        while True:
            value = await provider.code_index_lifecycle(**status_request)
            if value["lifecycle"] == "ready" and value["head_generation"] != previous:
                return value, round((time.perf_counter() - start) * 1000, 3)
            assert value["lifecycle"] not in {"failed", "erased"}, json.dumps(value, sort_keys=True)
            await asyncio.sleep(.025)


async def ask(scope, provider, query, owner="alice", session=None):
    return await query_code(repository=scope[0], provider=provider, auth_manager=lifecycle.Auth(),
        owner=owner, workspace_id=scope[1]["alice"].id,
        purpose="agent_workspace" if session else "app_folder", session=session, query=query)


def search(text, channel="exact", path=None):
    query = dict(operation="search", query=text, channels=[channel],
                 fields=["label", "source", "signature", "resolution", "witnesses", "properties"])
    if path:
        query["filter"] = dict(rel_path=path, kinds=["declaration"])
    return query


def declaration(result, name, path):
    assert result["error"] is None, result
    return next(hit for hit in result["hits"] if hit["kind"] == "declaration"
                and hit.get("label", "").split(".")[-1] == name
                and hit["source"]["rel_path"] == path)


def logical_snapshot(database, owner="alice"):
    """Complete producer payload equality, normalize opaque scope IDs only.

    All source ranges/hashes, signatures, bindings, diagnostics, resolutions,
    witness order and graph properties remain. Wall-clock and run provenance
    are compared separately, not confused with logical equality.
    """
    with sqlite3.connect(database) as connection:
        checkout = connection.execute("SELECT checkout_id FROM code_registrations WHERE owner=?", (owner,)).fetchone()[0]
        files = [(path, json.loads(payload)) for path, payload in connection.execute(
            "SELECT rel_path,payload_json FROM code_file_evidence WHERE owner=? ORDER BY rel_path", (owner,))]
        ids = {checkout: "<checkout>"}
        for path, evidence in files:
            ids[evidence["file_id"]] = "file:" + path
            for kind in ("scopes", "declarations", "occurrences", "bindings", "imports"):
                for row in evidence[kind]:
                    # A source occurrence (including duplicates) has its exact
                    # parsed byte span as independent local identity.
                    span = row.get("source", {}).get("span", row.get("span"))
                    key = dict(kind=kind, path=path, span=span,
                               name=row.get("qualified_name", row.get("symbol_text", row.get("name", row.get("local_name")))) )
                    ids[row["id"]] = json.dumps(key, sort_keys=True)
                    if "identity_key" in row:
                        ids[row["identity_key"]] = json.dumps(dict(path=path, name=row["qualified_name"], signature=row["signature"]), sort_keys=True)
            ids[evidence["public_surface_fingerprint"]] = "public-surface:" + path
        def normalize(value):
            if isinstance(value, dict):
                return {key: normalize(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [normalize(item) for item in value]
            if isinstance(value, str):
                if value in ids:
                    return ids[value]
                return "::".join(ids.get(part, part) for part in value.split("::"))
            return value
        nodes = connection.execute("SELECT id,kind,label,name,norm_name,layer,ref_table,ref_id,trust,candidate_id,status FROM graph_nodes WHERE owner=?", (owner,)).fetchall()
        node_names = {row[0]: normalize(row[3]) for row in nodes}
        graph_nodes = [normalize(row[1:]) for row in nodes]
        edges = [normalize([node_names[src], tag, node_names[dst], *rest]) for src, tag, dst, *rest in connection.execute(
            "SELECT src_id,tag,dst_id,fact_id,weight,traversal_count,trust,candidate_id,status FROM graph_edges WHERE owner=?", (owner,))]
        cues = [normalize([cue, node_names[node], *rest]) for cue, node, *rest in connection.execute(
            "SELECT cue,node_id,source,candidate_id,status FROM graph_cues WHERE owner=?", (owner,))]
        resolutions = [normalize(json.loads(row[0])) for row in connection.execute(
            "SELECT payload_json FROM code_resolutions WHERE owner=? ORDER BY rel_path,occurrence_id", (owner,))]
        receipts = list(connection.execute("SELECT rel_path,blake3,size,symbol_count,config_fingerprint FROM code_files ORDER BY rel_path"))
        latest = connection.execute("SELECT coverage FROM code_index_runs WHERE owner=? AND status IN ('ready','verified') ORDER BY rowid DESC LIMIT 1", (owner,)).fetchone()
        coverage = json.loads(latest[0])
        coverage = [row for row in coverage if row["status"] != "deleted"]
        for row in coverage:
            row.pop("source_generation", None)
            if row["status"] in {"indexed", "unchanged"}:
                row["status"] = "current"
        def ordered(rows):
            return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
        normalized_files = [normalize(value) for _, value in files]
        # The producer sorts occurrences/candidates by opaque checkout-scoped
        # IDs (code_evidence.rs). Independent checkouts change that order, not
        # the evidence. Re-sort only these ID-ordered collections after ID
        # normalization; retain every row, source span and witness order.
        for evidence in normalized_files:
            evidence["occurrences"].sort(key=lambda row: row["id"])
        for resolution in resolutions:
            resolution["candidate_ids"].sort()
        return dict(files=ordered(normalized_files), resolutions=ordered(resolutions),
                    nodes=ordered(graph_nodes), edges=ordered(edges), cues=ordered(cues),
                    receipts=receipts, coverage=ordered(coverage))


async def test_default_small_real_modules_and_recall_ignore_fixture_control(scope, tmp_path, monkeypatch):
    root=Path(lifecycle.native_request(scope)["root"])
    selected=("src/openclank/resource_refs.py", "packages/odysseus-files/src/quicklook_protocol.rs")
    before=corpus_manifest()
    for relative in selected:
        target=root/relative;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes((REPOSITORY/relative).read_bytes())
    provider,gate=await repository_provider(scope,tmp_path,monkeypatch)
    try:
        ready,elapsed=await publish(scope,provider)
        assert ready["coverage_complete"]
        declaration(await ask(scope,provider,search("issue_resource_ref",path=selected[0])),"issue_resource_ref",selected[0])
        declaration(await ask(scope,provider,search("encode_command",path=selected[1])),"encode_command",selected[1])
        assert await provider.recall("s11 nothing stored",owner="alice")==[]
        assert not list(gate.iterdir()),"Unreleased fixture gate must be inert in normal build"
        assert corpus_manifest()==before
        print("S11_SMALL_REAL "+json.dumps(dict(files=2,source_bytes=sum((root/p).stat().st_size for p in selected),initial_ms=elapsed)))
    finally:
        close_all_clients();await provider.shutdown()


def test_public_work_capacity_default_parity_and_finite_boundary():
    from src.openclank.code_query import DEFAULT_BUDGETS, MAX_QUERY_WORK, CodeQueryError, search_code_schema, validate_query
    from src.openclank.code_graph_projection import CodeGraphProjectionError, prepare_graph_query
    assert MAX_QUERY_WORK == 4_000_000
    omitted = validate_query(dict(operation="catalog"))
    explicit = validate_query(dict(operation="catalog", budgets=dict(DEFAULT_BUDGETS)))
    assert omitted == explicit
    graph, _ = prepare_graph_query(dict(operation="catalog"))
    assert graph["budgets"]["work"] == MAX_QUERY_WORK
    schema = search_code_schema()["function"]["parameters"]
    assert schema["properties"]["budgets"]["properties"]["work"]["maximum"] == MAX_QUERY_WORK
    with pytest.raises(CodeQueryError):
        validate_query(dict(operation="catalog", budgets=dict(work=MAX_QUERY_WORK + 1)))
    with pytest.raises(CodeGraphProjectionError):
        prepare_graph_query(dict(operation="catalog", budgets=dict(work=MAX_QUERY_WORK + 1)))
    assert validate_query(dict(operation="catalog", budgets=dict(work=1)))["budgets"]["work"] == 1


async def test_actual_repository_queries_source_and_copy_lifecycle(scope, tmp_path, monkeypatch):
    from core.models import Session
    from src import ai_interaction
    from src.frankenmemory_provider import FrankenmemoryProvider
    from src.memory_provider import MemoryRequestRejectedError
    from src.tool_execution import execute_tool_block
    from src.tool_schemas import function_call_to_tool_block

    root = Path(lifecycle.native_request(scope)["root"])
    records = copy_corpus(root)
    assert len(records) == 35 and len(RUST_PATHS) == 12
    original = manifest_digest(records)
    tree = ast.parse((root / "src/openclank/code_query.py").read_bytes())
    oracle = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "query_code")
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "resolve_owned_workspace" for node in ast.walk(oracle))
    rust = (root / "packages/odysseus-files/src/quicklook_protocol.rs").read_text()
    assert "pub fn encode_command(command: &Command) -> Result<Vec<u8>, WireError>" in rust
    provider, gate = await repository_provider(scope, tmp_path, monkeypatch)
    metrics = dict(corpus_digest=original, files=len(records), source_bytes=sum(row["bytes"] for row in records), phases={})
    session = Session(id="s11-real-repository", name="copied real source", owner="alice", endpoint_url="", model="", workspace_id=scope[1]["alice"].id)
    prior_sessions = ai_interaction.get_session_manager()
    prior_memory = tuple(getattr(ai_interaction, key) for key in ("_memory_manager", "_memory_vector", "_memory_provider", "_memory_lifecycle"))
    ai_interaction.set_session_manager(SimpleNamespace(get_session=lambda sid: session if sid == session.id else None))
    ai_interaction.set_memory_manager(None, provider=provider)
    fresh = None
    try:
        first, metrics["phases"]["initial_ms"] = await publish(scope, provider)
        assert first["coverage_complete"]
        assert list(gate.iterdir()) == [], "Non-feature indexing must ignore unreleased fixture gate"
        database = Path(provider._env["FM_DB_PATH"])
        initial = logical_snapshot(database)
        metrics.update(declarations=sum(len(row["declarations"]) for row in initial["files"]),
                       occurrences=sum(len(row["occurrences"]) for row in initial["files"]),
                       resolutions=len(initial["resolutions"]), graph_nodes=len(initial["nodes"]), graph_edges=len(initial["edges"]))
        assert len(initial["receipts"]) == 35 and len(initial["files"]) == 35
        cases = [("query_code", "exact", "query_code", "src/openclank/code_query.py"),
                 ("resolve owned workspace", "identifier", "resolve_owned_workspace", "src/openclank/workspace_policy_service.py"),
                 ("resolve_owned_workspce", "fuzzy", "resolve_owned_workspace", "src/openclank/workspace_policy_service.py"),
                 ("encode_command", "exact", "encode_command", "packages/odysseus-files/src/quicklook_protocol.rs")]
        for text, channel, name, path in cases:
            query = search(text, channel, path)
            begin = time.perf_counter()
            block = function_call_to_tool_block("search_code", json.dumps(query))
            description, reply = await execute_tool_block(block, owner="alice", session_id=session.id,
                workspace=str(root), authority_workspace_id=session.workspace_id)
            assert description.startswith("registry: search_code"), description
            result = json.loads(reply["output"])
            hit = declaration(result, name, path)
            metrics.setdefault("queries", []).append(dict(query=text, channel=channel, ms=round((time.perf_counter()-begin)*1000,3),
                hits=len(result["hits"]), cost=result["cost"]))
            if name == "query_code":
                selected, selected_result = hit, result
        assert selected["source"]["span"]["start_line"] == oracle.lineno
        unfiltered_start=time.perf_counter()
        unfiltered=await ask(scope,provider,search("query_code"))
        declaration(unfiltered,"query_code","src/openclank/code_query.py")
        metrics["unfiltered_exact"]=dict(ms=round((time.perf_counter()-unfiltered_start)*1000,3),cost=unfiltered["cost"])
        for text, channel in (("resolve owned workspace", "identifier"), ("resolve_owned_workspce", "fuzzy")):
            begin = time.perf_counter()
            result = await ask(scope, provider, search(text, channel))
            declaration(result, "resolve_owned_workspace", "src/openclank/workspace_policy_service.py")
            metrics["unfiltered_" + channel] = dict(ms=round((time.perf_counter()-begin)*1000,3), cost=result["cost"])
        app = mounted(scope, provider)
        graph = await post(app, session.workspace_id, search("query_code", path="src/openclank/code_query.py"))
        assert graph["receipt"] == selected_result["receipt"]
        assert {row["id"] for row in graph["hits"]} == {row["id"] for row in selected_result["hits"]}
        assert graph["domain"] == "code"
        semantic = await ask(scope, provider, dict(operation="search", query="query_code", channels=["semantic"]))
        assert semantic["error"] is None and not semantic["hits"]
        assert any(row["channel"] == "semantic" and not row["available"] for row in semantic["capabilities"])
        execution = next(row for row in semantic["channels"] if row["channel"] == "semantic")
        assert execution["requested"] and not execution["enabled"] and not execution["executed"]
        nohit_start=time.perf_counter()
        nohit=await ask(scope,provider,search("s11_absent_unique_9f71"))
        assert nohit["error"] is None and not nohit["hits"]
        metrics["unfiltered_no_hit"]=dict(ms=round((time.perf_counter()-nohit_start)*1000,3),cost=nohit["cost"])
        tiny=await ask(scope,provider,dict(operation="catalog",budgets=dict(work=1)))
        assert tiny["error"]["code"]=="budget_exceeded"
        denied = await ask(scope, provider, search("query_code"), owner="bob")
        assert denied["error"]["code"] == "denied" and not denied["hits"]
        request = dict(workspace_id=session.workspace_id, operation="resolve", excerpt_bytes=4096,
                       reference=dict(kind="code", id=selected["id"], receipt=graph["receipt"]))
        async def source():
            return await resolve_resource(repository=scope[0], provider=provider, auth_manager=lifecycle.Auth(), owner="alice", body=request)
        current = await source()
        assert current["state"] == "current" and current["current_source_verified"]
        assert "async def query_code" in current["excerpt"] and str(root) not in json.dumps(current)
        from src.openclank.files_host_provider import HostFilesProvider
        from src.openclank.filesystem_registry import FilesystemRootRegistry
        from src.openclank.files_facade import FilesFacade, ProviderContext
        context = ProviderContext(owner_subject_id=scope[1]["alice"].owner_subject_id, owner_username="alice",
            policy_generation=scope[0].generation(), workspace_id=session.workspace_id)
        facade = FilesFacade([HostFilesProvider(registry=FilesystemRootRegistry(repository=scope[0]))])
        destination = await facade.open(context, resource_ref=current["destination"]["resource"]["ref"])
        assert destination["target"]["app"] == "editor"
        payload = await facade.open_payload(context, resource_ref=destination["resource"]["ref"])
        assert "async def query_code" in json.dumps(payload) and str(root) not in json.dumps(payload)
        # Every mutation is in the disposable source copy, never repository.
        edited = root / "src/openclank/code_query.py"
        edited.write_bytes(edited.read_bytes() + b"\n# S11 copy-only body/provenance edit\n")
        assert (await source())["state"] == "stale"
        updated, metrics["phases"]["edit_ms"] = await publish(scope, provider, first["head_generation"])
        updated_query = await ask(scope, provider, search("query_code", path="src/openclank/code_query.py"))
        assert declaration(updated_query, "query_code", "src/openclank/code_query.py")["id"] == selected["id"]
        renamed = root / "src/openclank/code_query_renamed.py"
        edited.rename(renamed)
        moved, metrics["phases"]["rename_ms"] = await publish(scope, provider, updated["head_generation"])
        assert not (await ask(scope, provider, search("query_code", path="src/openclank/code_query.py")))["hits"]
        declaration(await ask(scope, provider, search("query_code", path="src/openclank/code_query_renamed.py")), "query_code", "src/openclank/code_query_renamed.py")
        (root / "src/openclank/operation_journal.py").unlink()
        deleted, metrics["phases"]["delete_ms"] = await publish(scope, provider, moved["head_generation"])
        delta = logical_snapshot(database)
        assert len(delta["files"]) == 34 and all(row["rel_path"] != "src/openclank/operation_journal.py" for row in delta["files"])
        # A completely empty database, no copied graph/files/runs/evidence.
        fresh_env = dict(provider._env, FM_DB_PATH=str(tmp_path / "independent-full.db"))
        fresh = FrankenmemoryProvider(command=provider._command, env=fresh_env)
        await fresh.initialize()
        _, metrics["phases"]["independent_full_ms"] = await publish(scope, fresh)
        independent = logical_snapshot(fresh_env["FM_DB_PATH"])
        assert delta == independent, {key: (len(delta[key]), len(independent[key])) for key in delta if delta[key] != independent[key]}
        await fresh.shutdown(); fresh = None
        command, environment = provider._command, dict(provider._env)
        before_restart = await ask(scope, provider, search("encode_command", path="packages/odysseus-files/src/quicklook_protocol.rs"))
        await provider.shutdown()
        provider = FrankenmemoryProvider(command=command, env=environment)
        await provider.initialize(); ai_interaction.set_memory_manager(None, provider=provider)
        after_restart = await ask(scope, provider, search("encode_command", path="packages/odysseus-files/src/quicklook_protocol.rs"))
        assert after_restart["receipt"] == before_restart["receipt"] and after_restart["hits"] == before_restart["hits"]
        assert logical_snapshot(database) == delta
        await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="remove"))
        assert (await ask(scope, provider, search("encode_command")))["error"]["code"] == "erased"
        assert (await source())["state"] == "erased"
        with pytest.raises(MemoryRequestRejectedError, match="explicit admit required"):
            await provider.code_index_lifecycle(**lifecycle.native_request(scope))
        with sqlite3.connect(database) as db:
            for table in ("code_files", "code_file_evidence", "code_resolutions", "code_index_heads", "graph_nodes", "graph_edges", "graph_cues"):
                assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
        readmitted, metrics["phases"]["readmit_ms"] = await publish(scope, provider, action="admit")
        assert readmitted["admission_epoch"] > deleted["admission_epoch"]
        assert logical_snapshot(database) == delta
        assert list(gate.iterdir()) == [], "Default binary must create no fixture markers"
        assert corpus_manifest() == records, "Original first-party bytes must remain unchanged"
        print("S11_REAL_REPOSITORY " + json.dumps(metrics, sort_keys=True))
    finally:
        ai_interaction.set_session_manager(prior_sessions)
        ai_interaction.set_memory_manager(prior_memory[0], vector=prior_memory[1], provider=prior_memory[2], lifecycle=prior_memory[3])
        close_all_clients()
        if fresh is not None:
            await fresh.shutdown()
        await provider.shutdown()
