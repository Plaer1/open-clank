"""Revision-qualified Python compiler scope for an admitted native call witness."""
from __future__ import annotations

import ast
import asyncio
import os
import platform
import re
import symtable
from pathlib import PurePosixPath

_MAX_FILE_BYTES = 32768
_PROFILE = "python_compiler_scope_v1"
_ID = re.compile(r"[0-9a-f]{64}\Z")
_SCOPES = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
) + tuple(filter(None, (getattr(ast, "TypeAlias", None),)))


def _observation(status, *, reason=None, qualifiers=(), target=None):
    value = {
        "version": 1, "profile": _PROFILE, "status": status,
        "producer": {"name": "CPython symtable", "version": platform.python_version()},
        "qualifiers": list(qualifiers),
    }
    if reason is not None:
        value["reason"] = reason
    if target is not None:
        value["target"] = target
    return value


def _function_at(module, span):
    functions = [
        node for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (node.lineno, node.col_offset) == (span.get("start_line"), span.get("start_column"))
    ]
    return functions[0] if len(functions) == 1 else None


def _scope_for(module, function):
    if function is None:
        return module
    candidates = [
        table for table in module.get_children()
        if table.get_name() == function.name and table.get_lineno() == function.lineno
        and table.get_type() == "function"
    ]
    return candidates[0] if len(candidates) == 1 else None


def _call_at(statements, span):
    pending = list(statements)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func
            if (name.lineno, name.col_offset, name.end_lineno, name.end_col_offset) == (
                span.get("start_line"), span.get("start_column"),
                span.get("end_line"), span.get("end_column"),
            ):
                return name.id
        if not isinstance(node, _SCOPES):
            pending.extend(ast.iter_child_nodes(node))
    return None


def _module_function(table, function):
    symbol = table.lookup(function.name)
    bindings = symbol.get_namespaces()
    return (not symbol.is_imported() and len(bindings) == 1
            and bindings[0].get_type() == "function" and bindings[0].get_lineno() == function.lineno)


def _global_call_name(tree, table, source, caller):
    function = _function_at(tree, caller["span"]) if caller is not None else None
    if caller is not None and function is None:
        return None
    name = _call_at(function.body if function is not None else tree.body, source["span"])
    scope = _scope_for(table, function)
    if name is None or scope is None:
        return None
    symbol = scope.lookup(name)
    return name if symbol.is_global() and (caller is None or not (symbol.is_assigned() or symbol.is_imported())) else None


def _explicit_import(tree, name):
    imports = [
        (statement, alias) for statement in tree.body if isinstance(statement, ast.ImportFrom)
        for alias in statement.names if (alias.asname or alias.name) == name
    ]
    return imports[0] if len(imports) == 1 else (None, None)


def _qualified_import_site(text, source, caller):
    tree = ast.parse(text, filename="<graph-source>")
    table = symtable.symtable(text, "<graph-source>", "exec")
    name = _global_call_name(tree, table, source, caller)
    if name is None or not table.lookup(name).is_imported():
        return None
    statement, alias = _explicit_import(tree, name)
    if statement is None or statement.level != 0 or not isinstance(statement.module, str):
        return None
    parts = statement.module.split(".")
    if (not 1 <= len(parts) <= 4
            or any(not part.isascii() or not part.isidentifier() for part in parts)
            or not alias.name.isascii() or not alias.name.isidentifier()):
        return None
    return statement.module, alias.name


def _qualify(text, source, caller, target_text, target, *, expected_module=None):
    # The compiler parses symbol scopes without importing or executing code.
    source_tree = ast.parse(text, filename="<graph-source>")
    source_table = symtable.symtable(text, "<graph-source>", "exec")
    same_file = all(source[key] == target[key] for key in ("file_id", "rel_path", "source_hash"))
    target_tree = source_tree if same_file else ast.parse(target_text, filename="<graph-target>")
    target_table = source_table if target_tree is source_tree else symtable.symtable(
        target_text, "<graph-target>", "exec",
    )
    target_function = _function_at(target_tree, target["span"])
    name = _global_call_name(source_tree, source_table, source, caller)
    if target_function is None or name is None or not _module_function(target_table, target_function):
        return None
    if source_tree is target_tree:
        return ("global_callable_namespace",) if name == target_function.name and not source_table.lookup(name).is_imported() else None
    # The native resolver supplies the candidate module. The compiler proves a
    # lexical imported name and a target function namespace, not loader behavior.
    root_symbol = source_table.lookup(name)
    statement, alias = _explicit_import(source_tree, name)
    if not root_symbol.is_imported() or statement is None or alias.name != target_function.name:
        return None
    qualifiers = ("global_import_binding", "target_function_namespace")
    if expected_module is not None:
        if statement.level != 0 or statement.module != expected_module:
            return None
        qualifiers += ("selected_workspace_module_path",)
    return qualifiers


def _account(cost, response):
    observed = response.get("cost") if isinstance(response, dict) else None
    if isinstance(observed, dict):
        for key in ("native_rpc_calls_completed", "inspected_records", "validation_bytes_read"):
            added = observed.get(key)
            if type(added) is int and added >= 0:
                cost[key] = cost.get(key, 0) + added


async def _acquire_compiler_claim(client, binding, locator, cost):
    from src.openclank.code_query import _relative, _span

    if not isinstance(locator.get("file_id"), str) or not _ID.fullmatch(locator["file_id"]):
        raise ValueError("missing canonical source")
    rel = _relative(locator.get("rel_path"))
    if not rel.endswith(".py"):
        raise ValueError("unsupported source language")
    path = os.path.join(binding["root"], *PurePosixPath(rel).parts)
    opened = await client.open_handle(path)
    handle, size = opened.get("handle"), opened.get("size")
    if not isinstance(handle, dict) or type(size) is not int or not 0 < size <= _MAX_FILE_BYTES:
        raise ValueError("outside compiler acquisition profile")
    response = await client.request_handle("read_range", handle, {
        "offset": 0, "length": size, "include_fingerprint": False,
    })
    data = response.get("data") or {}
    raw = data.get("bytes")
    if (not isinstance(raw, list) or len(raw) != size or data.get("offset") != 0
            or any(type(byte) is not int or not 0 <= byte <= 255 for byte in raw)):
        raise ValueError("incomplete compiler input")
    body = bytes(raw)
    cost["files_bytes_read"] = cost.get("files_bytes_read", 0) + len(body)
    claim = {"source": {**locator, "span": _span(body, 0, len(body))},
             "predicate": {"kind": "exact_text", "text": body.decode("utf-8")}}
    return claim

async def _verify_compiler_claim(provider, binding, query, claim, cost):
    response = await provider.code_query(binding=binding, query=query, source_claim=claim)
    _account(cost, response)
    if (not isinstance(response, dict) or response.get("receipt") != query["expected_receipt"]
            or response.get("error") is not None
            or (response.get("source_validation") or {}).get("state") != "current"):
        raise ValueError("compiler snapshot is no longer current")

async def enrich_compiler_scope(*, result, query, binding, repository, provider, auth_manager, session, purpose):
    """Best-effort enrichment only after the canonical native inspect result."""
    from src.openclank.code_query import (
        SCHEMA, CodeQueryError, _files_client, _source_guard, measure_envelope,
    )

    if (query["operation"] != "inspect" or query.get("cursor") is not None
            or query.get("expected_receipt") is None
            or not {"source", "properties", "resolution"} <= set(query["fields"])
            or len(query["seed_ids"]) != 1 or not query["seed_ids"][0].startswith("occ:")
            or result.get("receipt") != query["expected_receipt"] or result.get("error") is not None
            or platform.python_implementation() != "CPython"):
        return
    hits = result.get("hits", [])
    if len(hits) != 1:
        return
    hit = hits[0]
    props, source, resolution = hit.get("properties"), hit.get("source"), hit.get("resolution")
    if (hit.get("id") != query["seed_ids"][0] or hit.get("kind") != "occurrence"
            or not isinstance(props, dict) or props.get("language") != "python"
            or props.get("occurrence_kind") != "call" or not isinstance(source, dict)
            or not isinstance(resolution, dict)):
        return

    def publish(observation):
        props["compilerObservation"] = observation
        try:
            measure_envelope(result, query["budgets"])
        except CodeQueryError:
            props.pop("compilerObservation", None)

    def omit(status, reason):
        publish(_observation(status, reason=reason))

    target_id, caller_id = resolution.get("target_id"), resolution.get("caller_id")
    if (not isinstance(target_id, str) or not _ID.fullmatch(target_id)
            or resolution.get("state") not in ("exact", "inferred")
            or resolution.get("candidate_ids") != [target_id]):
        omit("unsupported", "no_unique_native_binding")
        return
    if caller_id is not None and (not isinstance(caller_id, str) or not _ID.fullmatch(caller_id)):
        omit("unsupported", "caller_identity_unavailable")
        return

    cost = result.setdefault("cost", {})
    expected = query["expected_receipt"]
    fence = None

    async def guard():
        nonlocal fence
        fence = await _source_guard(
            binding=binding, repository=repository, provider=provider, auth_manager=auth_manager,
            session=session, purpose=purpose, expected=expected, captured_fence=fence, cost=cost,
        )

    def bounded_query(**fields):
        remaining = query["budgets"]["work"] - cost.get("inspected_records", 0)
        if remaining <= 0:
            raise ValueError("compiler query work budget exhausted")
        return {**receipt_query, "budgets": {**query["budgets"], "work": remaining}, **fields}

    receipt_query = {"schema": SCHEMA, "operation": "capabilities", "fields": [], "channels": [],
                     "expected_receipt": expected, "budgets": query["budgets"]}
    target_key = "decl:" + target_id
    caller_key = "decl:" + caller_id if caller_id is not None else None
    endpoint_ids = sorted({target_key, caller_key} - {None})
    try:
        await guard()
        endpoints = await provider.code_query(binding=binding, query=bounded_query(
            operation="inspect", seed_ids=endpoint_ids, fields=["source"],
        ))
        _account(cost, endpoints)
        if (not isinstance(endpoints, dict) or endpoints.get("receipt") != expected
                or endpoints.get("error") is not None):
            raise ValueError("compiler endpoint receipt unavailable")
        locators = {
            entry["id"]: entry["source"] for entry in endpoints.get("hits", [])
            if entry.get("kind") == "declaration" and isinstance(entry.get("source"), dict)
        }
        target = locators[target_key]
        caller = locators[caller_key] if caller_key is not None else None
        if caller is not None and (caller.get("rel_path"), caller.get("source_hash"), caller.get("file_id")) != (
            source.get("rel_path"), source.get("source_hash"), source.get("file_id"),
        ):
            raise ValueError("caller is outside the source revision")
        client = _files_client(binding=binding, repository=repository, auth_manager=auth_manager, session=session, purpose=purpose)
        await guard()
        source_input = await _acquire_compiler_claim(client, binding, source, cost)
        await guard()
        target_input = await _acquire_compiler_claim(client, binding, target, cost)
        qualifiers = await asyncio.to_thread(
            _qualify, source_input["predicate"]["text"], source, caller,
            target_input["predicate"]["text"], target,
        )
        if qualifiers is None:
            omit("unsupported", "scope_outside_compiler_profile")
            return
        await _verify_compiler_claim(provider, binding, bounded_query(), source_input, cost)
        await _verify_compiler_claim(provider, binding, bounded_query(), target_input, cost)
        await guard()
        publish(_observation("qualified", qualifiers=qualifiers, target={"id": target_key, "source": target}))
    except asyncio.CancelledError:
        raise
    except Exception:
        # Files/provider-specific rejection details remain inside their owners.
        omit("unavailable", "compiler_snapshot_unavailable")


_CROSS_SCHEMA = "open-clank.code-cross-project/v2"


def unavailable_cross_workspace():
    return {
        "schema": _CROSS_SCHEMA, "state": "unavailable", "partial": True,
        "source": None, "target": None, "relation": None,
        "reasons": ["authority_or_source_unavailable"],
    }


def _python_module_path(rel):
    parts = PurePosixPath(rel).parts
    if not 1 <= len(parts) <= 5 or not parts[-1].endswith(".py"):
        return None
    names = (*parts[:-1], parts[-1][:-3])
    if names[-1] == "__init__":
        names = names[:-1]
    if not names or any(not name.isascii() or not name.isidentifier() for name in names):
        return None
    return ".".join(names)


async def cross_workspace_compiler_import(*, owner, repository, auth_manager, provider, source, target, current):
    """Compare two selected current workspaces using independent Files fences."""
    from src.openclank.code_query import (
        SCHEMA, MAX_QUERY_WORK, _files_client, _source_guard,
        resolve_query_binding, validate_query,
    )

    def select(value, field, prefix, *, discover=False):
        if not isinstance(value, dict) or (set(value) != {"workspace_id", field, "receipt"}
                and not (discover and set(value) == {"workspace_id", "receipt"})):
            raise ValueError("invalid endpoint")
        workspace, identity = value["workspace_id"], value.get(field)
        if (not isinstance(workspace, str) or not 1 <= len(workspace.encode()) <= 256
                or (field in value and (not isinstance(identity, str)
                    or not identity.startswith(prefix) or not _ID.fullmatch(identity[len(prefix):])))):
            raise ValueError("invalid endpoint")
        receipt = value["receipt"]
        validate_query({"operation": "capabilities", "fields": [], "channels": [], "expected_receipt": receipt})
        if not isinstance(receipt, dict) or receipt.get("workspace_id") != workspace:
            raise ValueError("invalid endpoint receipt")
        return workspace, identity, receipt

    def bind(workspace):
        return resolve_query_binding(
            repository=repository, auth_manager=auth_manager, owner=owner,
            workspace_id=workspace, purpose="app_folder",
        )

    source_workspace, source_id, source_receipt = select(source, "occurrence_id", "occ:")
    target_workspace, target_id, target_receipt = select(target, "declaration_id", "decl:", discover=True)
    if (source_workspace == target_workspace or platform.python_implementation() != "CPython"
            or not current()):
        return unavailable_cross_workspace()
    source_binding, source_identity = bind(source_workspace)
    target_binding, target_identity = bind(target_workspace)
    roots = (source_binding["root"], target_binding["root"])
    if os.path.commonpath(roots) in roots:
        return unavailable_cross_workspace()

    def live():
        return (current() and bind(source_workspace)[1] == source_identity
                and bind(target_workspace)[1] == target_identity)

    budgets = {"rows": 1, "bytes": 12288, "tokens": 8192, "hops": 0, "work": MAX_QUERY_WORK}
    costs = [{}, {}]
    fences = [None, None]
    bindings = (source_binding, target_binding)
    receipts = (source_receipt, target_receipt)

    async def guard(index):
        if not live():
            raise ValueError("workspace authority changed")
        fences[index] = await _source_guard(
            binding=bindings[index], repository=repository, provider=provider, auth_manager=auth_manager,
            session=None, purpose="app_folder", expected=receipts[index],
            captured_fence=fences[index], cost=costs[index],
        )
        if not live():
            raise ValueError("workspace authority changed")

    def query(index, operation, **fields):
        return {"schema": SCHEMA, "operation": operation, "expected_receipt": receipts[index],
                "channels": [], "fields": [], "budgets": budgets, **fields}

    def no_match(reason="qualified_import_not_witnessed"):
        return {"schema": _CROSS_SCHEMA, "state": "no_match", "partial": True,
                "source": None, "target": None, "relation": None, "reasons": [reason]}

    async def discover_endpoint(module, symbol):
        base = module.replace(".", "/")
        possible = []
        for rel in (base + ".py", base + "/__init__.py"):
            await guard(1)
            response = await provider.code_query(binding=target_binding, query=query(
                1, "search", query=symbol, channels=["exact"], fields=["source", "label"],
                filter={"rel_path": rel, "symbol_kinds": ["function"]},
                budgets={**budgets, "rows": 8},
            ))
            if (not live() or not isinstance(response, dict) or response.get("receipt") != target_receipt
                    or response.get("error") is not None):
                raise ValueError("target search receipt unavailable")
            if response.get("next_cursor") is not None:
                return None
            possible.extend(hit for hit in response.get("hits", [])
                            if hit.get("kind") == "declaration" and (hit.get("source") or {}).get("rel_path") == rel
                            and hit.get("label") == symbol)
        return possible[0] if len(possible) == 1 else None

    async def inspect(index, identity, fields):
        await guard(index)
        response = await provider.code_query(binding=bindings[index], query=query(
            index, "inspect", seed_ids=[identity], fields=fields,
        ))
        if (not live() or not isinstance(response, dict) or response.get("receipt") != receipts[index]
                or response.get("error") is not None or len(response.get("hits", [])) != 1):
            raise ValueError("endpoint receipt unavailable")
        hit = response["hits"][0]
        if hit.get("id") != identity or not isinstance(hit.get("source"), dict):
            raise ValueError("endpoint locator unavailable")
        return hit

    source_hit = await inspect(0, source_id, ["source", "properties", "resolution"])
    properties, resolution = source_hit.get("properties"), source_hit.get("resolution")
    if (source_hit.get("kind") != "occurrence" or not isinstance(properties, dict)
            or properties.get("language") != "python" or properties.get("occurrence_kind") != "call"
            or not isinstance(resolution, dict) or resolution.get("target_id") is not None
            or resolution.get("candidate_ids") != []
            or resolution.get("state") not in ("unresolved", "unsupported")):
        return unavailable_cross_workspace()
    caller_id, source_locator = resolution.get("caller_id"), source_hit["source"]
    caller = None
    if caller_id is not None:
        if not isinstance(caller_id, str) or not _ID.fullmatch(caller_id):
            return unavailable_cross_workspace()
        caller = (await inspect(0, "decl:" + caller_id, ["source"]))["source"]
        if any(caller.get(key) != source_locator.get(key) for key in ("file_id", "rel_path", "source_hash")):
            return unavailable_cross_workspace()
    async def acquire(index, locator):
        await guard(index)
        client = _files_client(
            binding=bindings[index], repository=repository, auth_manager=auth_manager,
            session=None, purpose="app_folder",
        )
        return await _acquire_compiler_claim(client, bindings[index], locator, costs[index])

    source_input = await acquire(0, source_locator)
    await _verify_compiler_claim(provider, source_binding, query(0, "capabilities"), source_input, costs[0])
    site = await asyncio.to_thread(_qualified_import_site, source_input["predicate"]["text"], source_locator, caller)
    if site is None:
        await guard(0)
        await guard(1)
        return no_match()
    module, symbol = site
    target_hit = (await discover_endpoint(module, symbol)) if target_id is None else await inspect(1, target_id, ["source"])
    if target_hit is None:
        await guard(0)
        await guard(1)
        return no_match("unique_selected_workspace_definition_unavailable")
    if target_hit.get("kind") != "declaration" or not isinstance(target_hit.get("source"), dict):
        return unavailable_cross_workspace()
    target_id, target_locator = target_hit.get("id"), target_hit["source"]
    if (not isinstance(target_id, str) or not target_id.startswith("decl:")
            or not _ID.fullmatch(target_id[5:]) or _python_module_path(target_locator["rel_path"]) != module):
        await guard(0)
        await guard(1)
        return no_match()
    target_input = await acquire(1, target_locator)
    qualifiers = await asyncio.to_thread(
        _qualify, source_input["predicate"]["text"], source_locator, caller,
        target_input["predicate"]["text"], target_locator, expected_module=module,
    )
    for index, claim in enumerate((source_input, target_input)):
        await guard(index)
        await _verify_compiler_claim(provider, bindings[index], query(index, "capabilities"), claim, costs[index])
    await guard(0)
    await guard(1)
    if not live():
        return unavailable_cross_workspace()
    if qualifiers is None:
        return no_match()
    return {
        "schema": _CROSS_SCHEMA, "state": "match", "partial": True,
        "source": {"workspace_id": source_workspace, "id": source_id,
                   "receipt": source_receipt, "source": source_locator},
        "target": {"workspace_id": target_workspace, "id": target_id,
                   "receipt": target_receipt, "source": target_locator},
        "relation": {"kind": "compiler_qualified_import", "semantics": "selected_workspace_lexical_candidate",
                     "observation": _observation("qualified", qualifiers=qualifiers)},
        "reasons": ["python_loader_package_search_and_runtime_unproved"],
    }
