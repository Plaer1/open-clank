#!/usr/bin/env node

import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const requests = [];
const json = (res, value, status = 200) => {
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify(value));
  return true;
};

await withCopalBrowser({
  page: '<!doctype html><meta charset="utf-8"><title>Host template provider</title>',
  request: async (req, res) => {
    const url = new URL(req.url, 'http://fixture');
    if (url.pathname !== '/api/files-v1/create' && url.pathname !== '/api/files-v1/open-resource') return false;
    const body = await new Promise(resolve => {
      let value = '';
      req.on('data', chunk => { value += chunk; });
      req.on('end', () => resolve(value ? JSON.parse(value) : {}));
    });
    requests.push({ path: url.pathname, body });
    if (url.pathname.endsWith('/create')) {
      assert.equal(body.parent_ref, 'rr1.writable-folder');
      assert.equal(Object.hasOwn(body, 'path'), false);
      return json(res, { version: 1, action: 'create', resource: { ref: 'rr1.host-template' } });
    }
    assert.deepEqual(body, { resource_ref: 'rr1.host-template' });
    return json(res, {
      version: 1,
      target: { app: 'editor' },
      payload: {
        name: 'Meeting.md', text: '# {{title}}\n', representation: 'markdown',
        resource: {
          key: { accountId: 'account-alice', workspaceId: 'host', provider: 'host', resourceId: 'resource-1' },
          revision: { kind: 'hostFingerprint', value: 'fp-1' },
          locator: { displayName: 'Meeting.md', locationLabel: 'Meeting.md' },
          representation: 'markdown', capabilities: { read: true, edit: true },
        },
        parent_resource_ref: 'rr1.writable-folder',
      },
    });
  },
}, async ({ evaluate }) => {
  const result = await evaluate(`(async () => {
    const { filesFacadeClient } = await import('/static/js/filesFacadeClient.js?host-template-provider=1');
    const created = await filesFacadeClient.createResource('rr1.writable-folder', { name:'Meeting.md', text:'# {{title}}\\n', actionId:'template-create-1' });
    const opened = await filesFacadeClient.openResource(created.resource.ref);
    return { created:created.resource.ref, opened:opened.payload.parent_resource_ref, text:opened.payload.text };
  })()`);
  assert.deepEqual(result, { created: 'rr1.host-template', opened: 'rr1.writable-folder', text: '# {{title}}\n' });
  assert.equal(requests.length, 2);
});

process.stdout.write(JSON.stringify({ lifecycle: 'pass', requests: requests.map(item => item.path) }) + '\n');
