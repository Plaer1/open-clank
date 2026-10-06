// Exact Kene route paint. This deliberately stores only clipped intervals on
// the route polyline: it never infers a cell, a remainder of an edge, or a
// chord across a corner that the route did not traverse.

const INITIAL_ALPHA = 0.34;
const FADE_BUCKETS = 8;
const MAX_COLORS = 6;

function finite(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function point(value) {
  const x = finite(value?.x ?? value?.[0]);
  const y = finite(value?.y ?? value?.[1]);
  return x === null || y === null ? null : { x, y };
}

function at(start, end, fraction) {
  return {
    x: start.x + (end.x - start.x) * fraction,
    y: start.y + (end.y - start.y) * fraction,
  };
}

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}

function colorList(colors) {
  const values = Array.isArray(colors)
    ? colors
    : (colors && typeof colors === 'object' ? Object.values(colors) : []);
  return values.filter(value => typeof value === 'string' && value.trim()).slice(0, MAX_COLORS);
}

function stableIndex(value, length) {
  let hash = 2166136261;
  for (const character of String(value)) {
    hash ^= character.charCodeAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) % length;
}

function resolveColor(value, palette, colors) {
  if (typeof value === 'number' && palette.length) {
    return palette[Math.abs(Math.trunc(value)) % palette.length];
  }
  if (typeof value === 'string' && colors && !Array.isArray(colors)
      && typeof colors === 'object' && typeof colors[value] === 'string') {
    return colors[value];
  }
  return typeof value === 'string' && value.trim() ? value : (palette[0] || '#F6BE48');
}

/**
 * Keep a bounded, fading record of exactly the route lengths a painter has
 * crossed. `time` is absolute effect time supplied by the animation owner;
 * this module never starts or samples a clock.
 */
export function createKenePaintTrail({ maxSegments = 16384, decayMs = 2600, cutoff = 0.012 } = {}) {
  const segmentLimit = Math.max(1, Math.floor(finite(maxSegments) ?? 16384));
  const fadeMs = Math.max(1, finite(decayMs) ?? 2600);
  const alphaCutoff = clamp(finite(cutoff) ?? 0.012, 0, INITIAL_ALPHA);
  const segments = new Map();
  const mergeKeys = new Map();
  let order = 0;

  function remove(key) {
    const segment = segments.get(key);
    if (!segment) return;
    segments.delete(key);
    const keys = mergeKeys.get(segment.mergeKey);
    if (!keys) return;
    keys.delete(key);
    if (!keys.size) mergeKeys.delete(segment.mergeKey);
  }

  function prune(time) {
    for (const [key, segment] of segments) {
      const age = Math.max(0, time - segment.time);
      if (INITIAL_ALPHA * Math.exp(-age / fadeMs) < alphaCutoff) remove(key);
    }
  }

  function enforceLimit() {
    while (segments.size > segmentLimit) {
      const oldestKey = segments.keys().next().value;
      if (oldestKey === undefined) return;
      remove(oldestKey);
    }
  }

  function addClippedEdge({ route, edgeIndex, edgeStart, edgeEnd, from, to, color, time, ownerKey }) {
    const points = route?.points;
    const startPoint = point(points?.[edgeIndex]);
    const endPoint = point(points?.[edgeIndex + 1]);
    const edgeLength = edgeEnd - edgeStart;
    if (!startPoint || !endPoint || !(edgeLength > 0) || !(to > from)) return false;

    const clippedStart = clamp(from, edgeStart, edgeEnd);
    const clippedEnd = clamp(to, edgeStart, edgeEnd);
    if (!(clippedEnd > clippedStart)) return false;

    const mergeKey = `${String(ownerKey)}|${edgeIndex}|${Math.floor(time / 100)}`;
    const epsilon = Math.max(1e-6, edgeLength * 1e-6);
    const keys = mergeKeys.get(mergeKey);
    if (keys) {
      for (const key of keys) {
        const existing = segments.get(key);
        if (!existing || existing.color !== color) continue;
        const touches = clippedStart <= existing.endDistance + epsilon
          && clippedEnd >= existing.startDistance - epsilon;
        if (!touches) continue;
        existing.startDistance = Math.min(existing.startDistance, clippedStart);
        existing.endDistance = Math.max(existing.endDistance, clippedEnd);
        existing.start = at(startPoint, endPoint, (existing.startDistance - edgeStart) / edgeLength);
        existing.end = at(startPoint, endPoint, (existing.endDistance - edgeStart) / edgeLength);
        existing.time = Math.max(existing.time, time);
        // Map insertion order is the O(1) eviction order. A refreshed merge
        // is recent even when it remains in the same 100ms merge bin.
        segments.delete(key);
        segments.set(key, existing);
        return true;
      }
    }

    let key = mergeKey;
    if (segments.has(key)) key = `${mergeKey}|${++order}`;
    const segment = {
      key,
      mergeKey,
      ownerKey: String(ownerKey),
      edgeIndex,
      color,
      time,
      order: ++order,
      startDistance: clippedStart,
      endDistance: clippedEnd,
      start: at(startPoint, endPoint, (clippedStart - edgeStart) / edgeLength),
      end: at(startPoint, endPoint, (clippedEnd - edgeStart) / edgeLength),
    };
    segments.set(key, segment);
    if (!mergeKeys.has(mergeKey)) mergeKeys.set(mergeKey, new Set());
    mergeKeys.get(mergeKey).add(key);
    enforceLimit();
    return true;
  }

  /**
   * Deposits only the exact route interval between two normalized progresses.
   * One entry is emitted per crossed polyline edge, so each turn stays a turn.
   */
  function depositRange(route, fromProgress, toProgress, color, time, ownerKey) {
    const points = route?.points;
    const cumulative = route?.cumulative;
    const total = finite(route?.total);
    const from = finite(fromProgress);
    const to = finite(toProgress);
    const stamp = finite(time);
    if (!Array.isArray(points) || !cumulative || points.length < 2 || !(total > 0)
        || from === null || to === null || stamp === null) return 0;

    const lower = clamp(Math.min(from, to), 0, 1) * total;
    const upper = clamp(Math.max(from, to), 0, 1) * total;
    if (!(upper > lower)) return 0;

    let deposited = 0;
    for (let edgeIndex = 0; edgeIndex < points.length - 1; edgeIndex += 1) {
      const edgeStart = finite(cumulative[edgeIndex]);
      const edgeEnd = finite(cumulative[edgeIndex + 1]);
      if (edgeStart === null || edgeEnd === null || !(edgeEnd > edgeStart)) continue;
      const clippedStart = Math.max(lower, edgeStart);
      const clippedEnd = Math.min(upper, edgeEnd);
      if (!(clippedEnd > clippedStart)) continue;
      if (addClippedEdge({
        route, edgeIndex, edgeStart, edgeEnd, from: clippedStart, to: clippedEnd,
        color, time: stamp, ownerKey,
      })) deposited += 1;
    }
    return deposited;
  }

  /** Draws compact color/fade groups without allocating canvases or Path2Ds. */
  function draw(ctx, { time, colors, intensity = 1, size = 1 } = {}) {
    const stamp = finite(time);
    if (!ctx || stamp === null) return;
    prune(stamp);
    if (!segments.size) return;

    const strength = clamp(finite(intensity) ?? 1, 0, 1);
    if (!strength) return;
    const scale = clamp(finite(size) ?? 1, 0.25, 4);
    const groups = new Map();
    const usedColors = [];
    const palette = colorList(colors);

    for (const segment of segments.values()) {
      const fade = Math.exp(-Math.max(0, stamp - segment.time) / fadeMs);
      if (INITIAL_ALPHA * fade < alphaCutoff) continue;
      let paintColor = resolveColor(segment.color, palette, colors);
      if (!usedColors.includes(paintColor)) {
        if (usedColors.length < MAX_COLORS) usedColors.push(paintColor);
        else paintColor = palette.length
          ? palette[stableIndex(paintColor, palette.length)]
          : usedColors[stableIndex(paintColor, usedColors.length)];
      }
      const fadeBucket = Math.min(FADE_BUCKETS - 1, Math.floor((1 - fade) * FADE_BUCKETS));
      const key = `${paintColor}|${fadeBucket}`;
      if (!groups.has(key)) groups.set(key, { color: paintColor, bucket: fadeBucket, segments: [] });
      groups.get(key).segments.push(segment);
    }

    ctx.save();
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    for (const group of groups.values()) {
      const representativeFade = 1 - ((group.bucket + 0.5) / FADE_BUCKETS);
      const alpha = INITIAL_ALPHA * strength * representativeFade;
      if (alpha <= 0) continue;
      ctx.strokeStyle = group.color;
      ctx.globalAlpha = alpha * 0.34;
      ctx.lineWidth = Math.max(1, scale * 3.2);
      ctx.beginPath();
      for (const segment of group.segments) {
        ctx.moveTo(segment.start.x, segment.start.y);
        ctx.lineTo(segment.end.x, segment.end.y);
      }
      ctx.stroke();

      ctx.globalAlpha = alpha;
      ctx.lineWidth = Math.max(0.7, scale * 1.05);
      ctx.beginPath();
      for (const segment of group.segments) {
        ctx.moveTo(segment.start.x, segment.start.y);
        ctx.lineTo(segment.end.x, segment.end.y);
      }
      ctx.stroke();
    }
    ctx.restore();
  }

  function clear() {
    segments.clear();
    mergeKeys.clear();
  }

  return {
    depositRange,
    draw,
    clear,
    segments,
    get segmentCount() { return segments.size; },
  };
}
