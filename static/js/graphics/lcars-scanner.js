// LCARS scanner renderer.  It is deliberately self-contained: theme.js owns
// the canvas, animation loop, persistence and control UI; this module only
// turns a bounded scene state into Canvas2D commands.

export const LCARS_SCANNER_CONTROLS = Object.freeze([
  { key: 'mode', label: 'Scanner mode', type: 'range', min: 0, max: 3, step: 1, default: 0, labels: { 0: 'Science', 1: 'Navigation', 2: 'Engineering', 3: 'Tactical' } },
  { key: 'sweepStyle', label: 'Sweep style', type: 'range', min: 0, max: 3, step: 1, default: 0, labels: { 0: 'Linear', 1: 'Return', 2: 'Dual', 3: 'Sector' } },
  { key: 'scanSpeed', label: 'Scan speed', type: 'range', min: 0, max: 300, step: 5, default: 100, unit: '%' },
  { key: 'beamWidth', label: 'Beam width', type: 'range', min: 5, max: 80, step: 1, default: 20, unit: '%' },
  { key: 'scanRange', label: 'Scan range', type: 'range', min: 20, max: 100, step: 1, default: 80, unit: '%' },
  { key: 'sensitivity', label: 'Sensitivity', type: 'range', min: 0, max: 100, step: 1, default: 65, unit: '%' },
  { key: 'contactDensity', label: 'Contact density', type: 'range', min: 0, max: 48, step: 1, default: 14 },
  { key: 'persistence', label: 'Contact persistence', type: 'range', min: 1, max: 15, step: 1, default: 5, unit: ' s' },
  { key: 'contactSize', label: 'Contact size', type: 'range', min: 50, max: 200, step: 1, default: 100, unit: '%' },
  { key: 'sweepBrightness', label: 'Sweep brightness', type: 'range', min: 0, max: 100, step: 1, default: 45, unit: '%' },
  { key: 'gridDetail', label: 'Grid detail', type: 'range', min: 0, max: 100, step: 1, default: 35, unit: '%' },
  { key: 'scannerHeight', label: 'Scanner strip height', type: 'range', min: 10, max: 40, step: 1, default: 22, unit: '%' },
  { key: 'showReadouts', label: 'Show readouts', type: 'toggle', default: true },
  { key: 'showTrails', label: 'Show trails', type: 'toggle', default: true },
  { key: 'anomalyChance', label: 'Anomaly chance', type: 'range', min: 0, max: 30, step: 1, default: 5, unit: '%' },
]);

const MAX_CONTACTS = 48;
const TAU = Math.PI * 2;
const MODE_NAMES = ['ION', 'WAYPOINT', 'POWER', 'CONTACT'];
const MODE_ANOMALIES = ['SUBSPACE', 'BUOY', 'PLASMA', 'SHIELD'];

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}

function number(value, fallback, low, high) {
  const parsed = Number(value);
  return clamp(Number.isFinite(parsed) ? parsed : fallback, low, high);
}

function controlValues(input = {}) {
  return {
    mode: Math.round(number(input.mode, 0, 0, 3)),
    sweepStyle: Math.round(number(input.sweepStyle, 0, 0, 3)),
    scanSpeed: number(input.scanSpeed, 100, 0, 300),
    beamWidth: number(input.beamWidth, 20, 5, 80),
    scanRange: number(input.scanRange, 80, 20, 100),
    sensitivity: number(input.sensitivity, 65, 0, 100),
    contactDensity: Math.round(number(input.contactDensity, 14, 0, MAX_CONTACTS)),
    persistence: number(input.persistence, 5, 1, 15),
    contactSize: number(input.contactSize, 100, 50, 200),
    sweepBrightness: number(input.sweepBrightness, 45, 0, 100),
    gridDetail: number(input.gridDetail, 35, 0, 100),
    scannerHeight: number(input.scannerHeight, 22, 10, 40),
    showReadouts: input.showReadouts === undefined ? true : input.showReadouts === true || input.showReadouts === 'true' || input.showReadouts === 1,
    showTrails: input.showTrails === undefined ? true : input.showTrails === true || input.showTrails === 'true' || input.showTrails === 1,
    anomalyChance: number(input.anomalyChance, 5, 0, 30),
  };
}

// A compact deterministic hash keeps the decorative pool stable without using
// Math.random() or a per-frame allocation.
function seed(index, salt) {
  let value = (index + 1) * 0x9e3779b1 + salt * 0x85ebca6b;
  value ^= value >>> 16;
  value = Math.imul(value, 0x7feb352d);
  value ^= value >>> 15;
  value = Math.imul(value, 0x846ca68b);
  value ^= value >>> 16;
  return (value >>> 0) / 0x100000000;
}

function createContact(index) {
  return {
    id: index + 1,
    x: .04 + seed(index, 1) * .92,
    y: .12 + seed(index, 2) * .76,
    strength: .12 + seed(index, 3) * .88,
    depth: .05 + seed(index, 4) * .95,
    anomaly: seed(index, 5),
    tint: Math.floor(seed(index, 6) * 6),
    discoveredAt: -Infinity,
    lastBeamX: -1,
  };
}

function normalizeBounds(bounds, size) {
  const width = Math.max(1, Number(bounds?.width ?? size?.width ?? size?.x ?? 1));
  const height = Math.max(1, Number(bounds?.height ?? size?.height ?? size?.y ?? 1));
  const left = Number(bounds?.left ?? bounds?.x ?? 0);
  const top = Number(bounds?.top ?? bounds?.y ?? 0);
  const right = Number(bounds?.right ?? left + width);
  const bottom = Number(bounds?.bottom ?? top + height);
  return {
    left: Math.min(left, right),
    right: Math.max(left + 1, right),
    top: Math.min(top, bottom),
    bottom: Math.max(top + 1, bottom),
  };
}

function stripFor(bounds, controls, scale) {
  const width = Math.max(1, bounds.right - bounds.left);
  const available = Math.max(1, bounds.bottom - bounds.top);
  const outer = clamp(Math.min(18 * scale, available * .08), 0, Math.max(0, (available - 1) / 2));
  const maxHeight = Math.max(1, available - outer * 2);
  const minHeight = Math.min(maxHeight, Math.max(1, 14 * scale));
  const height = clamp(available * controls.scannerHeight / 100, minHeight, maxHeight);
  const horizontal = clamp(Math.max(3 * scale, width * .035), 0, Math.max(0, (width - 2) / 2));
  const left = bounds.left + horizontal;
  const right = Math.max(left + 1, bounds.right - horizontal);
  const top = clamp(bounds.top + outer, bounds.top, Math.max(bounds.top, bounds.bottom - height));
  return { left, right, top, bottom: Math.min(bounds.bottom, top + height), height };
}

function palette(colors) {
  const usable = Array.isArray(colors) ? colors.filter((color) => typeof color === 'string' && color) : [];
  return usable.length ? usable : ['currentColor'];
}

function sweepBeams(phase, style) {
  if (style === 1) {
    const direction = Math.sin(phase * TAU) >= 0 ? 1 : -1;
    return [{ x: .5 - .5 * Math.cos(phase * TAU), direction, kind: 'reflect' }];
  }
  if (style === 2) return [{ x: phase, direction: 1, kind: 'wrap' }, { x: 1 - phase, direction: -1, kind: 'wrap' }];
  if (style === 3) {
    const sector = Math.min(11, Math.floor(phase * 12));
    return [{ x: (sector + .5) / 12, direction: 0, kind: 'sector', sector }];
  }
  return [{ x: phase, direction: 1, kind: 'wrap' }];
}

function wrappedDistance(a, b) {
  const raw = Math.abs(a - b);
  return Math.min(raw, 1 - raw);
}

function forwardArcContains(start, end, x, beam) {
  const travelled = (end - start + 1) % 1;
  const offset = (x - start + 1) % 1;
  return offset <= travelled + beam || wrappedDistance(start, x) <= beam || wrappedDistance(end, x) <= beam;
}

// Crossing deliberately follows the beam's actual movement. A reflected return
// is a bounded interval, and a reverse Dual beam traverses the opposite arc;
// neither may be misread as a forward wrap across the entire scanner.
function passesContact(previous, next, x, beam, initial = false) {
  if (next.kind === 'sector') {
    if (initial) return Math.abs(next.x - x) <= Math.max(beam, 1 / 24);
    return previous?.sector !== next.sector && Math.abs(next.x - x) <= Math.max(beam, 1 / 24);
  }
  if (next.kind === 'reflect') {
    // Return is a reflected interval, never a circular scanner coordinate.
    // Its left edge must not make a right-edge contact look current.
    if (initial || Math.abs(next.x - x) <= beam) return Math.abs(next.x - x) <= beam;
    return x >= Math.min(previous.x, next.x) - beam && x <= Math.max(previous.x, next.x) + beam;
  }
  if (initial) return wrappedDistance(next.x, x) <= beam;
  if (wrappedDistance(next.x, x) <= beam) return true;
  if (next.direction >= 0) return forwardArcContains(previous.x, next.x, x, beam);
  return forwardArcContains(next.x, previous.x, x, beam);
}

function drawDiamond(ctx, x, y, radius) {
  ctx.beginPath();
  ctx.moveTo(x, y - radius); ctx.lineTo(x + radius, y); ctx.lineTo(x, y + radius); ctx.lineTo(x - radius, y);
  ctx.closePath();
}

function drawShield(ctx, x, y, radius) {
  ctx.beginPath();
  ctx.moveTo(x, y - radius); ctx.lineTo(x + radius * .85, y - radius * .35);
  ctx.lineTo(x + radius * .62, y + radius * .8); ctx.lineTo(x, y + radius * 1.15);
  ctx.lineTo(x - radius * .62, y + radius * .8); ctx.lineTo(x - radius * .85, y - radius * .35);
  ctx.closePath();
}

function drawContact(ctx, contact, mode, x, y, radius, color, accent, alpha, anomaly, trails) {
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.globalAlpha = alpha;
  ctx.lineWidth = Math.max(.75, radius * .19);
  if (trails) {
    ctx.beginPath();
    ctx.moveTo(x - radius * 3.2, y); ctx.lineTo(x - radius * .9, y);
    ctx.globalAlpha = alpha * .36;
    ctx.stroke();
    ctx.globalAlpha = alpha;
  }
  if (mode === 0) {
    drawDiamond(ctx, x, y, radius);
    ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x - radius * 1.7, y); ctx.lineTo(x + radius * 1.7, y); ctx.stroke();
  } else if (mode === 1) {
    ctx.beginPath();
    ctx.moveTo(x - radius, y + radius); ctx.lineTo(x, y - radius); ctx.lineTo(x + radius, y + radius);
    ctx.stroke();
    ctx.fillRect(x - radius * .28, y - radius * .28, radius * .56, radius * .56);
  } else if (mode === 2) {
    ctx.strokeRect(x - radius, y - radius, radius * 2, radius * 2);
    ctx.beginPath(); ctx.moveTo(x - radius * 1.55, y); ctx.lineTo(x + radius * 1.55, y); ctx.moveTo(x, y - radius * 1.55); ctx.lineTo(x, y + radius * 1.55); ctx.stroke();
  } else {
    drawShield(ctx, x, y, radius);
    ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x - radius * 1.55, y + radius * 1.35); ctx.lineTo(x + radius * 1.55, y + radius * 1.35); ctx.stroke();
  }
  if (anomaly) {
    ctx.strokeStyle = accent;
    ctx.globalAlpha = alpha * .88;
    ctx.beginPath();
    ctx.moveTo(x - radius * .5, y - radius * .5); ctx.lineTo(x + radius * .5, y + radius * .5);
    ctx.moveTo(x + radius * .5, y - radius * .5); ctx.lineTo(x - radius * .5, y + radius * .5);
    ctx.stroke();
  }
}

function drawFrame(ctx, strip, colors, intensity, scale, gridDetail) {
  const width = strip.right - strip.left;
  const accent = colors[0];
  const secondary = colors[1 % colors.length];
  ctx.lineWidth = Math.max(1, scale);
  ctx.strokeStyle = accent;
  ctx.globalAlpha = intensity * .35;
  ctx.strokeRect(strip.left, strip.top, width, strip.height);
  const bracket = Math.min(width * .08, 24 * scale);
  const cap = Math.min(strip.height * .38, 14 * scale);
  ctx.globalAlpha = intensity * .72;
  for (const side of [strip.left, strip.right]) {
    const dir = side === strip.left ? 1 : -1;
    ctx.beginPath();
    ctx.moveTo(side, strip.top + cap); ctx.lineTo(side + dir * bracket, strip.top + cap); ctx.lineTo(side + dir * bracket, strip.top);
    ctx.moveTo(side, strip.bottom - cap); ctx.lineTo(side + dir * bracket, strip.bottom - cap); ctx.lineTo(side + dir * bracket, strip.bottom);
    ctx.stroke();
  }
  const columns = Math.round(gridDetail / 100 * 12);
  if (columns > 0) {
    ctx.strokeStyle = secondary;
    ctx.lineWidth = Math.max(.5, scale * .65);
    ctx.globalAlpha = intensity * (.04 + gridDetail / 100 * .12);
    for (let column = 1; column < columns; column += 1) {
      const x = strip.left + width * column / columns;
      ctx.beginPath(); ctx.moveTo(x, strip.top + 2 * scale); ctx.lineTo(x, strip.bottom - 2 * scale); ctx.stroke();
    }
    const rows = Math.max(1, Math.round(columns / 4));
    for (let row = 1; row <= rows; row += 1) {
      const y = strip.top + strip.height * row / (rows + 1);
      ctx.beginPath(); ctx.moveTo(strip.left + 2 * scale, y); ctx.lineTo(strip.right - 2 * scale, y); ctx.stroke();
    }
  }
}

function drawBeam(ctx, strip, positions, style, colors, intensity, controls, scale) {
  const width = strip.right - strip.left;
  const beam = width * controls.beamWidth / 100;
  const bright = controls.sweepBrightness / 100 * intensity;
  const main = colors[2 % colors.length];
  const accent = colors[3 % colors.length];
  for (let index = 0; index < positions.length; index += 1) {
    const x = strip.left + positions[index] * width;
    ctx.fillStyle = main;
    ctx.globalAlpha = bright * .08;
    ctx.fillRect(x - beam * .5, strip.top + scale, beam, Math.max(1, strip.height - scale * 2));
    ctx.strokeStyle = main;
    ctx.lineWidth = Math.max(1, scale * 1.25);
    ctx.globalAlpha = bright * .82;
    ctx.beginPath(); ctx.moveTo(x, strip.top); ctx.lineTo(x, strip.bottom); ctx.stroke();
    if (style === 3) {
      ctx.strokeStyle = accent;
      ctx.globalAlpha = bright * .45;
      ctx.lineWidth = Math.max(.75, scale);
      ctx.beginPath();
      ctx.moveTo(x, strip.top); ctx.lineTo(x + beam * .38, strip.top + strip.height * .5); ctx.lineTo(x, strip.bottom);
      ctx.stroke();
    }
  }
}

function drawReadouts(ctx, rows, strip, controls, colors, intensity, scale) {
  if (!controls.showReadouts || !rows.length || strip.height < 18 * scale) return;
  const fontSize = clamp(8 * scale, 7, 13);
  const innerTop = strip.top + fontSize;
  const innerBottom = strip.bottom - fontSize;
  if (innerBottom < innerTop) return;
  ctx.font = `${Math.round(fontSize)}px ui-monospace, SFMono-Regular, Menlo, monospace`;
  ctx.textBaseline = 'middle';
  ctx.fillStyle = colors[4 % colors.length];
  ctx.globalAlpha = intensity * .72;
  const max = Math.min(8, rows.length);
  for (let index = 0; index < max; index += 1) {
    const row = rows[index];
    const y = clamp(row.y + fontSize * 1.4, innerTop, innerBottom);
    const classification = row.anomaly ? MODE_ANOMALIES[controls.mode] : MODE_NAMES[controls.mode];
    const label = `${classification}-${String(row.id).padStart(2, '0')}`;
    ctx.fillText(label, clamp(row.x + 7 * scale, strip.left + 3 * scale, strip.right - 84 * scale), y);
  }
}

/**
 * Build an LCARS scanner renderer. State is intentionally inspectable for the
 * theme diagnostics panel and always bounded to the fixed 48-contact pool.
 */
export function createLcarsScanner() {
  const state = {
    contacts: Array.from({ length: MAX_CONTACTS }, (_, index) => createContact(index)),
    scanPhase: .18,
    lastTime: null,
    simulationTime: 0,
  };

  function draw(ctx, { time = 0, reduced = false, bounds = null, size = 1, intensity = 1, colors = [], controls = {} } = {}) {
    if (!ctx) return state;
    const values = controlValues(controls);
    const scale = clamp(Number(size) || 1, .4, 3);
    const alpha = clamp(Number(intensity) || 0, 0, 1);
    const area = normalizeBounds(bounds, size);
    if (area.right - area.left < 6 || area.bottom - area.top < 6 || alpha <= 0) return state;
    const strip = stripFor(area, values, scale);
    if (strip.height <= 1 || strip.right <= strip.left) return state;
    const colorsForFrame = palette(colors);
    const wallTime = Number.isFinite(Number(time)) ? Number(time) : 0;
    const previousPhase = state.scanPhase;
    const initialFrame = state.lastTime == null;
    if (!reduced) {
      const elapsed = initialFrame ? 0 : clamp((wallTime - state.lastTime) / 1000, 0, .12);
      state.lastTime = wallTime;
      // The logical age clock stops with speed zero. Contacts therefore retain
      // their discovery/fade state while scanning is paused, then continue in
      // real seconds only while the scanner is running.
      if (values.scanSpeed > 0 && elapsed > 0) {
        state.simulationTime += elapsed * 1000;
        state.scanPhase = (state.scanPhase + elapsed * .12 * values.scanSpeed / 100) % 1;
      }
    } else {
      // Reduced motion is a fixed still, independent of historical discoveries
      // and wall clock time. Animated state remains paused for a later resume.
      state.lastTime = null;
    }
    const phase = reduced ? .18 : state.scanPhase;
    const positions = sweepBeams(phase, values.sweepStyle);
    const priorPositions = sweepBeams(reduced ? .18 : previousPhase, values.sweepStyle);
    const now = state.simulationTime;
    const beamFraction = values.beamWidth / 100 * .5;
    const rangeGate = values.scanRange / 100;
    const sensitivityGate = 1 - values.sensitivity / 100;
    const anomalyGate = values.anomalyChance / 100;
    const visible = [];
    const width = strip.right - strip.left;
    const radius = clamp(2.8 * scale * values.contactSize / 100, 1.5, Math.max(2, strip.height * .22));
    const persistMs = values.persistence * 1000;

    ctx.save();
    ctx.beginPath(); ctx.rect(strip.left, strip.top, width, strip.height); ctx.clip();
    drawFrame(ctx, strip, colorsForFrame, alpha, scale, values.gridDetail);
    drawBeam(ctx, strip, positions.map((beam) => beam.x), values.sweepStyle, colorsForFrame, alpha, values, scale);
    for (let index = 0; index < values.contactDensity; index += 1) {
      const contact = state.contacts[index];
      if (contact.depth > rangeGate || contact.strength < sensitivityGate) continue;
      let crossed = false;
      for (let beamIndex = 0; beamIndex < positions.length; beamIndex += 1) {
        if (reduced) crossed ||= wrappedDistance(positions[beamIndex].x, contact.x) <= beamFraction;
        else if (values.scanSpeed > 0) crossed ||= passesContact(priorPositions[beamIndex], positions[beamIndex], contact.x, beamFraction, initialFrame);
      }
      if (!reduced && crossed) {
        contact.discoveredAt = now;
        contact.lastBeamX = contact.x;
      }
      // A reduced-motion paint is a fresh fixed composition, not a paused
      // replay. Historical animated discoveries never enter this branch.
      if (reduced && !crossed) continue;
      const age = now - contact.discoveredAt;
      const staticVisible = reduced && crossed;
      if (!staticVisible && (!Number.isFinite(contact.discoveredAt) || age < 0 || age > persistMs)) continue;
      const fade = staticVisible ? 1 : clamp(1 - age / persistMs, 0, 1);
      const x = strip.left + contact.x * width;
      const y = strip.top + contact.y * strip.height;
      const anomaly = contact.anomaly <= anomalyGate;
      const color = colorsForFrame[(contact.tint + values.mode) % colorsForFrame.length];
      const accent = colorsForFrame[(contact.tint + 2 + values.mode) % colorsForFrame.length];
      drawContact(ctx, contact, values.mode, x, y, radius, color, accent, alpha * fade * (.35 + contact.strength * .65), anomaly, values.showTrails);
      visible.push({ id: contact.id, x, y, strength: contact.strength, anomaly });
    }
    drawReadouts(ctx, visible, strip, values, colorsForFrame, alpha, scale);
    ctx.restore();
    return state;
  }

  return { draw, state };
}

export default createLcarsScanner;
