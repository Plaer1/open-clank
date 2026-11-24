// O02 worker inspector fixtures: paged child history, mixed edges, idle/interrupted worker, missing transcript,
// stale controls, same-actor follow-up, passive read. Run: node tests/agent_run_inspector.test.mjs
import assert from 'node:assert/strict'
import { openAgentRunInspector, observedEdges } from '../static/js/agentRunInspector.js'

class El {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.attrs = {}; this._text = ''; this.parent = null; this.dataset = {}; this.value = ''; this.disabled = false }
  set textContent(v) { this._text = String(v); this.children = [] }
  get textContent() { return this._text + this.children.map(c => c.textContent).join('') }
  append(...c) { for (const x of c) { x.parent = this; this.children.push(x) } }
  replaceChildren(...c) { this.children = []; this.append(...c) }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this) }
  setAttribute(k, v) { this.attrs[k] = v }
  addEventListener(e, f) { this.listeners[e] = f }
  click() { return this.disabled ? undefined : this.listeners.click?.() }
  focus() { El.focused = this }
  find(pred, out = []) { if (pred(this)) out.push(this); this.children.forEach(c => c.find(pred, out)); return out }
}
const doc = { createElement: t => new El(t) }
const btn = (root, label) => root.find(e => e.tag === 'button' && e.textContent === label)[0]
const tick = () => new Promise(r => setTimeout(r, 0))
const json = (body, status = 200) => ({ ok: status < 400, status, json: async () => body })
const page = (over = {}) => ({ actorID: 'a1', sessionID: 's1', executionRevision: 4, status: 'running', coverage: 'partial', transcriptVersion: 1,
  messages: [{ id: 'm1', messageID:'msg1', role:'assistant', time:1, kind:'text', text:'first', tool:null, state:null, omission:null }], receipts: { items: [] }, messageCursor: null, receiptCursor: null, ...over })
const ref = { kind: 'native_actor', sessionId: 's1', nativeId: 'a1', executionRevision: 4 }
const actorRow = (id, extra = {}, session = 's1') => ({ sourceRef: { kind: 'native_actor', sessionId: session, nativeId: id, executionRevision: 1 }, title: id, nativeStatus: 'running', links: [], ...extra })

function setup(handler, over = {}) {
  const calls = [], container = new El('div'), sent = [], events = []
  const fetchImpl = async (url, options) => { calls.push({ url, options }); return handler(url, calls) }
  const view = openAgentRunInspector({ container, doc, fetchImpl, scope: { chat: 'c1', mapRevision: 3 }, row: { sourceRef: ref, title: 'Worker A', nativeStatus: 'running', links: [] },
    relations: () => ({ rootSessionId: 'root', actors: [], tasks: [], workflows: [] }), current: () => true,
    renderAccounting: (wrap, acc) => { events.push(['accounting', acc]); wrap.textContent = `Sub-agents: ${acc.total}` },
    sendFollowUp: async args => { sent.push(args) }, onSelectActor: e => events.push(['select', e.row.sourceRef.nativeId]),
    onReturnToChat: () => events.push(['root']), onRefresh: () => events.push(['refresh']), onClose: () => events.push(['close']), opener: { focus() { events.push(['focus']) } }, ...over })
  return { container, calls, view, sent, events }
}

// 1) paged child history, passive reads only, escaped text, de-duplication, close restores focus
{
  const s = setup(url => url.includes('message_cursor=') ? json(page({ messages: [{ id: 'm0', text: 'older' }, { id: 'm1', text: 'first' }], messageCursor: null }))
    : json(page({ messages: [{ id: 'm1', text: '<script>alert(1)</script>' }], messageCursor: 'C/1' })))
  await tick()
  assert.match(s.calls[0].url, /\/api\/orchestration\/c1\/actors\/a1\/inspection\?.*map_revision=3.*actor_session_id=s1.*execution_revision=4/)
  assert.equal(s.container.find(e => e.tag === 'script').length, 0)
  assert.match(s.container.textContent, /<script>alert\(1\)<\/script>/)
  assert.match(s.container.textContent, /Partial transcript/)
  await btn(s.container, 'Older transcript').click(); await tick()
  assert.match(s.calls.at(-1).url, /message_cursor=C%2F1/)
  assert.equal(s.container.find(e => e.className === 'ari-message').length, 2) // m1 not duplicated, m0 added
  assert.equal(btn(s.container, 'Older transcript'), undefined)
  assert.match(s.container.textContent, /Start of available transcript/)
  // read side effects: only GETs on /inspection until an explicit follow-up
  assert.ok(s.calls.every(c => c.url.includes('/inspection') && !c.options.method && c.options.credentials === 'same-origin'))
  assert.equal(s.sent.length, 0)
  btn(s.container, 'Close details').click()
  assert.deepEqual(s.events.filter(e => e[0] !== 'accounting').map(e => e[0]), ['close', 'focus']); assert.equal(s.container.children.length, 0)
}

// 2) mixed edges from verified links with explicit types; child navigation distinct from root navigation
{
  const link = (kind, target, provenance) => ({ kind, target, provenance })
  const rel = { rootSessionId: 'root',
    actors: [{ row: actorRow('a1', { links: [link('actor_parent', { kind: 'native_actor', sessionId: 's1', nativeId: 'main' }, 'native_actor.parentActorID')], linkCoverage: 'observed' }), peer: false },
      { row: actorRow('main'), peer: false },
      { row: actorRow('kid', { links: [link('actor_parent', { kind: 'native_actor', sessionId: 's1', nativeId: 'a1' }, 'native_actor.parentActorID')], linkCoverage: 'observed' }), peer: false },
      { row: actorRow('peer1', {}, 'p9'), peer: true }],
    tasks: [{ sourceRef: { kind: 'native_task', sessionId: 's1', nativeId: 't2' }, linkCoverage: 'observed', links: [
      link('assigned_to', { kind: 'native_actor', sessionId: 's1', nativeId: 'a1' }, 'native_task.owner'), link('task_parent', { kind: 'native_task', sessionId: 's1', nativeId: 't1' }, 'native_task.parent_task_id')] }],
    workflows: [{ sourceRef: { kind: 'workflow', sessionId: 'root', nativeId: 'w1' } }] }
  const kinds = observedEdges(rel, 'a1', 's1').edges.map(e => e.kind).sort()
  assert.deepEqual(kinds, ['actor_parent', 'actor_parent', 'assigned_to', 'task_parent', 'workflow_session'])
  const s = setup(() => json(page()), { relations: () => rel })
  await tick()
  const text = s.container.textContent
  for (const kind of ['actor_parent', 'assigned_to', 'task_parent', 'workflow_session']) assert.match(text, new RegExp(kind))
  assert.doesNotMatch(text, /Link coverage: unknown/)
  btn(s.container, 'Return to chat').click()
  s.container.find(e => e.tag === 'button' && e.textContent === 'Inspect worker')[0].click()
  const selected = s.events.filter(e => e[0] === 'root' || e[0] === 'select')
  assert.deepEqual(selected[0], ['root']); assert.equal(selected.length, 2)
  assert.ok(['main', 'kid'].includes(selected[1][1]))
  // unknown link coverage is shown as such; no edge is invented for an unverified parent
  const open = setup(() => json(page()), { relations: () => ({ rootSessionId: 'root', actors: [{ row: actorRow('a1', { linkCoverage: 'unknown' }), peer: false }], tasks: [], workflows: [] }) })
  await tick()
  assert.match(open.container.textContent, /Link coverage: unknown/); assert.equal(open.container.find(e => e.className === 'ari-edge ari-edge-actor_parent').length, 0)
  assert.equal(observedEdges({ rootSessionId: 'root', actors: [{ row: actorRow('peer1', {}, 'p9'), peer: true }] }, 'peer1', 'p9').edges[0].kind, 'peer_session')
}

// 3) idle/interrupted worker and model display (the UI never picks a model)
{
  const s = setup(() => json(page({ status: 'idle' })), { row: { sourceRef: ref, title: 'W', nativeStatus: 'failed', nativeOutcome: 'interrupted', links: [], requestedModel: 'p/big', effectiveModel: { providerID: 'p', modelID: 'small' } },
    relations: () => ({ rootSessionId: 'root', actors: [{ row: actorRow('a1', { requestedModel: 'p/big', effectiveModel: { providerID: 'p', modelID: 'small' } }), peer: false }], tasks: [], workflows: [] }) })
  await tick()
  assert.match(s.container.textContent, /Execution status: idle · revision 4 · last outcome: interrupted/)
  assert.match(s.container.textContent, /requested p\/big; effective p\/small/)
  const acc = s.events.find(e => e[0] === 'accounting')[1]
  assert.equal(acc.actors[0].requested_model, 'p/big'); assert.deepEqual(acc.actors[0].effective_model, { provider_id: 'p', model_id: 'small' })
  assert.equal(s.container.find(e => e.tag === 'select').length, 0) // no model picker
  // the raw actor shape (providerID/modelID) is accepted and shown as effective model
  const raw = setup(() => json(page()), { row: { sourceRef: ref, title: 'W', nativeStatus: 'idle', links: [], effectiveModel: { providerID: 'p', modelID: 'raw' } } })
  await tick(); assert.match(raw.container.textContent, /requested not set; effective p\/raw/)
  const plain = setup(() => json(page()))
  await tick(); assert.match(plain.container.textContent, /Model: not reported by this observation/)
}

// 4) missing transcript, and a retired/unknown worker disables follow-up with a recoverable reason
{
  const empty = setup(() => json(page({ messages: [] })))
  await tick(); assert.match(empty.container.textContent, /No transcript text is available for this worker/)
  const gone = setup(() => json({ detail: { code: 'ACTOR_NOT_FOUND', message: 'x' } }, 404))
  await tick()
  assert.match(gone.container.textContent, /no longer registered\. Nothing was sent and no replacement worker was started/)
  assert.equal(btn(gone.container, 'Send follow-up').disabled, true)
  btn(gone.container, 'Refresh').click(); assert.deepEqual(gone.events.at(-1), ['refresh'])
  const net = setup(() => Promise.reject(new TypeError('down')))
  await tick(); assert.ok(btn(net.container, 'Retry')); assert.equal(btn(net.container, 'Send follow-up').disabled, false)
}

// 5) stale controls: the revision re-check refuses before anything is sent
{
  let phase = 0
  const s = setup(() => (phase++ === 0 ? json(page()) : json({ detail: 'x' }, 409)))
  await tick()
  const area = s.container.find(e => e.tag === 'textarea')[0]; area.value = 'please continue'
  await btn(s.container, 'Send follow-up').click(); await tick()
  assert.equal(s.sent.length, 0)
  assert.match(s.container.textContent, /changed since this view was opened/)
  assert.match(s.container.textContent, /Nothing was sent/); assert.equal(btn(s.container, 'Send follow-up').disabled, true)
  // returned revision differs from the selected one
  let n = 0
  const m = setup(() => (n++ === 0 ? json(page()) : json(page({ executionRevision: 5 }))))
  await tick(); m.container.find(e => e.tag === 'textarea')[0].value = 'go'
  await btn(m.container, 'Send follow-up').click(); await tick()
  assert.equal(m.sent.length, 0)
}

// 6) follow-up targets the same actor, after a passive revision check, with no model fields
{
  const s = setup(() => json(page({ status: 'idle' })))
  await tick(); s.container.find(e => e.tag === 'textarea')[0].value = '  keep going  '
  const before = s.calls.length
  await btn(s.container, 'Send follow-up').click(); await tick()
  assert.equal(s.calls.length, before + 1); assert.match(s.calls.at(-1).url, /execution_revision=4/)
  assert.deepEqual(s.sent, [{ actorID: 'a1', actorSessionID: 's1', text: 'keep going', chat: 'c1', mapRevision: 3, executionRevision: 4 }])
  assert.ok(!('modelOptions' in s.sent[0]) && !('model' in s.sent[0]))
  assert.match(s.container.textContent, /Follow-up queued for actor a1/)
  // a failing send keeps the draft guidance and never reports success
  const f = setup(() => json(page()), { sendFollowUp: async () => { throw new Error('boom') } })
  await tick(); f.container.find(e => e.tag === 'textarea')[0].value = 'x'
  await btn(f.container, 'Send follow-up').click(); await tick()
  assert.match(f.container.textContent, /not confirmed: boom/); assert.doesNotMatch(f.container.textContent, /queued for actor/)
}

// 7) scope change/dispose discards a late response
{
  let release, ok = true
  const s = setup(() => new Promise(r => { release = () => r(json(page({ messages: [{ id: 'late', text: 'LATE' }] }))) }), { current: () => ok })
  ok = false; release(); await tick()
  assert.doesNotMatch(s.container.textContent, /LATE/)
  s.view.dispose()
}
console.log('agent_run_inspector tests ok')

// Versioned transcript displays public tool state and explicit omissions without interpreting markup.
{
 const s = setup(() => json(page({messages:[
  {id:'p2',messageID:'m2',role:'assistant',time:1,kind:'tool',text:null,tool:'read',state:'running',omission:null},
  {id:'p1',messageID:'m1',role:'user',time:2,kind:'omitted',text:null,tool:null,state:null,omission:'attachment'}]})))
 await tick(); assert.match(s.container.textContent,/Tool read · running/)
 assert.match(s.container.textContent,/Content omitted \(attachment\)/)
 assert.match(s.container.textContent,/message m2 · part p2/)
 assert.equal(s.sent.length,0)
}
