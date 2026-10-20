import assert from 'node:assert/strict';
import {observeCodeIndex,indexPresentation} from '../../static/js/copal/codeIndexObserver.js';
const settle=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
function fixture(request){
  const calls=[],statuses=[],timers=new Map(),delays=[];let id=0,current=true;
  const release=observeCodeIndex({request:event=>{calls.push(event);return request(event);},
    isCurrent:()=>current,onStatus:s=>statuses.push(s),
    schedule:(fn,delay)=>{delays.push(delay);timers.set(++id,fn);return id;},unschedule:id=>timers.delete(id)});
  return {release,calls,statuses,timers,delays,invalidate:()=>{current=false;},
    async tick(){const [id,fn]=timers.entries().next().value||[];assert(fn);timers.delete(id);fn();await settle();}};
}
// No total build deadline: even a long build keeps one bounded-backoff poll.
{
  let n=0;const f=fixture(async()=>++n===1?{lifecycle:'queued'}:n<40?{lifecycle:'building',progress:{completed_files:n,total_files:50}}:{lifecycle:'ready',head_generation:'g'});
  await settle();for(let i=0;i<39;i++){assert.equal(f.timers.size,1);await f.tick();}
  assert.equal(f.calls.filter(x=>x==='open').length,1);assert.equal(f.calls.filter(x=>x==='status').length,39);
  assert.equal(f.timers.size,0);assert(Math.max(...f.delays)<=10000);assert.equal(f.statuses.at(-1).lifecycle,'ready');f.release();await settle();assert.equal(f.calls.at(-1),'release');
}
// Disposal before ACK releases twice, neither ACK nor old polls repaint.
{
  const open=deferred();const f=fixture(event=>event==='open'?open.promise:Promise.resolve({}));
  f.release();await settle();open.resolve({lifecycle:'queued'});await settle();
  assert.deepEqual(f.calls,['open','release','release']);assert.equal(f.statuses.length,0);assert.equal(f.timers.size,0);
}
// Scope invalidation and one in-flight status, including repeated refresh clicks.
{
  const pending=deferred();const f=fixture(event=>Promise.resolve(event==='status'?pending.promise:{lifecycle:'queued'}));
  await settle();const tick=f.tick();await settle();void f.release.refresh();assert.equal(f.calls.filter(x=>x==='status').length,1);
  f.invalidate();pending.resolve({lifecycle:'ready',head_generation:'g'});await tick;await settle();
  assert.equal(f.statuses.length,1);assert.equal(f.calls.at(-1),'release');assert.equal(f.timers.size,0);
}
// Ensure rejection resolves an erased tombstone with status, never auto-admit.
{
  const f=fixture(async event=>{if(event==='open')throw Object.assign(new Error('Rejected'),{status:409});return {lifecycle:event==='admit'?'queued':'erased'};});
  await settle();assert.deepEqual(f.calls,['open','status']);assert.equal(f.statuses.at(-1).lifecycle,'erased');assert.equal(f.timers.size,0);
  await f.release.readmit();assert.deepEqual(f.calls,['open','status','admit']);assert.equal(f.timers.size,1);f.release();
}
// Releasing one subscriber never invokes cancel or removes another consumer.
{
  const events=[];const request=async event=>{events.push(event);return {lifecycle:'ready',head_generation:'g'};};
  const a=fixture(request),b=fixture(request);await settle();a.release();await settle();assert.deepEqual(events,['open','open','release']);assert.equal(b.statuses.length,1);b.release();
}
for(const lifecycle of ['failed','erased','cancelled','denied','unavailable','missing']){
  const f=fixture(async()=>({lifecycle}));await settle();assert.equal(f.timers.size,0);assert.equal(indexPresentation({lifecycle}).usable,false);f.release();
}
// Only a witnessed pending build can recover STATUS transport failures. The
// unavailable projection is never a head or a fake pending lifecycle.
{
 let n=0;const f=fixture(async event=>{if(event==='open')return {lifecycle:'building'};if(event==='status'&&++n===1)throw Object.assign(new Error('Gateway timeout'),{status:504});return {lifecycle:'ready',head_generation:'same-head'};});
 await settle();await f.tick();const failed=f.statuses.at(-1),p=indexPresentation(failed);
 assert.equal(failed.lifecycle,'unavailable');assert.equal(p.recovering,true);assert.equal(p.pending,false);assert.equal(p.usable,false);assert.match(p.text,/temporarily unavailable; rechecking/);
 assert.equal(f.timers.size,1);await f.tick();assert.equal(f.statuses.at(-1).head_generation,'same-head');assert.equal(f.timers.size,0);
 assert.deepEqual(f.calls,['open','status','status']);f.release();
}
// Transient status outages never impose a job deadline or failure-count cap.
for(const status of [408,429,502,503,504,'network']){
 const f=fixture(async event=>{if(event==='open')return {lifecycle:'queued'};throw status==='network'?new TypeError('Failed to fetch'):Object.assign(new Error('Temporary'),{status});});
 await settle();for(let i=1;i<=40;i++){await f.tick();assert.equal(f.statuses.at(-1).recovering,true);assert.equal(f.timers.size,1);}
 assert.equal(f.calls.filter(e=>e==='open').length,1);assert.equal(f.calls.filter(e=>e==='status').length,40);assert.equal(indexPresentation(f.statuses.at(-1)).usable,false);assert(Math.max(...f.delays)<=10000);assert.equal(f.delays.at(-1),10000);f.release();await settle();assert.equal(f.timers.size,0);
}
// A long outage can become ready without a new open, even across valid pending
// observations. Valid terminal/invalid projections cannot grant recovery.
{
 let n=0;const f=fixture(async event=>{if(event==='open')return {lifecycle:'building'};n++;if(n===10)return {lifecycle:'building'};if(n===41)return {lifecycle:'ready',head_generation:'published-after-outage'};throw Object.assign(new Error('Timeout'),{status:504});});
 await settle();for(let i=1;i<=40;i++)await f.tick();assert.equal(f.statuses.at(-1).recovering,true);await f.tick();assert.equal(f.timers.size,0);assert.equal(f.statuses.at(-1).head_generation,'published-after-outage');assert.equal(f.calls.filter(e=>e==='open').length,1);assert(!f.calls.includes('admit'));f.release();
}
for(const terminal of [null,{},[],{lifecycle:'unknown'},...['failed','erased','cancelled','denied','unavailable','missing'].map(lifecycle=>({lifecycle,recovering:true}))]){
 const f=fixture(async event=>event==='open'?{lifecycle:'building'}:terminal);await settle();await f.tick();assert.equal(f.timers.size,0);assert.notEqual(f.statuses.at(-1).recovering,true);f.release();
}
// Authorization and nontransient errors stay terminal even after building.
for(const status of [400,401,403,404,409,422,500]){
 const f=fixture(async event=>{if(event==='open')return {lifecycle:'building'};throw Object.assign(new Error('Denied or permanent'),{status});});await settle();await f.tick();assert.equal(f.timers.size,0);assert.notEqual(f.statuses.at(-1).recovering,true);f.release();
}
for(const code of ['denied','revoked','access_revoked','permission_denied','erased','cancelled','failed','invalid_response']){
 const f=fixture(async event=>{if(event==='open')return {lifecycle:'building'};throw Object.assign(new Error('Explicit terminal authority'),{status:504,code});});await settle();await f.tick();assert.equal(f.timers.size,0);assert.notEqual(f.statuses.at(-1).recovering,true);f.release();
}
// Open/admit failure or a status following ready has no pending-build authority.
{
 const f=fixture(async()=>{throw Object.assign(new Error('Timeout'),{status:504});});await settle();assert.deepEqual(f.calls,['open']);assert.equal(f.timers.size,0);f.release();
 const ready=fixture(async event=>{if(event==='open')return {lifecycle:'ready',head_generation:'g'};throw Object.assign(new Error('Timeout'),{status:504});});await settle();await ready.release.refresh();assert.equal(ready.timers.size,0);assert.notEqual(ready.statuses.at(-1).recovering,true);ready.release();
 const erased=fixture(async event=>{if(event==='open')return {lifecycle:'erased'};throw Object.assign(new Error('Timeout'),{status:504});});await settle();await erased.release.readmit();assert.equal(erased.timers.size,0);assert.deepEqual(erased.calls,['open','admit']);erased.release();
}
// Recovery timers/inflight work respect scope/disposal and never reopen or
// consume another subscriber; refresh cannot overlap the recovering status.
for(const dispose of [false,true]){
 const waiting=deferred();let n=0;const f=fixture(event=>{if(event==='open')return Promise.resolve({lifecycle:'building'});if(event==='status'&&++n===1)return Promise.reject(Object.assign(new Error('Timeout'),{status:504}));return event==='status'?waiting.promise:Promise.resolve({});});
 await settle();await f.tick();const tick=f.tick();await settle();void f.release.refresh();assert.equal(f.calls.filter(e=>e==='status').length,2);
 if(dispose)f.release();else f.invalidate();waiting.resolve({lifecycle:'ready',head_generation:'late'});await tick;await settle();assert.equal(f.statuses.length,2);assert.equal(f.timers.size,0);assert.equal(f.calls.filter(e=>e==='open').length,1);assert(!f.calls.includes('admit'));assert.equal(f.calls.at(-1),'release');
}
{
 const f=fixture(async event=>{if(event==='open')return {lifecycle:'building'};throw Object.assign(new Error('Timeout'),{status:504});});await settle();await f.tick();f.release();await settle();assert.equal(f.timers.size,0);assert.deepEqual(f.calls,['open','status','release']);
}
assert.equal(indexPresentation({lifecycle:'ready',coverage_complete:true}).usable,false,'coverage completeness is not a head');
assert.equal(indexPresentation({lifecycle:'ready',head_generation:'g',freshness:'partial'}).partial,true);
assert.match(indexPresentation({lifecycle:'failed',head_generation:'old'}).text,/previous indexed head/);
assert.match(indexPresentation({lifecycle:'building',progress:{completed_files:2,total_files:9}}).text,/2\/9 files/);
console.log('Code index observer: cold build, backoff, isolation, tombstone and disposal contracts passed');
