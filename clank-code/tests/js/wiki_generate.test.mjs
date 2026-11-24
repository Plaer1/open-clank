import assert from 'node:assert/strict'
import {
  WIKI_MAX_BYTES, WIKI_MAX_PAGES, byteLength, classifyWikiPages, estimateAppend, groupPages, indexMarker, pageMarker,
  planWikiAppend, statusOutcome, validatePlan, wikiRequest,
} from '../../static/js/copal/wikiGenerate.js'

let groups = 0
const group = (name, fn) => { fn(); groups++; console.log('ok', name) }
const hex = n => String(n).padStart(64, '0').replace(/[^0-9]/g, '0')
const HASH = 'a'.repeat(64)
const index = (hash = HASH) => `${indexMarker(hash)}\n## Code wiki index\n\nbody\n`
const page = (n, path) => ({ page_id: hex(n), file_id: 'file:' + hex(n), rel_path: path, bytes_est: 100 })
const withMarkdown = (p, heading = p.rel_path.replace(/[.\-]/g, c => `&#${c.charCodeAt(0)};`)) => ({ ...p, markdown: `${pageMarker(p.page_id)}\n## ${heading}\n\nBody ${p.rel_path}\n` })
const pages = [page(1, 'src/a.py'), page(2, 'src/b-c.py'), page(3, 'README.md')]
const full = pages.map(p => withMarkdown(p))

group('statusOutcome', () => {
  assert.equal(statusOutcome(200), 'ok')
  assert.equal(statusOutcome(409), 'replan')
  for (const s of [401, 413, 503, 500, 404]) assert.equal(statusOutcome(s), 'failed')
})

group('validatePlan', () => {
  const plan = { receipt: { workspace_id: 'w' }, pages, cap: 25, omissions: ['x'], plan_hash: HASH, index_markdown: index() }
  assert.equal(validatePlan(plan).ok, true)
  assert.equal(validatePlan({ ...plan, plan_hash: 'b'.repeat(64) }).ok, false)
  assert.equal(validatePlan({ ...plan, pages: [pages[0], pages[0]] }).ok, false)
  const many = Array.from({ length: WIKI_MAX_PAGES + 1 }, (_, i) => page(i + 1, `f${i}.py`))
  assert.equal(validatePlan({ ...plan, pages: many }).ok, false)
  assert.equal(validatePlan({ ...plan, pages: [{ ...pages[0], bytes_est: WIKI_MAX_BYTES }] }).ok, false)
  assert.equal(validatePlan(null).ok, false)
})

group('groupPages', () => {
  assert.deepEqual(groupPages(pages).map(([d, ps]) => [d, ps.length]), [['(root)', 1], ['src', 2]])
})

group('classify add / skipped / stale', () => {
  const doc = `# Mine\n\n${withMarkdown(pages[0]).markdown}\n${withMarkdown(page(9, 'src/b-c.py')).markdown}\n${index('c'.repeat(64))}`
  const c = classifyWikiPages(doc, { indexMarkdown: index(), pages })
  assert.deepEqual(c.skipped.map(p => p.rel_path), ['src/a.py'])
  assert.deepEqual(c.stale.map(p => p.rel_path), ['src/b-c.py']) // same path, other page id; heading is entity-escaped
  assert.deepEqual(c.add.map(p => p.rel_path), ['README.md'])
  assert.equal(c.index, 'add')
  assert.deepEqual(c.staleIndexes, ['c'.repeat(64)])
  assert.equal(classifyWikiPages(doc + index(), { indexMarkdown: index(), pages }).index, 'present')
  assert.equal(estimateAppend(c, index()), 100 + byteLength(index()))
})

group('append block: index + pages, order and separator', () => {
  const r = planWikiAppend('# Mine', { indexMarkdown: index(), pages: full })
  assert.equal(r.outcome, 'append')
  assert.deepEqual(r.appended, [indexMarker(HASH), ...pages.map(p => pageMarker(p.page_id))])
  assert.ok(r.text.startsWith('\n\n' + indexMarker(HASH)))
  assert.ok(r.text.endsWith('\n') && !r.text.endsWith('\n\n'))
  assert.equal(r.bytes, byteLength(r.text))
  assert.equal(planWikiAppend('', { indexMarkdown: index(), pages: [] }).text.startsWith(indexMarker(HASH)), true)
  assert.equal(planWikiAppend('x\n', { indexMarkdown: index(), pages: [] }).text.startsWith('\n' + indexMarker(HASH)), true)
  assert.equal(planWikiAppend('x\n\n', { indexMarkdown: index(), pages: [] }).text.startsWith(indexMarker(HASH)), true)
})

group('skips present markers; second identical call appends nothing', () => {
  const source = '# Mine\n\nnotes'
  const first = planWikiAppend(source, { indexMarkdown: index(), pages: full })
  const after = source + first.text
  assert.ok(after.startsWith(source)) // existing text is a byte-identical prefix
  const second = planWikiAppend(after, { indexMarkdown: index(), pages: full })
  assert.equal(second.outcome, 'unchanged')
  assert.deepEqual(second.appended, [])
  assert.equal(second.skipped.length, 4)
  const partial = planWikiAppend(source + '\n\n' + full[0].markdown, { indexMarkdown: index(), pages: full })
  assert.deepEqual(partial.appended, [indexMarker(HASH), pageMarker(pages[1].page_id), pageMarker(pages[2].page_id)])
  assert.deepEqual(partial.skipped, [pageMarker(pages[0].page_id)])
})

group('stale sections stay; nothing is replaced or removed', () => {
  const old = withMarkdown(page(9, 'src/a.py')).markdown
  const source = `keep me\n\n${old}\n${index('c'.repeat(64))}`
  const r = planWikiAppend(source, { indexMarkdown: index(), pages: full })
  assert.equal(r.outcome, 'append')
  assert.deepEqual(r.stale, [pageMarker(pages[0].page_id)])
  assert.ok(!r.appended.includes(pageMarker(pages[0].page_id)))
  assert.ok(!r.text.includes(pageMarker(hex(9))))
  assert.ok((source + r.text).startsWith(source))
  assert.equal(r.appended.includes(indexMarker(HASH)), true) // new index still appended; old one untouched
})

group('caps refuse', () => {
  const big = withMarkdown(page(1, 'big.py')); big.markdown += 'x'.repeat(WIKI_MAX_BYTES)
  assert.equal(planWikiAppend('', { indexMarkdown: index(), pages: [big] }).outcome, 'failed')
  const many = Array.from({ length: WIKI_MAX_PAGES + 1 }, (_, i) => withMarkdown(page(i + 1, `f${i}.py`)))
  assert.equal(planWikiAppend('', { indexMarkdown: index(), pages: many }).outcome, 'failed')
})

group('malformed input refused', () => {
  assert.equal(planWikiAppend('', { indexMarkdown: 'no marker', pages: [] }).outcome, 'failed')
  assert.equal(planWikiAppend('', { indexMarkdown: index(), pages: [{ ...full[0], markdown: 'no marker' }] }).outcome, 'failed')
  assert.equal(planWikiAppend('', { indexMarkdown: index(), pages: [full[0], full[0]] }).outcome, 'failed')
  assert.equal(planWikiAppend('', { indexMarkdown: index(), pages: [{ ...full[0], markdown: full[0].markdown + pageMarker(full[0].page_id) }] }).outcome, 'failed')
})

{
  const calls = []
  const fake = (status, body) => async (url, init) => { calls.push([url, JSON.parse(init.body)]); return { status, json: async () => body } }
  const body = { workspace_id: 'w', receipt: { r: 1 }, page: { page_id: hex(1) } }
  const ok = await wikiRequest(fake(200, { page_id: hex(1), markdown: 'm' }), '', 'page', body)
  assert.equal(ok.outcome, 'ok')
  assert.equal(calls[0][0], '/api/memory/code/wiki/page')
  assert.deepEqual(calls[0][1].receipt, { r: 1 }) // receipt passed unchanged
  assert.equal((await wikiRequest(fake(409), '', 'page', body)).outcome, 'replan')
  assert.equal((await wikiRequest(fake(503), '', 'plan', {})).outcome, 'failed')
  assert.equal((await wikiRequest(fake(200, { page_id: hex(2), markdown: 'm' }), '', 'page', body)).outcome, 'failed')
  groups++; console.log('ok wikiRequest')
}

console.log(`${groups} test groups ok`)
