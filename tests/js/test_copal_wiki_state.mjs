import assert from 'node:assert/strict';
import { normalizeWikiPresentation, serializeWikiPresentation, moveWikiCard, closeWikiCard } from '../../static/js/copal/wikiState.js';

const first = normalizeWikiPresentation({}, ['a','b','c','d'], ['a','b','c']);
assert.deepEqual(first.story, ['a','b','c']);
const empty = normalizeWikiPresentation({ version:1, initialized:true, story:[], pinned:[], editing:[] }, ['a','b'], ['a','b']);
assert.deepEqual(empty.story, [], 'an explicitly empty story stays empty');
const restored = normalizeWikiPresentation({ version:1, initialized:true, story:['b','gone','b','a'], pinned:['b','gone'], editing:['a'], cards:{ b:{scrollTop:12,selectionStart:3,selectionEnd:5}, gone:{scrollTop:99} }, libraryScrollTop:7, storyScrollLeft:9 }, ['a','b'], []);
assert.deepEqual(restored.story, ['b','a']);
assert.deepEqual(restored.pinned, ['b']);
assert.deepEqual(restored.cards, { b:{scrollTop:12,selectionStart:3,selectionEnd:5} });
assert.deepEqual(moveWikiCard(restored.story, 'a', -1), ['a','b']);
assert.deepEqual(closeWikiCard(restored, 'b').story, ['b','a'], 'pinned card remains open');
assert.deepEqual(closeWikiCard({ ...restored, pinned:[] }, 'b').story, ['a']);
assert.deepEqual(JSON.parse(serializeWikiPresentation(empty)).story, []);
assert(!serializeWikiPresentation(restored).includes('body'), 'presentation state carries no document content');
console.log('Copal Wiki presentation state passed');
