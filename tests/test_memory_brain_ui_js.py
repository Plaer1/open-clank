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
      trust_score: 0.912, workspace_id: 'global',
    }}, {{}});
    console.log(JSON.stringify({{ buckets, chips }}));
    """
    data = json.loads(_node(js))
    assert data["buckets"] == ["low", "low", "med", "med", "high", "high"]
    score_chip = next(c for c in data["chips"] if c["label"].startswith("T:"))
    assert score_chip["label"] == "T:high"
    assert "0.912" in score_chip["title"], "raw float rides the hover title (T5)"


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


def test_brain_markup_carries_trust_panel_and_filters():
    html = (_REPO / "static" / "index.html").read_text()
    assert 'id="memory-trust-auto-toggle"' in html
    assert 'id="memory-trust-kinds"' in html
    assert 'id="memory-filter-kind"' in html
    assert 'id="memory-filter-provenance"' in html
    assert 'id="memory-filter-trust"' in html
    # Filters live INSIDE toolbar row 1 — a fourth toolbar row blows the
    # .memory-toolbar 120px cap and overlaps the list (e's screenshot).
    row_start = html.index('class="memory-toolbar-row"')
    search_at = html.index('id="memory-search"')
    filters_at = html.index('id="memory-signal-filters"')
    assert row_start < filters_at < search_at
    assert 'id="memory-digest-stamp"' in html


def test_memory_js_wires_prefs_and_chips():
    source = (_REPO / "static" / "js" / "memory.js").read_text()
    assert "memory_trust_auto" in source
    assert "memory_trust_auto_kinds" in source
    assert "memoryChips(" in source
    assert "_buildMemoryDetails" in source
    assert "_passesSignalFilters" in source


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


def test_memory_js_wires_question_lifecycle():
    source = (_REPO / "static" / "js" / "memory.js").read_text()
    assert "'unknown'" in source and "open question" in source
    assert "resolveQuestion" in source
    assert "/resolve" in source
