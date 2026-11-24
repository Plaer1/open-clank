// chatRenderer.js and settings.js need a full browser to import, so the pure/DOM functions are
// sliced out of the source text and evaluated with minimal stubs.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const slice = (file, name) => {
  const src = readFileSync(new URL(`../../static/js/${file}`, import.meta.url), 'utf8')
  let start = src.indexOf(`export function ${name}(`)
  if (start < 0) start = src.indexOf(`export async function ${name}(`)
  assert.ok(start >= 0, name)
  const end = src.indexOf('\n}\n', start)
  return src.slice(start, end + 2).replace(/^export /, '')
}
const helperSrc = slice('chatRenderer.js', 'controlPermissionView')
const cardSrc = slice('chatRenderer.js', 'renderPermissionCard')
const controlView = new Function(`${helperSrc}; return controlPermissionView`)()
const desktopControlDisabled = new Function(`${slice('settings.js', 'desktopControlDisabled')}; return desktopControlDisabled`)()

const T = 'lifetools_abc_control_desktop'
const text256 = Array.from({ length: 8 }, (_, i) => `line ${i}\t${'x'.repeat(30)}`).join('\n').slice(0, 256).padEnd(256, 'y')
assert.equal(text256.length, 256)

// helper
const v = controlView({ permission_type: T, detail: { summary: text256, args: {} } })
assert.equal(v.title, 'Desktop control')
assert.equal(v.summary, text256)
assert.deepEqual(v.options.map(o => o.id), ['once', 'reject'])
assert.equal(controlView({ permission_type: T, detail: { summary: '<img src=x onerror=alert(1)>' } }).summary, '<img src=x onerror=alert(1)>')
for (const detail of [undefined, {}, { summary: 5 }, { summary: '' }]) {
  const m = controlView({ permission_type: T, detail })
  assert.equal(m.summary, 'Details unavailable')
  assert.deepEqual(m.options.map(o => o.id), ['reject'])
}
assert.equal(controlView({ permission_type: 'bash', detail: { summary: 'x' } }), null)
assert.equal(controlView({ permission_type: 'lifetools_control_desktop' }), null)

// card rendering with a minimal DOM
class El {
  constructor(t) { this.tag = t; this.children = []; this.dataset = {}; this.style = {}; this.attrs = {}; this.L = {}; this.className = ''; this._t = '' }
  set textContent(x) { this._t = String(x); this.children = [] }
  get textContent() { return this._t + this.children.map(c => c.textContent).join('') }
  appendChild(c) { c.parent = this; this.children.push(c); return c }
  append(...c) { c.forEach(x => this.appendChild(x)) }
  get isConnected() { return !!this.parent && this.parent.children.includes(this) }
  setAttribute(k, x) { this.attrs[k] = x }
  addEventListener(e, f) { this.L[e] = f }
  remove() { this.parent.children = this.parent.children.filter(c => c !== this) }
  scrollIntoView() {}
  focus() { focused = this }
  all(p, o = []) { if (p(this)) o.push(this); this.children.forEach(c => c.all(p, o)); return o }
  querySelectorAll(sel) { return this.all(e => e.tag === 'button') }
  querySelector(sel) { const id = /\[data-option-id="(.+)"\]/.exec(sel)?.[1]; return this.all(e => e.dataset.optionId === id)[0] || null }
}
let focused = null
const chat = new El('div')
const doc = { getElementById: () => chat, createElement: t => new El(t) }
const render = new Function('document', 'exactOperationDetails', 'hasAdditionalOperationDetails', 'verifiedOperationExplanation', 'window', 'console', 'FormData',
  `${helperSrc}; ${cardSrc}; return renderPermissionCard`)(doc,
  d => (d && typeof d.filepath === 'string' ? [d.filepath] : []), () => false, async () => null, {}, console, class { append() {} })
const buttons = c => c.all(e => e.tag === 'button')

const card = render({ request_id: 'r1', permission_type: T, detail: { summary: text256, args: {} } }, 's1')
assert.ok(card.textContent.includes('Desktop control') && !card.textContent.includes(T))
assert.ok(card.textContent.includes(text256))
assert.ok(card.textContent.includes('This acts on your real desktop. Effects are not verified automatically.'))
assert.deepEqual(buttons(card).map(b => b.textContent), ['Allow once', 'Reject'])
assert.equal(focused.textContent, 'Reject')
assert.ok(!buttons(card).some(b => b.L.keydown))
const evil = render({ request_id: 'r2', permission_type: T, detail: { summary: '<img src=x onerror=alert(1)>' } }, 's1')
assert.ok(evil.textContent.includes('<img src=x onerror=alert(1)>'))
assert.equal(evil.all(e => e.tag === 'img').length, 0)
const none = render({ request_id: 'r3', permission_type: T, detail: {} }, 's1')
assert.deepEqual(buttons(none).map(b => b.textContent), ['Reject'])
assert.ok(none.textContent.includes('Details unavailable'))

// other types unchanged: five fixed options, generic title
const other = render({ request_id: 'r4', permission_type: 'edit', detail: { filepath: '/a/b' } }, 's1')
assert.deepEqual(buttons(other).map(b => b.textContent.replace(/Until.*|Shared.*|all .*/, '')), ['Allow once', 'Allow in this chat', 'Allow in this workspace', 'Always allow', 'Reject'])
assert.ok(other.textContent.includes('Agent requests permission: edit'))

// settings rule
// control, capture and accessibility are independent: nothing disables the control toggle
assert.equal(desktopControlDisabled(false), false)
assert.equal(desktopControlDisabled(true), false)
// reading approval card
assert.equal(controlView({ permission_type: 'lifetools_abc_inspect_desktop', detail: { summary: 'Read the open window of Demo' } }).title, 'Read an application window')

// Stop button: bounded watch that survives the human's reading time
const stopSrc = ['controlStopVisible', 'watchControlStop', 'checkControlStopOnLoad'].map(n => slice('chatRenderer.js', n)).join('\n')
let tick = null
let stopChat = new El('div')
let calls = []
let statusBody = { session_active: false, accessibility_active: false, busy: false }
const stopFetch = async (url, init) => { calls.push([url, init?.method || 'GET']); return url.endsWith('/status') ? { ok: true, json: async () => statusBody } : { ok: true } }
const settle = () => new Promise(r => setTimeout(r, 5))
const makeStop = () => {
  stopChat = new El('div'); tick = null; calls = []
  return new Function('document', 'window', 'setInterval', 'clearInterval', `let controlStopCard = null, controlStopTimer = null, controlStopQuiet = 0, controlStopLimit = 3; ${stopSrc}; return { controlStopVisible, watchControlStop, checkControlStopOnLoad }`)(
    { getElementById: () => stopChat, createElement: t => new El(t) }, {}, f => { tick = f; return 1 }, () => { tick = null })
}
let api = makeStop()
assert.equal(api.controlStopVisible(null), false)
assert.equal(api.controlStopVisible({ session_active: false }), false)
assert.equal(api.controlStopVisible({ accessibility_active: true }), true)

// card rendered (watch starts), 10 s pass with no session: the watch has ended, no button
api.watchControlStop(stopFetch); await settle()
for (let i = 0; i < 5 && tick; i++) { await tick(); await settle() }
assert.equal(tick, null, 'the watch ends after the quiet window')
assert.equal(buttons(stopChat).length, 0)
// Allow clicked: the watch restarts with a long window, survives a quiet gap, then the session becomes active
api.watchControlStop(stopFetch, 15); await settle()
for (let i = 0; i < 8; i++) { await tick(); await settle() }
assert.notEqual(tick, null, 'still waiting for the helper to become active')
assert.equal(buttons(stopChat).length, 0)
statusBody = { session_active: true }
await tick(); await settle()
assert.deepEqual(buttons(stopChat).map(b => b.textContent), ['Stop control'])
assert.ok(stopChat.textContent.includes('Global takeover key unavailable'))
assert.ok(stopChat.textContent.includes('Cursor overlay status unavailable'))
statusBody = { session_active: true, takeover_shortcut: '<Ctrl+Escape>', cursor_overlay_at_begin: true }
await tick(); await settle()
assert.ok(stopChat.textContent.includes('Takeover key: <Ctrl+Escape>'))
assert.ok(stopChat.textContent.includes('available at session start'))
assert.equal(stopChat.all(e => e.tag === 'img').length, 0)
// Stop clicked: POST, button gone, then polling ends after the quiet window
statusBody = { session_active: false }
buttons(stopChat)[0].L.click(); await settle()
assert.ok(calls.some(c => c[0] === '/api/desktop-control/stop' && c[1] === 'POST'))
assert.equal(buttons(stopChat).length, 0, 'the button goes away after Stop')
for (let i = 0; i < 4 && tick; i++) { await tick(); await settle() }
assert.equal(tick, null, 'polling ends once the session has been inactive for the quiet window')

// page load with an active session shows the button; with nothing running nothing starts
api = makeStop(); statusBody = { session_active: true }
await api.checkControlStopOnLoad(stopFetch); await settle()
assert.deepEqual(buttons(stopChat).map(b => b.textContent), ['Stop control'])
api = makeStop(); statusBody = { session_active: false }
await api.checkControlStopOnLoad(stopFetch); await settle()
assert.equal(tick, null); assert.equal(buttons(stopChat).length, 0)

// the Allow answer on a control card restarts the watch with the long window; Reject does not
const spy = []
const renderSpy = new Function('document', 'exactOperationDetails', 'hasAdditionalOperationDetails', 'verifiedOperationExplanation', 'window', 'console', 'FormData', 'fetch', 'watchControlStop',
  `${helperSrc}; ${cardSrc}; return renderPermissionCard`)(doc, () => [], () => false, async () => null, {}, console, class { append() {} }, async () => ({ ok: true, status: 200, json: async () => ({}) }), (f, n) => spy.push(n))
const ac = renderSpy({ request_id: 'r9', permission_type: T, detail: { summary: 'Click left at (1, 2)' } }, 's9')
assert.deepEqual(spy, [undefined], 'card render starts the watch')
const allow = buttons(ac).find(b => b.textContent.includes('Allow once')); allow.L.click(); await settle()
assert.deepEqual(spy, [undefined, 15])
console.log('control_approval_card ok')
