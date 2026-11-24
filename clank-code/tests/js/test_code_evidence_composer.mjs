import assert from 'node:assert/strict';
import { appendCodeEvidenceCAS,codeEvidenceCard } from '../../static/js/codeEvidenceComposer.js';
const evidence={schema:'open-clank.code-resource/v1',state:'current',validation_kind:'current_source',current_source_verified:true,workspace_id:'graph-ws',receipt:{workspace_id:'graph-ws',checkout_id:'checkout',head_generation:'g'},source:{rel_path:'src/a.py',source_hash:'a'.repeat(64),span:{start_byte:0,end_byte:4,start_line:1,end_line:1}},excerpt:'code'};
const attachments=['restored'],pending=[{name:'pending.png'}];
const expected={sid:'chat',owner:'alice',epoch:1,version:3,text:'Existing draft',attachments,pending,authEpoch:1};
let actual={...expected},commits=[],submits=0,current=true;
const run=(extra={})=>appendCodeEvidenceCAS(evidence,{expected,read:()=>actual,commit:text=>commits.push(text),isCurrent:()=>current,submit:()=>submits++,...extra});
assert(run().ok);assert.match(commits[0],/^Existing draft\n\n\[Untrusted code evidence/);assert.equal(submits,0);
assert.equal(actual.attachments,attachments);assert.equal(actual.pending,pending);
assert(run({ask:true}).ok);assert.equal(submits,1);
for(const key of ['sid','owner','epoch','version','text','authEpoch','attachments']){actual={...expected,[key]:key==='attachments'?[]:'changed'};assert.equal(run().ok,false);}
actual={...expected,pending:[{name:'pending.png'}]};assert.equal(run().ok,false);
actual={...expected};current=false;assert.equal(run({ask:true}).ok,false);assert.equal(submits,1);current=true;
for(const override of [{state:'stale'},{current_source_verified:false},{receipt:null},{excerpt:'x'.repeat(3000)}])assert.equal(codeEvidenceCard({...evidence,...override}),null);
assert.match(codeEvidenceCard(evidence),/Workspace: graph-ws/); // Need not equal chat workspace.
console.log('S07 composer CAS, explicit Ask, citations and attachment preservation passed');
