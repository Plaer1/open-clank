// Self-contained force layout for the Brain graph canvas (memory-trust
// metaplan T4). Pure math, no DOM, no external libs — node-testable.
// Deterministic: seeded ring start + fixed iteration count, so the same
// graph always settles into the same picture.

export function forceLayout(nodes, edges, options = {}) {
  const width = options.width || 800;
  const height = options.height || 600;
  const iterations = options.iterations ?? 120;
  const count = nodes.length;
  const positions = new Map();
  if (!count) return positions;

  // Seed on a ring (deterministic — no randomness, stable pictures).
  const cx = width / 2;
  const cy = height / 2;
  const seedRadius = Math.min(width, height) * 0.35;
  nodes.forEach((node, index) => {
    const angle = (index / count) * Math.PI * 2;
    positions.set(node.id, {
      x: cx + seedRadius * Math.cos(angle),
      y: cy + seedRadius * Math.sin(angle),
    });
  });
  if (count === 1) {
    positions.set(nodes[0].id, { x: cx, y: cy });
    return positions;
  }

  const ids = nodes.map((n) => n.id);
  const springs = edges
    .filter((e) => positions.has(e.src_id) && positions.has(e.dst_id))
    .map((e) => [e.src_id, e.dst_id]);

  const area = width * height;
  const k = Math.sqrt(area / count) * 0.6; // ideal spacing
  let temperature = Math.min(width, height) * 0.1;
  const cool = temperature / (iterations + 1);

  for (let step = 0; step < iterations; step++) {
    const disp = new Map(ids.map((id) => [id, { x: 0, y: 0 }]));

    // Repulsion between every pair.
    for (let i = 0; i < count; i++) {
      for (let j = i + 1; j < count; j++) {
        const a = positions.get(ids[i]);
        const b = positions.get(ids[j]);
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let dist = Math.hypot(dx, dy) || 0.01;
        const force = (k * k) / dist;
        dx /= dist; dy /= dist;
        const da = disp.get(ids[i]);
        const db = disp.get(ids[j]);
        da.x += dx * force; da.y += dy * force;
        db.x -= dx * force; db.y -= dy * force;
      }
    }

    // Spring attraction along edges.
    for (const [srcId, dstId] of springs) {
      const a = positions.get(srcId);
      const b = positions.get(dstId);
      let dx = a.x - b.x;
      let dy = a.y - b.y;
      let dist = Math.hypot(dx, dy) || 0.01;
      const force = (dist * dist) / k;
      dx /= dist; dy /= dist;
      const da = disp.get(srcId);
      const db = disp.get(dstId);
      da.x -= dx * force; da.y -= dy * force;
      db.x += dx * force; db.y += dy * force;
    }

    // Gentle pull toward center keeps disconnected components on screen.
    for (const id of ids) {
      const p = positions.get(id);
      const d = disp.get(id);
      d.x += (cx - p.x) * 0.02;
      d.y += (cy - p.y) * 0.02;
    }

    for (const id of ids) {
      const p = positions.get(id);
      const d = disp.get(id);
      const len = Math.hypot(d.x, d.y) || 0.01;
      const clamped = Math.min(len, temperature);
      p.x += (d.x / len) * clamped;
      p.y += (d.y / len) * clamped;
      p.x = Math.max(20, Math.min(width - 20, p.x));
      p.y = Math.max(20, Math.min(height - 20, p.y));
    }
    temperature -= cool;
  }
  return positions;
}

// Hit test in GRAPH coordinates (caller converts screen → graph through
// its pan/zoom transform first). Nearest node within radius wins.
export function hitTest(positions, x, y, radius = 14) {
  let best = null;
  let bestDist = radius;
  for (const [id, p] of positions.entries()) {
    const dist = Math.hypot(p.x - x, p.y - y);
    if (dist <= bestDist) {
      best = id;
      bestDist = dist;
    }
  }
  return best;
}

// Tag histogram for the filter chips: [{tag, count}] sorted desc.
export function tagCounts(edges) {
  const counts = new Map();
  for (const edge of edges) {
    const tag = String(edge.tag || '');
    if (!tag) continue;
    counts.set(tag, (counts.get(tag) || 0) + 1);
  }
  return [...counts.entries()]
    .map(([tag, count]) => ({ tag, count }))
    .sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag));
}

// Merge an expand() result into the working graph without duplicates.
export function mergeExpansion(graph, hits) {
  const nodeIds = new Set(graph.nodes.map((n) => n.id));
  const edgeIds = new Set(graph.edges.map((e) => e.id));
  for (const hit of hits || []) {
    if (hit.other && !nodeIds.has(hit.other.id)) {
      graph.nodes.push(hit.other);
      nodeIds.add(hit.other.id);
    }
    if (hit.edge && !edgeIds.has(hit.edge.id)) {
      graph.edges.push(hit.edge);
      edgeIds.add(hit.edge.id);
    }
  }
  return graph;
}
