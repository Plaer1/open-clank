"""SLICE-04 — Brain UI signal surface (T2/T5/T7) contract tests.

static/js/util/memoryTrust.js is the presentation-side mirror of
src/memory_trust.py. The parity test runs the SAME record/prefs matrix
through both implementations — any drift between what the Brain shows
as "trusted" and what injection actually trusts is a bug.
"""
import itertools
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.memory_trust import trusted

_REPO = Path(__file__).resolve().parent.parent
_HELPER = _REPO / "static" / "js" / "util" / "memoryTrust.js"
_HTTP_ERROR_HELPER = _REPO / "static" / "js" / "util" / "httpError.js"
_HAS_NODE = shutil.which("node") is not None

needs_node = pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")


def _node(js: str) -> str:
    proc = subprocess.run(
        ["node", "--input-type=module"], input=js,
        capture_output=True, text=True, cwd=str(_REPO), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _matrix():
    cases = []
    for source_type, pinned, kind, master, kind_on in itertools.product(
        ["human", "ai", "auto_extracted", "procedural"],
        [False, True],
        ["instruction", "persona", "fact", "wiki", "raw", "unknown", "mystery"],
        [False, True],
        [False, True],
    ):
        record = {"source_type": source_type, "pinned": pinned, "kind": kind}
        prefs = {
            "memory_trust_auto": master,
            "memory_trust_auto_kinds": {kind: kind_on},
        }
        cases.append((record, prefs))
    cases.append(({}, {}))  # degraded entry fails closed
    return cases


@needs_node
def test_js_classifier_matches_python_exactly():
    cases = _matrix()
    payload = json.dumps([{"record": r, "prefs": p} for r, p in cases])
    js = f"""
    import {{ isTrusted }} from '{_HELPER.as_posix()}';
    const cases = {payload};
    console.log(JSON.stringify(cases.map(c => isTrusted(c.record, c.prefs))));
    """
    js_results = json.loads(_node(js))
    py_results = [trusted(record, prefs) for record, prefs in cases]
    assert js_results == py_results, (
        "static/js/util/memoryTrust.js drifted from src/memory_trust.py"
    )


@needs_node
def test_score_buckets_and_hover_raw():
    js = f"""
    import {{ scoreBucket, memoryChips }} from '{_HELPER.as_posix()}';
    const buckets = [0, 0.33, 0.34, 0.66, 0.67, 1].map(scoreBucket);
    const chips = memoryChips({{
      source_type: 'auto_extracted', kind: 'fact', category: 'fact',
      trust_score: 0.912, confidence_score: 0.912, workspace_id: 'global',
    }}, {{}});
    console.log(JSON.stringify({{ buckets, chips }}));
    """
    data = json.loads(_node(js))
    assert data["buckets"] == ["low", "low", "med", "med", "high", "high"]
    trust_chip = next(c for c in data["chips"] if c["label"].startswith("trust:"))
    assert trust_chip["label"] == "trust:unreviewed"
    assert "producer signals cannot set Trust" in trust_chip["title"]
    quality_chip = next(c for c in data["chips"] if c["label"].startswith("quality:"))
    assert quality_chip["label"] == "quality:high"
    assert "0.912" in quality_chip["title"], "raw technical quality rides the hover title (T5)"


@needs_node
def test_owner_trust_assignment_is_the_only_epistemic_score():
    js = f"""
    import {{ memoryChips }} from '{_HELPER.as_posix()}';
    const chips = memoryChips({{
      source_type: 'auto_extracted', kind: 'fact', category: 'fact',
      trust_score: 0.99, confidence_score: 0.21,
      trust: {{ state: 'assigned', value: 0.75, actor_id: 'owner-1', reason_code: 'reviewed' }}
    }}, {{}});
    console.log(JSON.stringify(chips));
    """
    chips = json.loads(_node(js))
    labels = [chip["label"] for chip in chips]
    assert "trust:high" in labels
    assert "quality:low" in labels
    assert not any(label.startswith("T:") or label.startswith("C:") for label in labels)


@needs_node
def test_chip_semantics():
    js = f"""
    import {{ memoryChips }} from '{_HELPER.as_posix()}';
    const trusted = memoryChips({{ source_type: 'human', kind: 'fact', category: 'fact' }}, {{}});
    const reference = memoryChips({{ source_type: 'auto_extracted', kind: 'instruction', category: 'fact',
                                     workspace_id: 'repo-x', exempt_from_decay: true, archived: true }}, {{}});
    console.log(JSON.stringify({{ trusted, reference }}));
    """
    data = json.loads(_node(js))
    assert data["trusted"][0]["label"] == "trusted"
    labels = [c["label"] for c in data["reference"]]
    assert labels[0] == "reference"
    assert "instruction" in labels, "kind chip when kind differs from category"
    assert "auto" in labels, "provenance chip"
    assert "repo-x" in labels, "workspace scope chip"
    assert "archived" in labels and "no-decay" in labels


@needs_node
def test_unknown_kind_chips_read_as_open_question():
    js = f"""
    import {{ memoryChips, isTrusted }} from '{_HELPER.as_posix()}';
    const question = memoryChips({{ source_type: 'human', kind: 'unknown', category: 'unknown' }}, {{}});
    const smuggled = isTrusted({{ source_type: 'ai', kind: 'unknown' }},
                               {{ memory_trust_auto: true, memory_trust_auto_kinds: {{ unknown: true }} }});
    console.log(JSON.stringify({{ question, smuggled }}));
    """
    data = json.loads(_node(js))
    labels = [c["label"] for c in data["question"]]
    assert labels[0] == "trusted", "human-authored question is always trusted"
    assert "open question" in labels, "kind chip reads as a question, not 'unknown'"
    assert "unknown" not in labels
    assert data["smuggled"] is False, "non-human unknown can never auto-trust"


@needs_node
def test_memory_import_error_preserves_backend_details_without_duplicate_prefix():
    js = f"""
    import {{
      contextualErrorMessage,
      errorPayloadMessage,
      responseError,
      responseErrorMessage,
    }} from '{_HTTP_ERROR_HELPER.as_posix()}';

    const nested = errorPayloadMessage({{
      detail: {{
        message: 'Memory route is unavailable',
        details: {{ reason: 'No enabled memory route for owner alice' }},
      }},
    }});
    const validation = errorPayloadMessage({{
      detail: [{{ loc: ['body', 'file'], msg: 'Field required', type: 'missing' }}],
    }});
    const plain = await responseErrorMessage({{
      status: 502,
      text: async () => 'provider connection closed',
    }}, 'Import failed');
    const empty = await responseErrorMessage({{
      status: 503,
      text: async () => '',
    }}, 'Import failed');
    const html = await responseErrorMessage({{
      status: 502,
      headers: {{ get: () => 'text/html; charset=utf-8' }},
      text: async () => '<!doctype html><html><body><h1>Bad Gateway</h1>'
        + '<pre>giant private proxy diagnostic</pre></body></html>',
    }}, 'Import failed');
    const htmlFragment = await responseErrorMessage({{
      status: 504,
      text: async () => '<h1>Gateway Timeout</h1><p>giant diagnostic</p>',
    }}, 'Import failed');
    const duplicate = contextualErrorMessage('Import failed', 'Import failed; import failed');
    const alreadyContextual = contextualErrorMessage(
      'Import failed',
      'Import failed; no enabled memory route',
    );
    const detailed = contextualErrorMessage('Import failed', nested);
    const typed = await responseError({{
      status: 409,
      text: async () => JSON.stringify({{ detail: {{
        code: 'MEMORY_ROUTE_UNCONFIGURED',
        message: 'Choose a Memory model and retry.',
        eligible_routes: [{{ model_route_id: 'route-1' }}],
      }} }}),
    }}, 'Import failed');
    console.log(JSON.stringify({{
      nested, validation, plain, empty, html, htmlFragment, duplicate,
      alreadyContextual, detailed, typed,
    }}));
    """
    data = json.loads(_node(js))
    assert data["nested"] == (
        "Memory route is unavailable; No enabled memory route for owner alice"
    )
    assert data["validation"] == "file: Field required"
    assert data["plain"] == "provider connection closed"
    assert data["empty"] == "Import failed (HTTP 503)"
    assert data["html"] == (
        "Import failed (HTTP 502; server returned an HTML error page)"
    )
    assert data["htmlFragment"] == (
        "Import failed (HTTP 504; server returned an HTML error page)"
    )
    assert data["duplicate"] == "Import failed"
    assert data["alreadyContextual"] == "Import failed; no enabled memory route"
    assert data["detailed"] == (
        "Import failed — Memory route is unavailable; "
        "No enabled memory route for owner alice"
    )
    assert data["typed"]["message"] == "Choose a Memory model and retry."
    assert data["typed"]["problem"]["code"] == "MEMORY_ROUTE_UNCONFIGURED"
    assert data["typed"]["problem"]["eligible_routes"] == [
        {"model_route_id": "route-1"}
    ]


@needs_node
def test_admin_danger_zone_runtime_all_and_brain_navigation():
    source = (_REPO / "static" / "js" / "admin.js").read_text(encoding="utf-8")
    start = source.index("function initDangerZone()")
    end = source.index("\n}\n\n/* ═", start) + 2
    danger_zone = source[start:end]
    script = f"""
    const calls = [], traces = [], confirms = [], prompts = [];
    const makeButton = (kind, id='') => ({{ dataset: kind ? {{ wipeKind: kind }} : {{}}, id,
      listeners: {{}}, disabled: false, innerHTML: 'Delete',
      addEventListener(name, fn) {{ this.listeners[name] = fn; }} }});
    const all = makeButton('__all__');
    const brain = makeButton('', 'adm-open-brain-reset');
    const nodes = {{ 'adm-wipeMsg': {{ textContent: '', className: '' }}, 'tool-memory-btn': {{ click() {{ traces.push('brain-open'); }} }}, [brain.id]: brain }};
    globalThis.el = id => nodes[id];
    globalThis.modalEl = {{ querySelectorAll() {{ return [all]; }} }};
    globalThis.document = {{ querySelector(sel) {{ if (sel.includes('memory-tab')) return {{ click() {{ traces.push('brain-settings'); }} }}; return null; }} }};
    globalThis.settingsModule = {{ close() {{ traces.push('settings-close'); }} }};
    globalThis.uiModule = {{ styledConfirm: async message => {{ prompts.push(message); return confirms.shift(); }} }};
    globalThis.checkedFetch = async (url, init) => {{ calls.push([url, init]); return {{ ok: true, json: async () => ({{ count: 1 }}) }}; }};
    {danger_zone}
    initDangerZone();
    brain.listeners.click();
    await new Promise(resolve => setTimeout(resolve, 0));
    if (traces.join(',') !== 'settings-close,brain-open,brain-settings') throw new Error(traces);
    if (calls.length || prompts.length) throw new Error('navigation triggered a wipe');
    confirms.push(true, true); await all.listeners.click();
    const expected = ['chats','skills','notes','tasks','documents','gallery','calendar'].map(k => '/api/admin/wipe/' + k);
    if (JSON.stringify(calls.map(x => x[0])) !== JSON.stringify(expected)) throw new Error(JSON.stringify(calls));
    if (calls.some(x => x[1].method !== 'DELETE')) throw new Error('non-delete');
    if (prompts.length !== 2 || prompts.some(message => !message.includes('across all accounts') || !message.includes('Memories, graph, and ingest data are excluded; skills are included.'))) throw new Error('ambiguous scope');
    if (all.disabled || all.innerHTML !== 'Delete') throw new Error('button not restored');
    calls.length = 0; confirms.push(false); await all.listeners.click();
    if (calls.length) throw new Error('cancel fetched');
    confirms.push(true, false); await all.listeners.click();
    if (calls.length) throw new Error('second confirmation cancel fetched');
    """
    _node(script)


def test_memory_nuke_ui_uses_two_phase_owner_contract_and_fails_closed():
    source = (_REPO / "static" / "js" / "memory.js").read_text()
    assert "fetch('/api/memory/nuke'" in source
    assert "/api/admin/wipe" not in source
    assert "{ action: 'preview', components }" in source
    assert "action: 'commit'" in source
    assert "operation_id: preview.operation_id" in source
    assert "preview_token: preview.preview_token" in source
    assert "confirmation: preview.confirmation" in source
    assert "responseError(response, fallback)" in source
    assert "preview.status === 'preview'" in source
    assert "preview.complete === false" in source
    assert "result.complete === true && result.status === 'complete'" in source
    assert "result.complete === false && result.status === 'partial'" in source
    assert "if (result.complete === true)" in source
    assert "_setMemoryNukeSelection(false)" in source
    assert "detail.error" in source
