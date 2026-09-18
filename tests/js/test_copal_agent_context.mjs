import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const source = fs.readFileSync(new URL('../../static/js/copal.js', import.meta.url), 'utf8');

test('Copal exposes the identifier-only active agent context bridge', () => {
  assert.match(source, /export function getActiveAgentContext\(\)/);
  assert.match(source, /workspace: state\.workspace/);
  assert.match(source, /resourceKind/);
  assert.match(source, /resourceId/);
  assert.match(source, /__odysseusGetActiveCopalContext/);
  assert.match(source, /__odysseusFlushActiveCopalResource/);
});
