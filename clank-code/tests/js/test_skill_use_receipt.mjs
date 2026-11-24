import assert from 'node:assert/strict';
import { skillUseReceipt, renderSkillUseReceipt } from '../../static/js/skillUseReceipt.js';

const use = {version: 1, name: 'test<skill>', callID: 'call-1', bodySHA256: 'a'.repeat(64), source: '/old/SKILL.md'};
const event = {id: 'call-1', type: 'tool_output', status: 'completed', exit_code: 0,
  data: {toolCallId: 'call-1', rawOutput: {metadata: {skillUse: use}, output: 'original output'}}};
const esc = s => String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;');
assert.equal(skillUseReceipt(event, {live: true}).bodySHA256, use.bodySHA256);
assert.match(renderSkillUseReceipt(event, esc, {live: true}), /test&lt;skill>/);
assert.equal(renderSkillUseReceipt({...event, id: 'call-2'}, esc, {live: true}), '');
assert.equal(renderSkillUseReceipt({...event, status: 'failed'}, esc, {live: true}), '');
const history = {id:'call-1', exit_code:0, structured:event.data, output:event.data.rawOutput.output};
assert.equal(skillUseReceipt(history).source, '/old/SKILL.md');
console.log('live and historical skill receipt projection passed');
