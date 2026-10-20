import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {parseMaxThreads, schedulerSummary, mountCodeIndexSettings} from '../../static/js/codeIndexSettings.js';

test('strict integer input does not coerce or truncate', () => {
  for (const value of ['1','2','32']) assert.equal(parseMaxThreads(value), Number(value));
  for (const value of ['', '0','33','1.5','1e1','02',' 2','2 ',true,2,null,'<script>']) assert.equal(parseMaxThreads(value), null);
});
test('readback distinguishes pending, drain, applied and unavailable from zero', () => {
  const data = {code_index_max_threads:2, code_index_settings_revision:3, code_index_scheduler:{native_status:'unavailable',status:'pending',effective:null,active:null,queued:null,native_revision:null,effective_revision:null,drain_pending:null}};
  assert.match(schedulerSummary(data), /Pending application.*Effective: unknown; active: unknown; queued: unknown/);
  Object.assign(data.code_index_scheduler,{native_status:'available',status:'draining',effective:4,active:2,queued:0,drain_pending:true});
  assert.match(schedulerSummary(data), /Draining active work.*Effective: 4; active: 2; queued: 0/);
  data.code_index_scheduler.status='applied'; assert.match(schedulerSummary(data), /Applied/);
  assert.doesNotMatch(schedulerSummary({code_index_scheduler:{native_status:'<script>',status:'pending'}}), /script/);
});
function fixture(fetcher) {
  const nodes = new Map();
  for (const id of ['threads','save','refresh','settings-status']) nodes.set(id,{value:'2',disabled:false,textContent:'',events:{},addEventListener(e,fn){this.events[e]=fn;},setAttribute(){},removeAttribute(){},focus(){this.focused=true;}});
  const root={dataset:{},querySelector(sel){return nodes.get(sel.slice('[data-code-index-'.length,-1));}};
  mountCodeIndexSettings(root,{fetcher});
  return {root,nodes};
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
test('one initial read, explicit save and refresh only; mounted once; exact POST whitelist', async () => {
  const calls=[]; const response={code_index_max_threads:2,code_index_scheduler:{native_status:'unavailable',status:'pending'}};
  const fetcher=async (url,options)=>{calls.push({url,...options});return {ok:true,json:async()=>response};};
  const {root,nodes}=fixture(fetcher); await settle();
  mountCodeIndexSettings(root,{fetcher}); assert.equal(calls.length,1);
  nodes.get('threads').value='3.5'; nodes.get('save').events.click(); await settle(); assert.equal(calls.length,1);
  nodes.get('threads').value='3'; nodes.get('save').events.click(); await settle();
  assert.deepEqual(JSON.parse(calls[1].body),{code_index_max_threads:3});
  assert.equal(calls[1].credentials,'same-origin');
  nodes.get('refresh').events.click(); await settle(); assert.equal(calls.length,3); assert.equal(calls[2].method,'GET');
});
test('forbidden and raw backend errors never render arbitrary text', async () => {
  const {nodes}=fixture(async()=>({ok:false,status:403,json:async()=>({error:'private path'})})); await settle();
  assert.equal(nodes.get('save').disabled,true); assert.equal(nodes.get('refresh').disabled,false);
  assert.doesNotMatch(nodes.get('settings-status').textContent,/private/);
});
test('in-flight read coalesces actions and network failure restores explicit refresh', async () => {
  let rejectRead; let calls=0;
  const {nodes}=fixture(()=>{calls++; return new Promise((_,reject)=>{rejectRead=reject;});});
  assert.equal(nodes.get('threads').disabled,true);
  nodes.get('save').events.click(); nodes.get('refresh').events.click(); assert.equal(calls,1);
  rejectRead(new Error('SECRET backend path')); await settle();
  assert.equal(nodes.get('threads').disabled,false); assert.equal(nodes.get('refresh').disabled,false);
  assert.doesNotMatch(nodes.get('settings-status').textContent,/SECRET/);
});
test('saved pending result never invents effective worker counts and Enter saves exact value', async () => {
  const calls=[];
  const {nodes}=fixture(async (_,options)=>{calls.push(options);return {ok:true,json:async()=>({code_index_max_threads:4,code_index_settings_revision:2,code_index_scheduler:{native_status:'unavailable',status:'pending',effective:null,active:null,queued:null}})};});
  await settle(); assert.equal(nodes.get('threads').value,'4');
  let prevented=false; nodes.get('threads').events.keydown({key:'Enter',preventDefault(){prevented=true;}}); await settle();
  assert.equal(prevented,true); assert.deepEqual(JSON.parse(calls[1].body),{code_index_max_threads:4});
  assert.match(nodes.get('settings-status').textContent,/Saved\/requested: 4.*Pending.*Effective: unknown/);
});
test('System markup and lazy hook expose searchable accessible settings without polling', () => {
  const html=readFileSync(new URL('../../static/index.html',import.meta.url),'utf8');
  assert.match(html,/data-code-index-settings/); assert.match(html,/Max indexing threads/); assert.match(html,/indexing workers/);
  const source=readFileSync(new URL('../../static/js/codeIndexSettings.js',import.meta.url),'utf8'); assert.doesNotMatch(source,/setInterval|setTimeout|innerHTML/);
  const settings=readFileSync(new URL('../../static/js/settings.js',import.meta.url),'utf8'); assert.match(settings,/tab === 'system'\) mountCodeIndexSettings/);
});
