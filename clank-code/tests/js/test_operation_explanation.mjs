import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {exactOperationDetails,verifiedOperationExplanation,hasAdditionalOperationDetails} from '../../static/js/operationExplanation.js';
globalThis.crypto ??= webcrypto;
const detail={filepath:'/tmp/x',command:'echo "雪" | cat > x',workdir:'/tmp',argv:['sh','-c','echo "雪" | cat > x']};
const payload={request_id:'perm',session_id:'chat',turn_id:'turn',revision:7,permission_type:'bash',detail,explanation:{version:1,request_id:'perm',session_id:'chat',turn_id:'turn',revision:7,action_sha256:'308cad011c31b641a52d2015478fad330904d714bea37adf395e73bd04688d9d',intent:'unknown',destructive:'unknown',network:'unknown',privilege:'unknown',recovery:'unknown',grant:'depends_on_choice'}};
const lines=exactOperationDetails(detail);assert(lines.some(x=>x.includes(detail.command)) && lines.some(x=>x.includes(detail.filepath)) && lines.some(x=>x.includes(detail.workdir)) && lines.some(x=>x.startsWith('Argv:')));
assert.deepEqual(await verifiedOperationExplanation(payload,'chat'),{intent:'unknown'});
assert.equal(await verifiedOperationExplanation({...payload,detail:{...detail,workdir:'/changed'}},'chat'),null);
assert.equal(await verifiedOperationExplanation(payload,'other-chat'),null);
assert.equal(await verifiedOperationExplanation({...payload,explanation:{...payload.explanation,intent:'requested_file_edit'}},'chat'),null);
console.log('exact action display and bound cross-language explanation passed');
const numericKeys={...payload,detail:{'2':{z:2,'10':1},'10':'snow'},explanation:{...payload.explanation,action_sha256:'7a8bd3595154675e237dbb34eb4e088d10ea3ad9de5b6a32474a74d2c19797bd'}};
assert.deepEqual(await verifiedOperationExplanation(numericKeys,'chat'),{intent:'unknown'});
assert.equal(await verifiedOperationExplanation({...payload,detail:{command:'x'.repeat(65537)}},'chat'),null);

assert(exactOperationDetails({command:{unexpected:'value'},filepath:'/tmp/x'}).some(line=>line.includes('unexpected')));

assert(hasAdditionalOperationDetails({command:'echo x',actions:['unknown']}));
assert(!hasAdditionalOperationDetails({command:'echo x',argv:[1]}));
