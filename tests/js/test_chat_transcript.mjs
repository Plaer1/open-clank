import assert from 'node:assert/strict';
import test from 'node:test';

import { normalizeAssistantTranscript } from '../../static/js/chatTranscript.js';

const tools = [
  { round: 1, id: 'tool-1' },
  { round: 2, id: 'tool-2' },
];

test('canonical answer survives missing, empty, partial, and stale legacy rounds', () => {
  for (const round_texts of [undefined, [], ['  '], ['partial'], ['answer', 'answer']]) {
    const result = normalizeAssistantTranscript('answer', { tool_events: tools, round_texts });
    assert.equal(result.source, 'canonical-fallback');
    assert.deepEqual(result.roundTexts, ['', '', 'answer']);
  }
});

test('exact legacy segmentation is retained without duplicating canonical prose', () => {
  const result = normalizeAssistantTranscript('first\n\nlast', {
    tool_events: tools,
    round_texts: ['first', 'last'],
  });
  assert.equal(result.source, 'legacy-matched');
  assert.deepEqual(result.roundTexts, ['first', 'last']);
});

test('blank historical canonical content can recover meaningful rounds', () => {
  const result = normalizeAssistantTranscript('', {
    tool_events: tools,
    round_texts: ['', 'recovered'],
  });
  assert.equal(result.source, 'legacy-recovery');
  assert.deepEqual(result.roundTexts, ['', 'recovered']);
});

test('valid terminal layout references canonical content and tool ids', () => {
  const result = normalizeAssistantTranscript('answer', {
    tool_events: tools,
    transcript_v2: {
      version: 1,
      content_length: 6,
      content_sha256: 'server-validated',
      blocks: [
        { kind: 'tool', call_id: 'tool-1', round: 1 },
        { kind: 'tool', call_id: 'tool-2', round: 2 },
        { kind: 'answer', start: 0, end: 6, round: 2 },
      ],
    },
  });
  assert.equal(result.source, 'transcript_v2');
  assert.deepEqual(result.roundTexts, ['', '', 'answer']);
});

test('malformed terminal layout falls back to canonical content', () => {
  for (const transcript_v2 of [
    { version: 99, content_length: 6, blocks: [] },
    { version: 1, content_length: 5, blocks: [] },
    { version: 1, content_length: 6, blocks: [{ kind: 'tool', call_id: 'missing', round: 1 }] },
    { version: 1, content_length: 6, blocks: [{ kind: 'answer', start: 1, end: 6, round: 1 }] },
  ]) {
    const result = normalizeAssistantTranscript('answer', { tool_events: tools, transcript_v2 });
    assert.equal(result.source, 'canonical-fallback');
    assert.equal(result.roundTexts.at(-1), 'answer');
  }
});
