import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createInsertionDescriptors,
  deserializeInsertionDescriptors,
  deserializeTemplateDescriptors,
  deserializeTemplateHandoff,
  expandTemplate,
  formatTemplateDate,
  isInTemplateFolder,
  normalizeTemplateFolder,
  normalizeTemplateFolderSelection,
  serializeInsertionDescriptors,
  serializeTemplateDescriptors,
  serializeTemplateHandoff,
} from '../../static/js/copal/templateModel.js';

const fixed = new Date('2026-01-02T03:04:05.000Z');

test('core template variables and documented date formats are deterministic', () => {
  assert.equal(formatTemplateDate('YYYY/MM/DD HH:mm:ss', fixed, 'UTC'), '2026/01/02 03:04:05');
  const result = expandTemplate('{{title}} {{date}} {{time}} {{date:YYYY/MM}} {{unknown}}', { title: '日本語', now: fixed, timeZone: 'UTC' });
  assert.equal(result.text, '日本語 2026-01-02 03:04 2026/01 {{unknown}}');
  assert.match(result.diagnostics.join(' '), /Unsupported template variable/);
});

test('unknown formats and optional scripting stay inert with diagnostics', () => {
  const result = expandTemplate('{{date:YYYY/INVALID}} <% await run() %>', { now: fixed, timeZone: 'UTC' });
  assert.equal(result.text, '{{date:YYYY/INVALID}} <% await run() %>');
  assert.match(result.diagnostics.join(' '), /Unsupported template date format/);
  assert.match(result.diagnostics.join(' '), /Executable template expressions/);
});

test('configured folder identity uses boundary semantics and rejects traversal', () => {
  assert.equal(normalizeTemplateFolder(' Projects//Obsidian/Templates/ '), 'Projects/Obsidian/Templates');
  assert.equal(isInTemplateFolder('Projects/Obsidian/Templates', 'Projects/Obsidian/Templates'), true);
  assert.equal(isInTemplateFolder('Projects/Obsidian/Templates/a.md', 'Projects/Obsidian/Templates'), true);
  assert.equal(isInTemplateFolder('Projects/Obsidian/Templates-old/a.md', 'Projects/Obsidian/Templates'), false);
  assert.throws(() => normalizeTemplateFolder('../Templates'));
  assert.throws(() => normalizeTemplateFolder('/Templates'));
  assert.throws(() => normalizeTemplateFolder('Templates\\Nested'));
  assert.deepEqual(normalizeTemplateFolderSelection({ resource_ref: 'rr', logical_path: 'Templates', capabilities: ['read', 'stat', 'children'] }), {
    resourceRef: 'rr', resourceKey: null, revision: null, provider: null, kind: 'folder', logicalPath: 'Templates', capabilities: { read: true, stat: true, children: true },
  });
  assert.throws(() => normalizeTemplateFolderSelection({ resource_ref: 'rr', logical_path: 'Templates', capabilities: ['read', 'stat', 'children'] }, { purpose: 'create' }));
  assert.equal(normalizeTemplateFolderSelection({ resource_ref: 'rr', logical_path: 'Templates', capabilities: { read: true, stat: true, children: true, write: true } }, { purpose: 'create' }).resourceRef, 'rr');
  assert.equal(normalizeTemplateFolderSelection({ resource_ref: 'rr', kind: 'directory', logical_path: 'Templates', capabilities: ['read', 'stat', 'children'] }).kind, 'directory');
  assert.equal(normalizeTemplateFolderSelection({ resource_ref: 'rr', kind: 'provider_root', logical_path: '', capabilities: ['read', 'stat', 'children'] }).kind, 'provider_root');
  assert.throws(() => normalizeTemplateFolderSelection({ resource_ref: 'rr', kind: 'file', logical_path: 'Templates', capabilities: ['read', 'stat', 'children'] }));
  assert.throws(() => normalizeTemplateFolderSelection({ resource_ref: 'rr', kind: 'unknown', logical_path: 'Templates', capabilities: ['read', 'stat', 'children'] }));
  assert.throws(() => normalizeTemplateFolderSelection({ resource_ref: 'rr', logical_path: 'Templates', capabilities: ['read', 'stat', 'children'] }, { purpose: 'x'.repeat(33) }));
  assert.throws(() => normalizeTemplateFolderSelection({ resource_ref: 'rr', logical_path: '日'.repeat(2049), capabilities: ['read', 'stat', 'children'] }));
  assert.throws(() => normalizeTemplateFolderSelection({ resource_ref: 'rr', logical_path: 'Templates', capabilities: ['read', 'stat', 'children', 'é'.repeat(33)] }));
  assert.throws(() => normalizeTemplateFolderSelection({ resource_ref: 'rr', logical_path: 'Templates', capabilities: new Array(257).fill('read') }));
});

test('multicursor descriptors are stable and round trip', () => {
  const descriptors = createInsertionDescriptors('Hi {{title}}', [{ from: 8, to: 8 }, { from: 2, to: 4 }], { title: 'Entry', now: fixed, timeZone: 'UTC' });
  assert.deepEqual(descriptors.map(item => [item.descriptorIndex, item.selectionIndex, item.from, item.to, item.text]), [
    [0, 1, 2, 4, 'Hi Entry'], [1, 0, 8, 8, 'Hi Entry'],
  ]);
  assert.deepEqual(deserializeInsertionDescriptors(serializeInsertionDescriptors(descriptors)), descriptors);
  assert.throws(() => createInsertionDescriptors('x', [{ from: NaN, to: 1 }]));
  assert.throws(() => createInsertionDescriptors('x', [{ from: 1, to: 3 }, { from: 2, to: 4 }]));
  assert.deepEqual(createInsertionDescriptors('x', [{ from: 4, to: 2 }], { title: 'x' })[0].from, 2);
});

test('each preserved unsupported placeholder gets one diagnostic', () => {
  const result = expandTemplate('{{unknown}} {{date:INVALID}} {{unknown}}', { now: fixed, timeZone: 'UTC' });
  assert.equal(result.diagnostics.filter(item => String(item).includes('Unsupported template variable: {{unknown}}')).length, 1);
});

test('S01 asset and link descriptors serialize as opaque descriptors', () => {
  const encoded = serializeTemplateDescriptors({
    assets: [{ resource_ref: 'asset-ref', revision: { kind: 'hostFingerprint', value: 'v1' }, url: '/must-not-be-invented' }],
    links: [{ resource_key: 'note-key', target: 'Note' }],
  });
  assert.deepEqual(deserializeTemplateDescriptors(encoded), {
    version: 1,
    assets: [{ resource_ref: 'asset-ref', revision: { kind: 'hostFingerprint', value: 'v1' } }],
    links: [{ resource_key: 'note-key', target: 'Note' }],
  });
});

test('descriptor bounds and UTF-8 payload limits reject unsafe handoffs', () => {
  assert.throws(() => createInsertionDescriptors('x', [{ from: -10, to: -2 }]));
  assert.throws(() => createInsertionDescriptors('x', [{ from: 10000001, to: 10000002 }]));
  assert.throws(() => deserializeInsertionDescriptors({ version: 1, descriptors: [{
    descriptorVersion: 1, descriptorIndex: 0, selectionIndex: 0, from: -1, to: 0, text: 'x', diagnostics: [],
  }] }));
  assert.throws(() => deserializeInsertionDescriptors({ version: 1, descriptors: [{
    descriptorVersion: 1, descriptorIndex: 0, selectionIndex: 0, from: 0, to: 0, text: 'x', diagnostics: [{ message: 'bad', offset: -1 }],
  }] }));
  assert.throws(() => expandTemplate('日本語'.repeat(100000)));
  assert.throws(() => serializeTemplateHandoff({ resourceRef: 'rr', logicalPath: 'Templates', provider: { url: 'file:///secret' }, capabilities: ['read', 'stat', 'children'] }));
  const handoff = serializeTemplateHandoff({ resourceRef: 'rr', logicalPath: 'Templates', capabilities: ['read', 'stat', 'children'] });
  assert.equal(deserializeTemplateHandoff(handoff).folder.resourceRef, 'rr');
});

test('template handoff retains Files account/workspace and policy scope', () => {
  const encoded = serializeTemplateHandoff({
    resourceRef: 'rr', resourceKey: 'host:key', provider: 'host', logicalPath: 'Templates',
    accountScope: 'account-a', workspaceScope: 'workspace-a', generation: 4, policyGeneration: 9,
    capabilities: ['read', 'stat', 'children'],
  });
  assert.deepEqual(deserializeTemplateHandoff(encoded).folder, {
    resourceRef: 'rr', resourceKey: 'host:key', revision: null, provider: 'host', kind: 'folder',
    logicalPath: 'Templates', capabilities: { read: true, stat: true, children: true },
    accountId: 'account-a', workspaceId: 'workspace-a', generation: 4, policyGeneration: 9,
  });
  assert.throws(() => serializeTemplateDescriptors({ links: [{ target: 'https://example.test/note' }] }));
});
