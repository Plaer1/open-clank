"""SLICE-05 — Brain graph canvas (T4) contract tests.

static/js/util/memoryGraph.js is the self-contained force layout (no
CDN, no DOM). Node tests pin: deterministic settle, on-screen bounds,
hit testing, tag histograms, and duplicate-free expansion merging.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HELPER = _REPO / "static" / "js" / "util" / "memoryGraph.js"
_HAS_NODE = shutil.which("node") is not None

needs_node = pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")

GRAPH = {
    "nodes": [
        {"id": "n:person:e", "kind": "person", "name": "e"},
        {"id": "n:project:open-clank", "kind": "project", "name": "open-clank"},
        {"id": "n:tool:frankenmemory", "kind": "tool", "name": "frankenmemory"},
    ],
    "edges": [
        {"id": "e1", "src_id": "n:person:e", "dst_id": "n:project:open-clank", "tag": "works_on"},
        {"id": "e2", "src_id": "n:project:open-clank", "dst_id": "n:tool:frankenmemory", "tag": "uses"},
        {"id": "e3", "src_id": "n:person:e", "dst_id": "n:tool:frankenmemory", "tag": "uses"},
    ],
}


def _node(js: str) -> str:
    proc = subprocess.run(
        ["node", "--input-type=module"], input=js,
        capture_output=True, text=True, cwd=str(_REPO), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@needs_node
def test_layout_is_deterministic_and_in_bounds():
    js = f"""
    import {{ forceLayout }} from '{_HELPER.as_posix()}';
    const graph = {json.dumps(GRAPH)};
    const run = () => [...forceLayout(graph.nodes, graph.edges, {{width: 640, height: 420}}).entries()];
    console.log(JSON.stringify({{ a: run(), b: run() }}));
    """
    data = json.loads(_node(js))
    assert data["a"] == data["b"], "no randomness — same graph, same picture"
    positions = dict(data["a"])
    assert len(positions) == 3
    for point in positions.values():
        assert 0 <= point["x"] <= 640 and 0 <= point["y"] <= 420
    # Nodes must actually spread out, not collapse onto one point.
    coords = list(positions.values())
    spread = max(
        abs(coords[i]["x"] - coords[j]["x"]) + abs(coords[i]["y"] - coords[j]["y"])
        for i in range(3) for j in range(i + 1, 3)
    )
    assert spread > 50


@needs_node
def test_hit_test_tags_and_merge():
    js = f"""
    import {{ forceLayout, hitTest, tagCounts, mergeExpansion }} from '{_HELPER.as_posix()}';
    const graph = {json.dumps(GRAPH)};
    const positions = forceLayout(graph.nodes, graph.edges, {{width: 640, height: 420}});
    const p = positions.get('n:person:e');
    const hit = hitTest(positions, p.x + 3, p.y - 3, 14);
    const miss = hitTest(positions, -500, -500, 14);
    const tags = tagCounts(graph.edges);
    const merged = mergeExpansion(
      {{ nodes: [...graph.nodes], edges: [...graph.edges] }},
      [
        {{ other: {{ id: 'n:tool:frankenmemory' }}, edge: {{ id: 'e2' }} }},
        {{ other: {{ id: 'n:place:workshop', kind: 'place', name: 'workshop' }},
           edge: {{ id: 'e4', src_id: 'n:person:e', dst_id: 'n:place:workshop', tag: 'works_at' }} }},
      ],
    );
    console.log(JSON.stringify({{ hit, miss, tags, nodeCount: merged.nodes.length, edgeCount: merged.edges.length }}));
    """
    data = json.loads(_node(js))
    assert data["hit"] == "n:person:e"
    assert data["miss"] is None
    assert data["tags"][0] == {"tag": "uses", "count": 2}
    assert data["nodeCount"] == 4, "existing node not duplicated, new node added"
    assert data["edgeCount"] == 4, "existing edge not duplicated, new edge added"


def test_brain_markup_carries_graph_and_digest_tabs():
    html = (_REPO / "static" / "index.html").read_text()
    assert 'data-memory-tab="graph"' in html
    assert 'data-memory-tab="digest"' in html
    assert 'id="memory-graph-canvas"' in html
    assert 'id="memory-graph-detail"' in html
    assert 'id="memory-digest-trusted"' in html
    assert 'id="memory-digest-untrusted"' in html


def test_memory_js_wires_graph_and_digest():
    source = (_REPO / "static" / "js" / "memory.js").read_text()
    assert "loadMemoryGraph" in source
    assert "loadDigestPreview" in source
    assert "op: 'overview'" in source or '"op": "overview"' in source or "op=overview" in source or "'overview'" in source
    assert "digest-preview" in source
    for external in ("d3js.org", "cdn.jsdelivr", "unpkg.com"):
        assert external not in source, "canvas must stay self-contained (T4)"
