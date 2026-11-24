import assert from 'node:assert/strict';
import test from 'node:test';
import { createJunctionRainScene, stepJunctionRain, JUNCTION_RARE_UPWARD_PROBABILITY } from '../static/js/graphics/junction-rain.js';

for (const mode of ['matrix', 'emoji']) {
  test(`${mode} streams advance in fixed lanes irrespective of paint cadence`, () => {
    const options = { width: 640, height: 320, mode, emojiCharset: '✨🧑‍💻', random: () => .5 };
    const whole = createJunctionRainScene(options), half = createJunctionRainScene(options);
    stepJunctionRain(whole, { deltaSeconds: 1 / 30, random: () => .5 });
    for (let i = 0; i < 2; i++) stepJunctionRain(half, { deltaSeconds: 1 / 60, random: () => .5 });
    for (const [i, drop] of whole.drops.entries()) {
      assert.equal(drop.x, drop.laneX);
      assert.ok(drop.y > options.height * .5);
      assert.ok(Math.abs(drop.y - half.drops[i].y) < 1e-8);
      assert.equal(drop.emoji, mode === 'emoji');
    }
    stepJunctionRain(whole, { reduced: true, random: () => .5 });
    const still = whole.drops.map(drop => drop.y);
    stepJunctionRain(whole, { reduced: true, deltaSeconds: 1 / 30, random: () => .1 });
    assert.deepEqual(whole.drops.map(drop => drop.y), still);
  });
}

test('rare inversion uses the one-in-ten-thousand spawn threshold', () => {
  const make = value => createJunctionRainScene({ width: 30, height: 30, random: () => value }).drops[0];
  assert.equal(JUNCTION_RARE_UPWARD_PROBABILITY, 1 / 10000);
  assert.equal(make(JUNCTION_RARE_UPWARD_PROBABILITY - Number.EPSILON).direction, -1);
  assert.equal(make(JUNCTION_RARE_UPWARD_PROBABILITY).direction, 1);
});
