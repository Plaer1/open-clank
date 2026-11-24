import { test, expect, mock } from 'bun:test';
mock.module('../static/js/workspace.js', () => ({ getWorkspaceId: () => 'workspace' }));
class Element extends EventTarget {
  constructor(tag) { super(); this.tag = tag; this.children = []; this.isConnected = true; this.textContent = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  remove() { this.isConnected = false; }
}
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const row = (source, id) => ({sourceRef:{kind:source,nativeId:id}, title:id, displayCategory:source === 'scheduler_routine' ? 'routine' : 'execution', nativeStatus:'running', links:[]});
const response = (source, items, cursor) => ({ok:true, json:async () => ({sources:{[source]:{availability:cursor?'partial':'available', observedAt:1, items, ...(cursor ? {nextCursor:cursor} : {})}}})});
test('mounted workload fences auth changes and repeated source cursors', async () => {
  const doc = new EventTarget(); doc.createElement = tag => new Element(tag); doc.visibilityState = 'visible'; doc.body = {dataset:{accountId:'a'}};
  globalThis.document = doc;
  const win = new EventTarget(); win.sessionModule = {getCurrentSessionId: () => 'chat'}; globalThis.window = win;
  const requests = []; let delayed, appendResolve;
  globalThis.fetch = (url, options) => {
    requests.push({url, options}); const source = url.split('/observations/')[1].split('?')[0];
    if (source === 'native_actor' && requests.length === 2) return new Promise(resolve => { delayed = resolve; });
    if (source === 'scheduler_routine' && url.includes('cursor=')) return new Promise(resolve => { appendResolve = resolve; });
    return Promise.resolve(response(source, [row(source, 'one')], source === 'scheduler_routine' ? 'next' : null));
  };
  const {mountTaskWorkload} = await import('../static/js/taskWorkload.js');
  const container = new Element('div'), view = mountTaskWorkload(container);
  await tick(); expect(requests).toHaveLength(5);
  expect(requests.every(r => r.url.includes('limit=50') && r.options.credentials === 'same-origin')).toBe(true);
  doc.body.dataset.accountId = 'b'; win.dispatchEvent(new Event('openclank:auth-context-changed'));
  await tick(); expect(requests).toHaveLength(10); expect(requests[1].options.signal.aborted).toBe(true);
  delayed(response('native_actor', [row('native_actor','stale')], null)); await tick();
  const root = container.children[0], content = root.children[2];
  expect(content.children[1].children.some(x => x.children?.some(y => y.textContent === 'stale'))).toBe(false);
  const routine = content.children[2], more = routine.children.at(-1);
  more.dispatchEvent(new Event('click')); more.dispatchEvent(new Event('click')); await tick();
  expect(requests).toHaveLength(11);
  appendResolve(response('scheduler_routine', [row('scheduler_routine','one'),row('scheduler_routine','two')], null)); await tick();
  expect(root.children[1].textContent).toContain('2 routines');
  expect(content.children.at(-1).textContent).toContain('Copal tasks');
  view.dispose(); expect(requests.every(r => r.options.signal.aborted)).toBe(true);
  doc.dispatchEvent(new Event('odysseus:session-selected')); await tick(); expect(requests).toHaveLength(11);
});
