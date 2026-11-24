// Smooth 1-D value noise. Hash only the neighbouring lattice points, then
// ease between them so a pulse has no sharp corners or fixed repeat period.
function sample(cell, seed) {
  let bits = Math.imul(cell ^ seed, 0x85ebca6b);
  bits = Math.imul(bits ^ (bits >>> 13), 0xc2b2ae35);
  return ((bits ^ (bits >>> 16)) >>> 0) / 0xffffffff;
}

export function pulseGain(phase, depth, seed) {
  const cell = Math.floor(phase);
  const t = phase - cell;
  const blend = t * t * (3 - 2 * t);
  const from = sample(cell, seed);
  const to = sample(cell + 1, seed);
  return 1 - depth * (from + (to - from) * blend);
}
