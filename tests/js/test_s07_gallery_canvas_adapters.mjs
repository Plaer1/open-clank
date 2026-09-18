import assert from 'node:assert/strict';
import test from 'node:test';

import {
  CANVAS_MAX_IMPORT_BYTES,
  FILES_CANVAS_IMAGE_MIME,
  classifyCanvasDrop,
  canvasDropOperationKey,
  createFilesCanvasImagePayload,
} from '../../static/js/editor/clipboard-and-drop.js';

const image = (overrides = {}) => ({ name: 'photo.png', type: 'image/png', size: 32, lastModified: 4, ...overrides });

test('Canvas consumes supported OS image drops and classifies bounded metadata', () => {
  const result = classifyCanvasDrop({ files: [image()], items: [], types: ['Files'] });
  assert.equal(result.ok, true);
  assert.equal(result.kind, 'os-image');
  assert.equal(result.files.length, 1);
});

test('Canvas consumes folders and text with visible bounded reasons', () => {
  const folder = classifyCanvasDrop({ files: [], items: [{ webkitGetAsEntry: () => ({ isDirectory: true }) }], types: ['Files'] });
  assert.equal(folder.code, 'directory');
  assert.match(folder.reason, /folders/i);
  const text = classifyCanvasDrop({ files: [], items: [{ kind: 'string' }], types: ['text/plain'] });
  assert.equal(text.code, 'text');
  assert.match(text.reason, /text and code/i);
});

test('Canvas rejects unsupported and oversized OS media before decoding', () => {
  assert.equal(classifyCanvasDrop({ files: [image({ type: 'application/pdf' })], items: [], types: ['Files'] }).code, 'unsupported');
  assert.equal(classifyCanvasDrop({ files: [image({ size: CANVAS_MAX_IMPORT_BYTES + 1 })], items: [], types: ['Files'] }).code, 'oversize');
});

test('Files image payload requires dedicated typed authority and stable revision', () => {
  const payload = {
    version: 1, type: FILES_CANVAS_IMAGE_MIME, resource_ref: 'rr.current', resource_key: 'account-1|workspace-1|host:image-1',
    account: 'account-1', workspace: 'workspace-1', provider: 'host',
    mime_type: 'image/png', revision: { kind: 'hostFingerprint', value: 'fp-1' },
    size: 32, capabilities: ['read', 'download'], operation_id: 'op-1', item_id: 'item-1',
    gesture_context: {
      commandId:'op-1', generation:1, policyGeneration:1, selectionEpoch:1,
      owner:'account-1', workspace:'workspace-1', pane:'files-window', provider:'host', parent:'rr-root', scopeKey:'account-1|workspace-1|host|rr-root||',
      selectedKeys:['account-1|workspace-1|host:image-1'], sourceCapabilities:{'account-1|workspace-1|host:image-1':{read:true,open:false,download:true,export:false}}, allowCopy:true, allowMove:false,
    },
  };
  const result = classifyCanvasDrop({
    types: [FILES_CANVAS_IMAGE_MIME],
    getData: () => JSON.stringify(payload),
  });
  assert.equal(result.ok, true);
  assert.equal(result.ref, 'rr.current');
  assert.equal(canvasDropOperationKey(payload), 'files:op-1:item-1');
  assert.equal(classifyCanvasDrop({ types: [FILES_CANVAS_IMAGE_MIME], getData: () => JSON.stringify({ ...payload, capabilities: ['read'] }) }).code, 'unauthorized');
  assert.equal(classifyCanvasDrop({ types: ['application/x-openclank-files-image'], getData: () => JSON.stringify(payload) }).code, 'invalid_payload');
  assert.equal(classifyCanvasDrop({ types: [FILES_CANVAS_IMAGE_MIME], getData: () => '{"version":1,"version":1}' }).code, 'invalid_payload');
});

test('OS drops have deterministic idempotency keys for repeated delivery', () => {
  const file = image();
  assert.equal(canvasDropOperationKey({ files: [file] }), canvasDropOperationKey({ files: [file] }));
});

test('Files image DTO preserves current ref and revision without inventing a layer ref', () => {
  const payload = createFilesCanvasImagePayload({
    ref: 'rr.current', resource_key: 'account-1|workspace-1|host:image-1', revision: { kind: 'hostFingerprint', value: 'fp-1' },
    mime_type: 'image/png', size: 12, provider: 'host', capabilities: ['read', 'download'],
  }, { account: 'account-1', workspace: 'workspace-1', operationId: 'op-2', itemId: 'item-2', context: {
    commandId:'op-2', generation:1, policyGeneration:1, selectionEpoch:1, owner:'account-1', workspace:'workspace-1', pane:'files-window', provider:'host', parent:'rr-root', scopeKey:'account-1|workspace-1|host|rr-root||', selectedKeys:['account-1|workspace-1|host:image-1'], sourceCapabilities:{'account-1|workspace-1|host:image-1':{read:true,open:false,download:true,export:false}}, allowCopy:true, allowMove:false,
  } });
  assert.equal(payload.type, FILES_CANVAS_IMAGE_MIME);
  assert.equal(payload.resource_ref, 'rr.current');
  assert.equal(payload.revision.value, 'fp-1');
  assert.deepEqual(payload.capabilities, ['read', 'download']);
  assert.equal(Object.hasOwn(payload, 'layer_id'), false);
});
