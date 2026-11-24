import assert from 'node:assert/strict';
import { configureCopalStorage, copalStorageKey } from '../static/js/copal/storage.js';

assert.throws(() => copalStorageKey('openclank-copal-view'), /not initialized/);

configureCopalStorage('local');
assert.equal(copalStorageKey('openclank-copal-view'), 'openclank-copal-view');
assert.equal(copalStorageKey('openclank-copal-notes-layout', 'default'), 'openclank-copal-notes-layout:default');

configureCopalStorage('user:alice');
const alice = copalStorageKey('openclank-copal-notes-layout', 'default');
assert.equal(alice, 'openclank-copal-notes-layout:scope:user%3Aalice:default');

configureCopalStorage('user:local');
assert.notEqual(copalStorageKey('openclank-copal-view'), 'openclank-copal-view');

configureCopalStorage('user:bob');
const bob = copalStorageKey('openclank-copal-notes-layout', 'default');
assert.equal(bob, 'openclank-copal-notes-layout:scope:user%3Abob:default');
assert.notEqual(alice, bob);
assert.notEqual(bob, 'openclank-copal-notes-layout:default');

console.log('copal storage scope: ok');
