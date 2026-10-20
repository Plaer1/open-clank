"""Real retained-context helper; controlled C04 responses, no app/child/model.

Canonical source hashes and SHA256 observed-window hashes are deliberately
different. These fixtures qualify retention/eligibility, not source acquisition.
"""
import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from src.openclank import code_context as cc


TEXT = "def target():\n    return 'café'\n"
RECEIPT = dict(workspace_id="workspace-a", registration_id="registration-a",
    source_snapshot_id="snapshot-a", checkout_id="checkout-a", head_generation="head-a",
    admission_epoch=1, policy_generation=2, config_fingerprint="config-a")


def setup_context(monkeypatch, *, held=None):
    calls, entered, release = [], asyncio.Event(), asyncio.Event()
    source = dict(file_id="f" * 64, rel_path="src/target.py", source_hash="b" * 64,
        span=dict(start_byte=0, end_byte=len(TEXT.encode()), start_line=1, end_line=2))
    raw = dict(rel_path=source["rel_path"], start_byte=0, end_byte=len(TEXT.encode()),
        hash_algorithm="sha256", hash_scope="file", hash=hashlib.sha256(TEXT.encode()).hexdigest(),
        state="observed_file", file_size=len(TEXT.encode()), continuation=None)
    read = dict(schema="open-clank.code-query/v1", operation="read", receipt=copy.deepcopy(RECEIPT),
        capabilities=[], operation_capabilities=[], channels=[], edges=[], paths=[], counts=[], facets={},
        omissions=[], partial=False, degraded=False, reasons=[], next_cursor=None,
        cost=dict(inspected_records=1, serialized_bytes=1024, token_upper_bound=1024,
            metadata_bytes=0, files_bytes_read=len(TEXT.encode()), validation_bytes_read=len(TEXT.encode())),
        error=None, hits=[dict(id="file:" + source["file_id"], kind="raw_window", label="src/target.py",
            evidence_state="observed", matches=[], current_source_verified=True,
            source=source, excerpt=TEXT, raw_source=raw)])
    async def query(**kw):
        operation = kw["query"]["operation"]
        calls.append(kw)
        if operation == held:
            entered.set()
            await release.wait()
        if operation == "search":
            return dict(receipt=copy.deepcopy(RECEIPT), error=None, hits=[dict(id="decl:" + "d" * 64,
                kind="declaration", evidence_state="exact", source=source,
                properties=dict(identity_state="stable"), matches=[dict(channel="exact")])])
        assert operation == "read"
        return copy.deepcopy(read)
    monkeypatch.setattr(cc, "query_code", query)
    prefs = dict(code_context_enabled=True, memory_enabled=True, memory_mode="auto")
    args = dict(messages=[dict(role="user", content="Explain function target")],
        message="Explain function target", session=SimpleNamespace(id="chat-a", owner="alice",
            workspace_id="workspace-a", archived=False, incognito=False),
        provider=object(), repository=object(), auth_manager=object(), owner="alice",
        prefs=prefs, prefs_reader=lambda: prefs, context_length=32000)
    return args, calls, read, entered, release


@pytest.mark.parametrize("held", ["search", "read"])
@pytest.mark.parametrize("flag", ["incognito", "archived", "owner", "workspace_id",
    "code_context_enabled", "memory_enabled", "memory_mode"])
async def test_late_eligibility_change_never_inserts_card(monkeypatch, held, flag):
    args, calls, _, entered, release = setup_context(monkeypatch, held=held)
    task = asyncio.create_task(cc.append_code_context(**args))
    await asyncio.wait_for(entered.wait(), 2)
    if flag in {"incognito", "archived", "owner", "workspace_id"}:
        setattr(args["session"], flag, True if flag in {"incognito", "archived"} else "different")
    else:
        args["prefs"][flag] = "off" if flag == "memory_mode" else False
    release.set()
    assert await asyncio.wait_for(task, 2) is args["messages"]
    assert [c["query"]["operation"] for c in calls] == (["search"] if held == "search" else ["search", "read"])


async def test_fresh_preference_reader_replacement_is_observed(monkeypatch):
    args, _, _, entered, release = setup_context(monkeypatch, held="read")
    current = [dict(args["prefs"])]
    args["prefs_reader"] = lambda: current[0]
    task = asyncio.create_task(cc.append_code_context(**args))
    await asyncio.wait_for(entered.wait(), 2)
    current[0] = dict(code_context_enabled=False)
    release.set()
    assert await asyncio.wait_for(task, 2) is args["messages"]


def retained_manual(read, shape):
    payload = copy.deepcopy(read)
    # Actual allowed excerpt-only selection omits canonical source/properties.
    payload["hits"][0].pop("source")
    assert "properties" not in payload["hits"][0]
    if shape == "json":
        content = json.dumps(payload, ensure_ascii=False)
    else:
        content = [dict(type="tool-result", toolCallId="manual-a", toolName="search_code",
            output=dict(type="json", value=dict(output=json.dumps(payload, ensure_ascii=False), exit_code=0)))]
    return dict(role="tool", content=content)


@pytest.mark.parametrize("shape", ["json", "sdk"])
async def test_retained_excerpt_only_same_file_window_dedups(monkeypatch, shape):
    args, calls, read, _, _ = setup_context(monkeypatch)
    manual = retained_manual(read, shape)
    args["messages"].insert(0, manual)
    assert read["hits"][0]["source"]["source_hash"] != read["hits"][0]["raw_source"]["hash"]
    assert await cc.append_code_context(**args) is args["messages"]
    assert [c["query"]["operation"] for c in calls] == ["search", "read"]
    assert calls[1]["query"]["expected_receipt"] == RECEIPT


@pytest.mark.parametrize("different", ["file", "checkout", "workspace", "role", "unverified", "range", "text"])
async def test_unrelated_or_unverified_manual_window_does_not_suppress(monkeypatch, different):
    args, _, read, _, _ = setup_context(monkeypatch)
    manual = copy.deepcopy(read)
    if different == "file":
        manual["hits"][0]["id"] = "file:" + "e" * 64
    elif different in {"checkout", "workspace"}:
        manual["receipt"][different + "_id"] = "different"
    elif different == "unverified":
        manual["hits"][0]["current_source_verified"] = False
    elif different == "range":
        manual["hits"][0]["raw_source"]["end_byte"] -= 1
    elif different == "text":
        manual["hits"][0]["excerpt"] = TEXT.replace("target", "targex")
    message = retained_manual(manual, "sdk")
    if different == "role":
        message["role"] = "user"
    args["messages"].insert(0, message)
    result = await cc.append_code_context(**args)
    assert len(result) == len(args["messages"]) + 1
    assert sum(cc.SENTINEL in str(m["content"]) for m in result) == 1


async def test_retention_reset_releases_suppression_without_history_cache(monkeypatch):
    args, _, read, _, _ = setup_context(monkeypatch)
    retained = [retained_manual(read, "json"), *args["messages"]]
    assert await cc.append_code_context(**{**args, "messages": retained}) is retained
    result = await cc.append_code_context(**args)
    assert len(result) == 2 and cc.SENTINEL in result[0]["content"]
    assert result[-1] == args["messages"][-1]
    assert args["messages"] == [dict(role="user", content="Explain function target")]


@pytest.mark.parametrize("different", [None, "file", "checkout", "workspace", "role", "currentness"])
async def test_full_source_retained_result_requires_same_typed_scope(monkeypatch, different):
    args, _, read, _, _ = setup_context(monkeypatch)
    manual = copy.deepcopy(read)
    if different == "file":
        manual["hits"][0]["id"] = "file:" + "e" * 64
        manual["hits"][0]["source"]["file_id"] = "e" * 64
    elif different in {"checkout", "workspace"}:
        manual["receipt"][different + "_id"] = "different"
    elif different == "currentness":
        manual["hits"][0]["current_source_verified"] = False
    # Keep identical path/text/canonical hash: a flattened substring fallback
    # must not override typed identity, receipt, role or currentness checks.
    args["messages"].insert(0, dict(role="user" if different == "role" else "tool",
        content=json.dumps(manual, ensure_ascii=False)))
    result = await cc.append_code_context(**args)
    if different is None:
        assert result is args["messages"]
    else:
        assert len(result) == len(args["messages"]) + 1
        assert sum(cc.SENTINEL in str(m["content"]) for m in result) == 1
