import assert from 'node:assert/strict';
import { mediaInputNotice } from '../../static/js/mediaInputNotice.js';

const omit = (reason, extra = {}) => ({ media_input: { outcome: 'omit', reason, ...extra } });
// unchanged / admitted input: nothing new
assert.equal(mediaInputNotice({ media_input: { outcome: 'admit', reason: null, sent: true } }), null);
assert.equal(mediaInputNotice({}), null);
assert.equal(mediaInputNotice(null), null);
// every blocking reason shows a short plain-words state
for (const reason of ['too_large_bytes', 'too_many_pixels', 'side_too_long', 'route_without_vision']) {
  const text = mediaInputNotice(omit(reason, { sent: false }));
  assert.match(text, /^Not sent to the model: /, reason);
  assert.ok(!text.includes('_'), 'plain words, not the code: ' + reason);
}
assert.match(mediaInputNotice(omit('route_without_vision', { sent: false })), /Choose a model that accepts images/);
// upload-time receipt (no `sent`): blocking reasons show, non-blocking reasons do not
assert.ok(mediaInputNotice(omit('too_many_pixels')));
assert.equal(mediaInputNotice(omit('unsupported_type')), null);
assert.equal(mediaInputNotice(omit('type_mismatch')), null);
// an image that was sent anyway never shows an omitted state; unknown codes are ignored
assert.equal(mediaInputNotice(omit('unsupported_type', { sent: true })), null);
assert.equal(mediaInputNotice(omit('made_up', { sent: false })), null);
// retained-turn resume: the stored message metadata round-trips through JSON and renders the same state
const stored = JSON.parse(JSON.stringify(omit('too_many_pixels', { sent: false, width: 30000, height: 30000 })));
assert.equal(mediaInputNotice(stored), mediaInputNotice(omit('too_many_pixels', { sent: false, width: 30000, height: 30000 })));
console.log('media_input_notice ok');
