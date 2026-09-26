/**
 * Rain physics for S24 — direction composition, rare upward inversion and
 * collision response. Pure functions so seeded tests can assert the exact
 * 1-in-10,000 threshold without a browser or a long natural run.
 *
 * Junction reference motion: collision is a small lateral shove that preserves
 * vertical travel, not a reversal of vertical velocity.
 */

/** Rare-effect sample probability — once per spawn/recycle, never per frame. */
export const RARE_UPWARD_PROBABILITY = 1 / 10000;

/** Obstacle alpha threshold in 0..1 (Junction: 24/255). */
export const COLLISION_ALPHA_THRESHOLD = 24 / 255;

/**
 * Compose drop direction from the user's controls and the rare effect.
 *
 * Order (S24): normal direction → explicit reverse chance → rare inversion.
 * With defaults (down, 0% reverse, rare ON) a rare sample sends the drop up
 * and it spawns from below the viewport.
 *
 * @param {object} options
 * @param {boolean} [options.rainDown=true] Explicit user direction control.
 * @param {number} [options.reverseChance=0] Percent (0..100), coarse slider.
 * @param {boolean} [options.rareUpward=true] Separate rare-effect toggle.
 * @param {number} [options.noise=0] Deterministic sample in [0,1) for reverse.
 * @param {number} [options.rareNoise=1] Deterministic sample in [0,1) for rare.
 *   Defaults to 1 (never fires) so callers must pass the per-spawn sample.
 * @returns {{direction: 1|-1, rare: boolean, reversed: boolean}}
 */
export function composeRainDirection(options = {}) {
  const {
    rainDown = true,
    reverseChance = 0,
    rareUpward = true,
    noise = 0,
    rareNoise = 1,
  } = options;
  let direction = rainDown ? 1 : -1;
  let reversed = false;
  const chance = Number(reverseChance);
  if (Number.isFinite(chance) && chance > 0 && noise < chance / 100) {
    direction = direction > 0 ? -1 : 1;
    reversed = true;
  }
  const rare = !!rareUpward && shouldRareUpward(rareNoise);
  if (rare) {
    direction = direction > 0 ? -1 : 1;
  }
  return { direction, rare, reversed };
}

/**
 * Exact rare threshold. Sampled once per spawn/recycle with a deterministic
 * noise value in [0,1). Natural long runs are not a reliable way to test a
 * rare event — callers should inject rareNoise at the boundary in tests.
 */
export function shouldRareUpward(rareNoise) {
  const value = Number(rareNoise);
  return Number.isFinite(value) && value >= 0 && value < RARE_UPWARD_PROBABILITY;
}

/**
 * Spawn placement for a drop. Rare inversions always enter from the travel
 * origin edge so a default rare drop travels up from below the viewport.
 */
export function spawnPosition(options = {}) {
  const {
    direction = 1,
    rare = false,
    waves = false,
    width = 0,
    height = 0,
    radius = 0,
    entryNoise = 0,
    lateralNoise = 0.5,
    spread = 1,
    cell = 16,
    initialX = width / 2,
  } = options;
  const fromEdge = waves || rare;
  const band = height * (waves ? 0.25 : 1) * Math.max(0, spread);
  const offset = Math.max(0, Math.min(1, Number(entryNoise) || 0)) * band;
  const y = direction > 0
    ? -radius - offset
    : height + radius + offset;
  const x = fromEdge || waves
    ? initialX + (Math.max(0, Math.min(1, Number(lateralNoise) || 0)) - 0.5) * cell * spread
    : Math.max(0, Math.min(width, initialX + (Number(lateralNoise) - 0.5) * cell * spread));
  return { x, y };
}

/**
 * Junction collision response: a small sideways shove along the obstacle edge.
 * Vertical velocity (speed/direction) is preserved so the drop continues its
 * prevailing travel after the visible deflection.
 *
 * @returns {{x:number,y:number,lateral:number,hit:boolean}}
 */
export function resolveCollision(options = {}) {
  const {
    x = 0,
    y = 0,
    radius = 0,
    obstacle = null,
    glyphSize = 12,
    scale = 1,
    collisionForce = 2.2,
    bounce = 1.3,
    random = 0.5,
    minAlpha = COLLISION_ALPHA_THRESHOLD,
    behind = false,
    lastHitAt = -Infinity,
    time = 0,
    hitCooldownMs = 130,
  } = options;

  if (behind || !obstacle) {
    return { x, y, lateral: 0, hit: false };
  }
  if (obstacle.maskAlpha != null && obstacle.maskAlpha <= Math.round(minAlpha * 255)) {
    return { x, y, lateral: 0, hit: false };
  }
  if (Number.isFinite(lastHitAt) && Number.isFinite(time) && time - lastHitAt <= hitCooldownMs) {
    return { x, y, lateral: 0, hit: false };
  }
  const overlaps = x + radius > obstacle.left && x - radius < obstacle.right
    && y + radius > obstacle.top && y - radius < obstacle.bottom;
  if (!overlaps) {
    return { x, y, lateral: 0, hit: false };
  }

  // Deflect along the nearest edge (axis of least penetration) so the drop
  // slides along the obstacle rather than reversing vertical travel.
  const pushLeft = x + radius - obstacle.left;
  const pushRight = obstacle.right - (x - radius);
  const pushTop = y + radius - obstacle.top;
  const pushBottom = obstacle.bottom - (y - radius);
  const horizontal = Math.min(pushLeft, pushRight);
  const vertical = Math.min(pushTop, pushBottom);

  // The .04 term is intentional and remains when both sliders are 0.
  const shove = glyphSize * scale * (
    0.04 + collisionForce * 0.02 + bounce * (0.03 + Math.max(0, Math.min(1, random)) * 0.05)
  );

  if (vertical < horizontal) {
    // Top/bottom edge: still a lateral shove (Junction does not reverse vy);
    // push out just enough to clear the edge, then resume travel.
    const side = y < (obstacle.top + obstacle.bottom) / 2 ? -1 : 1;
    return {
      x,
      y: y + side * Math.max(1, vertical),
      lateral: (x < (obstacle.left + obstacle.right) / 2 ? -1 : 1) * shove * 0.65,
      hit: true,
    };
  }

  const side = x < (obstacle.left + obstacle.right) / 2 ? -1 : 1;
  return {
    x: x + side * shove,
    y,
    lateral: side * shove * 3,
    hit: true,
  };
}

/**
 * Whether a stream is eligible for collisions. Behind-layer drops ignore
 * collisions (Junction: ~34% of particles sit behind the wordmark).
 */
export function collidesWithObstacles(stream) {
  return !!(stream && !stream.behind);
}

/**
 * Test seam: exact boundary cases for the 1-in-10,000 rare sample.
 * Returns true only for rareNoise in [0, 1/10000).
 */
export function isRareUpwardSample(rareNoise) {
  return shouldRareUpward(rareNoise);
}
