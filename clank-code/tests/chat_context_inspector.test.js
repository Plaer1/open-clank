import {test, expect} from 'bun:test';
class Element extends EventTarget {
  constructor() { super(); this.children=[]; this.textContent=''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; this.textContent=''; }
  setAttribute() {} remove() {}
}
const tick = () => new Promise(r => setTimeout(r, 0));
test('inspector retains prior pages and discards stale account responses', async () => {
  const doc = new EventTarget(); doc.createElement=()=>new Element(); doc.body={dataset:{accountId:'a'}}; globalThis.document=doc;
  globalThis.window=new EventTarget(); let resolve, previewResolve, signal, url, calls=0, posted=[], failNextPage=true;
  const frame = {sequence:{attempt:1,transform:1},observedAt:1,model:{providerID:'p',modelID:'m',window:{hard:100,usable:0}},identity:{sessionID:'s'},components:{prompt:{state:'present',digest:'a'.repeat(64)}},sources:[{kind:'part',id:'x',revision:'r1',digest:'b'.repeat(64)}],omittedSourceCount:0};
  globalThis.fetch=(u,options)=>{url=u; signal=options.signal; calls++; if (calls===1) return Promise.resolve({ok:true,json:async()=>({availability:'available',items:[{messageID:'m',availability:'available',frames:[frame],omittedFrameCount:2}],nextCursor:'next'})}); if (options.method === 'POST') { const body=JSON.parse(options.body); posted.push(body); if(posted.length===1) return new Promise(r=>{previewResolve=r}); return Promise.resolve({ok:true,json:async()=>({preview:{version:1,action:body.action,parts:[]},applied:!!body.expected})}); } if(failNextPage) { failNextPage=false; return Promise.reject(new Error('offline')); } return new Promise(r=>{resolve=r})};
  const {mountChatContextInspector} = await import('../static/js/chatContextInspector.js');
  const container=new Element(), mounted=mountChatContextInspector(container,()=> 'chat');
  const [button,output,notice]=container.children[0].children; button.dispatchEvent(new Event('click')); await tick();
  expect(url).toContain('/api/orchestration/chat/context-observations?limit=5');
  expect(output.children[0].children[0].textContent).toContain('2 omitted');
  expect(output.children[0].children[1].children[1].textContent).toContain('usable window 0');
  const section=output.children[0].children[1], controls=section.children.at(-1);
  const [choice, preview, apply, status]=controls.children; choice.value='trim';
  preview.dispatchEvent(new Event('click')); choice.value='restore'; choice.dispatchEvent(new Event('change'));
  previewResolve({ok:true,json:async()=>({preview:{version:1,action:'trim',parts:[]}})}); await tick();
  expect(apply.disabled).toBe(true); expect(status.textContent).toContain('changed');
  choice.value='trim'; preview.dispatchEvent(new Event('click')); await tick(); expect(posted[1].action).toBe('trim'); expect(apply.disabled).toBe(false);
  apply.dispatchEvent(new Event('click')); await tick(); expect(posted[2].expected.version).toBe(1); expect(status.textContent).toContain('applied');
  button.dispatchEvent(new Event('click')); await tick(); expect(output.children.length).toBe(1); expect(notice.textContent).toContain('retry');
  button.dispatchEvent(new Event('click')); expect(url).toContain('cursor=next');
  doc.body.dataset.accountId='b'; doc.dispatchEvent(new Event('openclank:auth-context-changed'));
  expect(signal.aborted).toBe(true);
  resolve({ok:true,json:async()=>({availability:'available',items:[]})}); await tick();
  expect(output.children.length).toBe(0); expect(output.textContent).toBe('');
  mounted.dispose();
});
