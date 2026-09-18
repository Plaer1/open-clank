"""Acceptance ledger for Python-side outbound model execution.

The hard cut deliberately distinguishes model execution from bounded GET-only
catalogue/health observation. Retired text request shapers remain isolated for
protocol fixture coverage; speech transports are removed entirely.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCOPED_NETWORK_FILES = (
    "src/model_dispatch.py",
    "src/llm_core.py",
    "services/stt/stt_service.py",
    "services/tts/tts_service.py",
    "src/service_health.py",
    "src/model_discovery.py",
    "src/model_context.py",
)


@dataclass(frozen=True)
class NetworkCall:
    path: str
    function: str
    verb: str


class _NetworkVisitor(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.scope: list[str] = []
        self.calls: list[NetworkCall] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute):
            base = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
            method = node.func.attr.lower()
            verb = ""
            clients = {"httpx", "requests", "client", "session"}
            if base in clients and method in {"get", "post", "put", "patch", "delete"}:
                verb = method.upper()
            elif base in clients and method in {"request", "stream"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    verb = first.value.upper()
            elif base in clients and method == "send":
                # The method lives on the request object and is therefore not
                # statically recoverable here.  Keep it visible and unclassified.
                verb = "SEND"
            if verb:
                self.calls.append(
                    NetworkCall(
                        self.path,
                        ".".join(self.scope) or "<module>",
                        verb,
                    )
                )
        self.generic_visit(node)


def _tree(relative: str) -> ast.Module:
    return ast.parse((ROOT / relative).read_text(encoding="utf-8"), relative)


def _network_calls(relative: str) -> list[NetworkCall]:
    visitor = _NetworkVisitor(relative)
    visitor.visit(_tree(relative))
    return visitor.calls


def _function(relative: str, qualified_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    wanted = qualified_name.split(".")
    found = None

    class Find(ast.NodeVisitor):
        def __init__(self):
            self.scope: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            nonlocal found
            self.scope.append(node.name)
            if self.scope == wanted:
                found = node
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

    Find().visit(_tree(relative))
    assert found is not None, f"missing ledger entry point {relative}:{qualified_name}"
    return found


def _called_names(node: ast.AST) -> set[str]:
    result = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Name):
            result.add(child.func.id)
        elif isinstance(child.func, ast.Attribute):
            result.add(child.func.attr)
    return result


def test_scoped_outbound_network_has_an_exhaustive_role_ledger():
    calls = [
        call
        for relative in SCOPED_NETWORK_FILES
        for call in _network_calls(relative)
    ]
    observed = {(call.path, call.function, call.verb) for call in calls}

    # These are observation only: bounded catalogue/context/health GETs.  They
    # cannot submit a prompt, synthesize media, or mutate provider state.
    get_only_observation = {
        ("src/llm_core.py", "list_model_ids", "GET"),
        ("src/service_health.py", "_http_get", "GET"),
        ("src/model_discovery.py", "ModelDiscovery._fingerprint_provider", "GET"),
        ("src/model_discovery.py", "ModelDiscovery._check_port", "GET"),
        ("src/model_context.py", "_proxy_catalog_context", "GET"),
        ("src/model_context.py", "_query_context_length", "GET"),
    }

    # Request shaping/parsing is retained only as inert compatibility code.
    # Public entry points below are separately asserted to have no path here.
    inert_legacy_execution = {
        ("src/llm_core.py", "_legacy_llm_call", "POST"),
        ("src/llm_core.py", "_legacy_llm_call_async", "POST"),
        ("src/llm_core.py", "_legacy_stream_llm_inner", "POST"),
    }

    assert observed == get_only_observation | inert_legacy_execution
    assert not _network_calls("src/model_dispatch.py")


def test_public_compatibility_doors_cannot_reach_legacy_model_execution():
    public_doors = {
        "src/llm_core.py": (
            "llm_call",
            "llm_call_with_fallback",
            "llm_call_async",
            "llm_call_async_with_fallback",
            "stream_llm",
            "stream_llm_with_fallback",
        ),
        "services/stt/stt_service.py": ("STTService.transcribe",),
        "services/tts/tts_service.py": ("TTSService.synthesize",),
    }
    for relative, names in public_doors.items():
        for name in names:
            calls = _called_names(_function(relative, name))
            assert not any(value.startswith("_legacy_") for value in calls), (
                relative,
                name,
                calls,
            )
            assert not calls.intersection({"post", "stream", "_get_http_client"}), (
                relative,
                name,
                calls,
            )

    stream_calls = _called_names(_function("src/model_dispatch.py", "stream_chat_target"))
    auxiliary_calls = _called_names(_function("src/model_dispatch.py", "run_auxiliary_inference"))
    assert "complete_text" in stream_calls
    assert "complete_text" in auxiliary_calls
    assert not {"stream_llm", "stream_llm_with_fallback", "llm_call_async"}.intersection(
        stream_calls | auxiliary_calls
    )


def test_no_shipped_module_imports_or_calls_private_legacy_transports():
    legacy_names = {
        "_legacy_llm_call",
        "_legacy_llm_call_async",
        "_legacy_stream_llm",
        "_legacy_stream_llm_inner",
    }
    definition_files = {
        "src/llm_core.py",
        "services/stt/stt_service.py",
        "services/tts/tts_service.py",
    }
    references: list[tuple[str, int, str]] = []
    roots = ("app.py", "core", "routes", "services", "src", "mcp_servers")
    paths: list[Path] = []
    for raw in roots:
        path = ROOT / raw
        paths.extend([path] if path.is_file() else path.rglob("*.py"))
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), relative)

        class LegacyReferenceVisitor(ast.NodeVisitor):
            def __init__(self):
                self.functions: list[str] = []

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self.functions.append(node.name)
                self.generic_visit(node)
                self.functions.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def _record(self, node: ast.AST, name: str) -> None:
                current = self.functions[-1] if self.functions else ""
                if relative in definition_files and current.startswith("_legacy_"):
                    return
                references.append((relative, getattr(node, "lineno", 0), name))

            def visit_Name(self, node: ast.Name) -> None:
                if node.id in legacy_names:
                    self._record(node, node.id)

            def visit_Attribute(self, node: ast.Attribute) -> None:
                if node.attr in legacy_names:
                    self._record(node, node.attr)
                self.generic_visit(node)

            def visit_alias(self, node: ast.alias) -> None:
                if node.name in legacy_names:
                    self._record(node, node.name)

        LegacyReferenceVisitor().visit(tree)
    assert references == []


@pytest.mark.asyncio
async def test_retired_public_dispatch_fails_before_any_provider_client(monkeypatch, tmp_path):
    import src.llm_core as llm_core
    from services.stt.stt_service import STTService
    from services.tts.tts_service import TTSService
    from src.endpoint_resolver import resolve_model_target
    from src.model_dispatch import call_model_target, stream_chat_target

    def touched(*_args, **_kwargs):
        raise AssertionError("retired provider client was reached")

    monkeypatch.setattr(llm_core.httpx, "post", touched)
    monkeypatch.setattr(llm_core, "_get_http_client", touched)

    with pytest.raises(llm_core.DirectModelDispatchRetired):
        llm_core.llm_call(
            "https://provider.invalid/v1/chat/completions",
            "model",
            [{"role": "user", "content": "hello"}],
        )
    with pytest.raises(llm_core.DirectModelDispatchRetired):
        await llm_core.llm_call_async(
            "https://provider.invalid/v1/chat/completions",
            "model",
            [{"role": "user", "content": "hello"}],
        )
    candidate = (
        "https://provider.invalid/v1/chat/completions",
        "model",
        {},
    )
    with pytest.raises(llm_core.DirectModelDispatchRetired):
        llm_core.llm_call_with_fallback(
            [candidate],
            [{"role": "user", "content": "hello"}],
        )
    with pytest.raises(llm_core.DirectModelDispatchRetired):
        await llm_core.llm_call_async_with_fallback(
            [candidate],
            [{"role": "user", "content": "hello"}],
        )
    legacy_stream = [
        chunk
        async for chunk in llm_core.stream_llm(
            "https://provider.invalid/v1/chat/completions",
            "model",
            [{"role": "user", "content": "hello"}],
        )
    ]
    assert len(legacy_stream) == 2
    assert "LEGACY_PROVIDER_ROUTE_RETIRED" in legacy_stream[0]
    assert legacy_stream[-1] == "data: [DONE]\n\n"
    fallback_stream = [
        chunk
        async for chunk in llm_core.stream_llm_with_fallback(
            [candidate],
            [{"role": "user", "content": "hello"}],
        )
    ]
    assert "LEGACY_PROVIDER_ROUTE_RETIRED" in fallback_stream[0]

    http_target = resolve_model_target(
        "https://provider.invalid/v1/chat/completions",
        "model",
    )
    with pytest.raises(Exception) as rejected:
        await call_model_target(
            http_target,
            [{"role": "user", "content": "hello"}],
            owner="alice",
        )
    assert getattr(rejected.value, "code", "") == "LEGACY_PROVIDER_ROUTE_RETIRED"
    rejected_stream = [
        chunk
        async for chunk in stream_chat_target(
            http_target,
            [{"role": "user", "content": "hello"}],
            session_id="ledger-http",
            owner="alice",
        )
    ]
    assert "LEGACY_PROVIDER_ROUTE_RETIRED" in rejected_stream[0]
    assert rejected_stream[-1] == "data: [DONE]\n\n"

    tts = TTSService(cache_dir=str(tmp_path / "tts"))
    stt = STTService()
    monkeypatch.setattr(tts, "_load_settings", lambda owner=None: {
        "tts_enabled": True,
        "tts_provider": "endpoint:retired",
    })
    monkeypatch.setattr(stt, "_load_settings", lambda owner=None: {
        "stt_enabled": True,
        "stt_provider": "endpoint:retired",
    })
    assert tts.synthesize("hello", use_cache=False, owner="alice") is None
    assert stt.transcribe(b"audio", owner="alice") is None


@pytest.mark.asyncio
async def test_managed_compatibility_dispatch_enters_router_without_provider_authority(monkeypatch):
    from src.endpoint_resolver import ResolvedModelTarget
    from src.model_dispatch import call_model_target, stream_chat_target

    calls = []

    monkeypatch.setattr(
        "src.openclank.chat_routing.resolve_chat_route",
        lambda **_kwargs: SimpleNamespace(
            model_route_id="pmr-ledger",
            provider_grant_id=None,
        ),
    )

    async def complete_text(**kwargs):
        calls.append(kwargs)
        return "managed answer"

    monkeypatch.setattr(
        "src.openclank.modality_facade.complete_text",
        complete_text,
    )
    target = ResolvedModelTarget(
        transport="acp",
        endpoint_url="openclank://engine",
        model_id="pcn-ledger/model-a",
        endpoint_id="pcn-ledger",
        provider_id="pcn-ledger",
        headers={},
        capabilities={"chat": True, "tools": False},
        lifecycle="ephemeral",
    )

    result = await call_model_target(
        target,
        [{"role": "user", "content": "hello"}],
        session_id="ledger-complete",
        owner="alice",
        purpose="research-query",
        root_operation_id="root-ledger",
    )
    stream = [
        chunk
        async for chunk in stream_chat_target(
            target,
            [{"role": "user", "content": "rewrite"}],
            session_id="ledger-stream",
            owner="alice",
            turn_envelope={"root_operation_id": "root-stream"},
            max_tokens=0,
        )
    ]

    assert result == "managed answer"
    assert json.loads(stream[0][6:])["delta"] == "managed answer"
    assert stream[-1] == "data: [DONE]\n\n"
    assert [call["purpose"] for call in calls] == ["research", "chat"]
    assert [call["root_operation_id"] for call in calls] == [
        "root-ledger",
        "root-stream",
    ]
    assert all(call["owner"] == "alice" for call in calls)
    assert all(call["model_route_id"] == "pmr-ledger" for call in calls)
    assert all("url" not in call and "headers" not in call for call in calls)
