import assert from 'node:assert/strict';

import {
  WINDOW_SIZE_VERSION,
  normalizeWindowSizeRecord,
} from '../../static/js/windowResize.js';

assert.deepEqual(
  normalizeWindowSizeRecord({ w:900, h:700 }, { width:800, height:600, minWidth:560, minHeight:420 }),
  { version:WINDOW_SIZE_VERSION, width:800, height:600 },
);
assert.deepEqual(
  normalizeWindowSizeRecord({ version:99, width:10, height:20 }, { width:390, height:300, minWidth:560, minHeight:420 }),
  { version:WINDOW_SIZE_VERSION, width:390, height:300 },
);
assert.deepEqual(
  normalizeWindowSizeRecord({ version:2, width:640.4, height:480.6 }, { width:1200, height:900, minWidth:560, minHeight:420 }),
  { version:WINDOW_SIZE_VERSION, width:640, height:481 },
);
assert.equal(normalizeWindowSizeRecord({ width:'bad', height:400 }, { width:800, height:600 }), null);
assert.equal(normalizeWindowSizeRecord(null, { width:800, height:600 }), null);

console.log('window resize normalization tests passed');
