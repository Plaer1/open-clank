import test from 'node:test';
import assert from 'node:assert/strict';
import { FilesFacadeClient } from '../static/js/filesFacadeClient.js';

test('report backup uses the existing Files staged import and receipt contract', async () => {
  const requests = [];
  const client = new FilesFacadeClient({ fetchImpl: async (url, options) => {
    requests.push({ url, options });
    return new Response(JSON.stringify({
      operation_id:'op-report-1', generation:12, state:'complete',
      items:[{ item_id:'item-report-1', outcome:'committed', resource_ref:'opaque-resource-ref' }],
    }), { status:200, headers:{ 'content-type':'application/json' } });
  } });
  const file = new File(['{"version":1}'], 'report-progress.json', { type:'application/json' });
  const receipt = await client.importFile(file, {
    operationId:'op-report-1', itemId:'item-report-1', generation:12,
    destinationRef:'sealed-files-destination', name:file.name, relativePath:file.name,
    collision:'rename',
  });
  assert.equal(receipt.items[0].outcome, 'committed');
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/files-v1/imports');
  assert.equal(requests[0].options.credentials, 'same-origin');
  const form = requests[0].options.body;
  assert.equal(form.get('file').name, 'report-progress.json');
  assert.deepEqual(JSON.parse(form.get('metadata')), {
    operation_id:'op-report-1', item_id:'item-report-1', generation:12,
    destination_ref:'sealed-files-destination', name:'report-progress.json',
    relative_path:'report-progress.json', collision:'rename',
  });
});
