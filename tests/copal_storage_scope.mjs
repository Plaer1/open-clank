import assert from 'node:assert/strict';
import { configureCopalStorage, copalStorageKey } from '../static/js/copal/storage.js';

assert.throws(() => copalStorageKey('odysseus-copal-view'), /not initialized/);

configureCopalStorage('local');
assert.equal(copalStorageKey('odysseus-copal-view'), 'odysseus-copal-view');
assert.equal(copalStorageKey('odysseus-copal-notes-layout', 'default'), 'odysseus-copal-notes-layout:default');

configureCopalStorage('user:alice');
const alice = copalStorageKey('odysseus-copal-notes-layout', 'default');
assert.equal(alice, 'odysseus-copal-notes-layout:scope:user%3Aalice:default');

configureCopalStorage('user:local');
assert.notEqual(copalStorageKey('odysseus-copal-view'), 'odysseus-copal-view');

configureCopalStorage('user:bob');
const bob = copalStorageKey('odysseus-copal-notes-layout', 'default');
assert.equal(bob, 'odysseus-copal-notes-layout:scope:user%3Abob:default');
assert.notEqual(alice, bob);
assert.notEqual(bob, 'odysseus-copal-notes-layout:default');

console.log('copal storage scope: ok');
