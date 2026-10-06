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
 * Advance one Junction-style drop in bounded 60 Hz reference steps.
 *
 * Junction stores ``vx``/``vy`` in glyph-relative frame units: it damps vx,
 * pulls it toward a lane, then moves by glyph size. Open Clank keeps that
 * shape but makes gravity direction-aware so the owner's normal downward
 * control remains downward instead of eventually reversing itself.
 */
export function advanceRainDrop(options = {}) {
  const {
    x = 0,
    y = 0,
    vx = 0,
    vy = 0,
    laneX = x,
    glyphSize = 12,
    direction = 1,
    speed = 1,
    gravity = 1,
    deltaSeconds = 0,
  } = options;
  // One caller step is at most 1/60th of a second. Keeping this conversion
  // explicit makes the reference's per-frame constants stable at real cadence.
  const frames = Math.max(0, Math.min(1, Number(deltaSeconds) * 60 || 0));
  const size = Math.max(1, Number(glyphSize) || 1);
  const signedDirection = Number(direction) < 0 ? -1 : 1;
  const liveSpeed = Math.max(.1, Number(speed) || 1);
  const liveGravity = Math.max(.1, Number(gravity) || 1);
  const nextVx = Number(vx || 0) * Math.pow(.88, frames)
    + (Number(laneX) - Number(x)) * .0025 * frames;
  // Junction's `vy -= .015 * gravity` is adapted to selected direction. It
  // preserves a normal down/up stream rather than silently flipping it.
  const nextVy = Number(vy || 0) + signedDirection * .015 * liveGravity * frames;
  return {
    x: Number(x) + nextVx * size * frames,
    y: Number(y) + nextVy * size * .36 * liveSpeed * frames,
    vx: nextVx,
    vy: nextVy,
  };
}

/**
 * Junction collision response: a small sideways shove on every confirmed
 * painted-mask hit. There is deliberately no cooldown or vertical nudge:
 * continuous lateral motion lets a drop slide over a glyph edge.
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
    confirmedHit = false,
  } = options;

  if (behind || !obstacle) return { x, y, vxDelta: 0, hit: false };
  if (obstacle.maskAlpha != null && obstacle.maskAlpha <= Math.round(minAlpha * 255)) {
    return { x, y, vxDelta: 0, hit: false };
  }
  const overlaps = confirmedHit || (x + radius > obstacle.left && x - radius < obstacle.right
    && y + radius > obstacle.top && y - radius < obstacle.bottom);
  if (!overlaps) return { x, y, vxDelta: 0, hit: false };

  const centerX = (Number(obstacle.left) + Number(obstacle.right)) / 2;
  const side = x < centerX ? -1 : 1;
  const size = Math.max(1, Number(glyphSize) || 1);
  const shove = size * Math.max(.1, Number(scale) || 1) * (
    .04 + Math.max(0, Number(collisionForce) || 0) * .02
      + Math.max(0, Number(bounce) || 0) * (.03 + Math.max(0, Math.min(1, Number(random) || 0)) * .05)
  );
  return {
    x: x + side * shove,
    y,
    // Junction applies the complete pixel shove to lateral velocity. Keep
    // that exact magnitude; the caller owns its glyph-coordinate movement.
    vxDelta: side * shove * .015,
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
