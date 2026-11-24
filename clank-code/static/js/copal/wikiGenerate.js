import { wireDialog } from './overlays.js'

// Structure-only code wiki. No model is used and no summary endpoint is called.
// Pure decision helpers are exported for tests; the dialog is the only DOM code.

export const WIKI_MAX_PAGES = 100
export const WIKI_MAX_BYTES = 4 * 1024 * 1024
const PAGE_MARKER_RE = /<!-- openclank-wiki-page:v1:([0-9a-f]{64}) -->\n## ([^\n]*)/g
const INDEX_MARKER_RE = /<!-- openclank-wiki-index:v1:([0-9a-f]{64}) -->/g
const HEX = /^[0-9a-f]{64}$/

export const pageMarker = id => `<!-- openclank-wiki-page:v1:${id} -->`
export const indexMarker = hash => `<!-- openclank-wiki-index:v1:${hash} -->`
export const byteLength = text => new TextEncoder().encode(text).length
export const indexHashOf = markdown => (typeof markdown === 'string' && /^<!-- openclank-wiki-index:v1:([0-9a-f]{64}) -->\n/.exec(markdown)?.[1]) || null
// Page headings are entity-escaped by the server; decode &#NN; to compare paths.
const unescapeLabel = text => text.replace(/&#(\d+);/g, (whole, n) => Number(n) <= 0x10FFFF ? String.fromCodePoint(Number(n)) : whole)

/** Map an endpoint reply status to what the dialog does next. */
export function statusOutcome(status) {
  if (status >= 200 && status < 300) return 'ok'
  if (status === 409) return 'replan' // code changed, receipt/body rejected: a new plan is needed
  return 'failed' // 401, 413, 503 and anything else
}

/** Plan validation; the server is trusted for content, not for shape. */
export function validatePlan(plan) {
  const bad = message => ({ ok: false, message })
  if (!plan || typeof plan !== 'object' || !Array.isArray(plan.pages) || !plan.receipt || typeof plan.receipt !== 'object') return bad('The wiki plan is malformed.')
  if (plan.pages.length > WIKI_MAX_PAGES) return bad(`The plan has more than ${WIKI_MAX_PAGES} pages. Lower the cap.`)
  if (!Array.isArray(plan.omissions) || plan.omissions.some(o => typeof o !== 'string')) return bad('The wiki plan is malformed.')
  if (!HEX.test(plan.plan_hash || '') || indexHashOf(plan.index_markdown) !== plan.plan_hash || !plan.index_markdown.isWellFormed()) return bad('The wiki index is malformed.')
  const seen = new Set()
  for (const page of plan.pages) {
    if (!page || !HEX.test(page.page_id) || typeof page.file_id !== 'string' || typeof page.rel_path !== 'string' || !page.rel_path
        || !Number.isInteger(page.bytes_est) || page.bytes_est < 0 || seen.has(page.page_id)) return bad('The wiki plan is malformed.')
    seen.add(page.page_id)
  }
  const estimate = plan.pages.reduce((sum, page) => sum + page.bytes_est, byteLength(plan.index_markdown))
  if (estimate > WIKI_MAX_BYTES) return bad('The planned wiki would exceed 4 MiB. Lower the cap.')
  return { ok: true }
}

/** Group planned pages by directory, as the index does. */
export function groupPages(pages) {
  const groups = new Map()
  for (const page of pages) {
    const directory = page.rel_path.includes('/') ? page.rel_path.slice(0, page.rel_path.lastIndexOf('/')) : '(root)'
    if (!groups.has(directory)) groups.set(directory, [])
    groups.get(directory).push(page)
  }
  return [...groups].sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
}

/**
 * Sort plan pages against the current document text.
 * add: no marker for this page; skipped: marker already present;
 * stale: no marker for this page but a section for the same path with another page id exists (never replaced).
 */
export function classifyWikiPages(source, { indexMarkdown, pages } = {}) {
  const hash = indexHashOf(indexMarkdown), byPath = new Set()
  for (const match of source.matchAll(PAGE_MARKER_RE)) byPath.add(unescapeLabel(match[2]))
  const out = { add: [], skipped: [], stale: [], index: 'add', staleIndexes: [] }
  for (const page of pages) {
    if (source.includes(pageMarker(page.page_id))) out.skipped.push(page)
    else if (byPath.has(page.rel_path)) out.stale.push(page)
    else out.add.push(page)
  }
  const found = [...new Set([...source.matchAll(INDEX_MARKER_RE)].map(match => match[1]))]
  if (hash && found.includes(hash)) out.index = 'present'
  out.staleIndexes = found.filter(item => item !== hash)
  return out
}

/** Estimated bytes to append, from plan bytes_est (real size is checked after fetch). */
export function estimateAppend(classified, indexMarkdown) {
  return classified.add.reduce((sum, page) => sum + page.bytes_est, classified.index === 'add' ? byteLength(indexMarkdown) : 0)
}

/**
 * Build the append-only block for `source`. Never edits existing text: the result
 * is only the text to add after it. pages carry {page_id, rel_path, markdown}.
 */
export function planWikiAppend(source, { indexMarkdown, pages } = {}) {
  const fail = message => ({ outcome: 'failed', message })
  if (typeof source !== 'string' || !Array.isArray(pages) || pages.length > WIKI_MAX_PAGES) return fail(`A wiki write holds at most ${WIKI_MAX_PAGES} pages.`)
  const hash = indexHashOf(indexMarkdown)
  if (!hash || !indexMarkdown.isWellFormed() || indexMarkdown.indexOf(indexMarker(hash)) !== indexMarkdown.lastIndexOf(indexMarker(hash))) return fail('The wiki index is malformed.')
  const ids = new Set()
  for (const page of pages) {
    const marker = page && HEX.test(page.page_id) ? pageMarker(page.page_id) : null
    if (!marker || typeof page.rel_path !== 'string' || typeof page.markdown !== 'string' || !page.markdown.isWellFormed()
        || !page.markdown.startsWith(marker + '\n') || page.markdown.indexOf(marker) !== page.markdown.lastIndexOf(marker) || ids.has(page.page_id)) return fail('A wiki page is malformed.')
    ids.add(page.page_id)
  }
  const classified = classifyWikiPages(source, { indexMarkdown, pages })
  const addIndex = classified.index === 'add'
  const sections = [...(addIndex ? [indexMarkdown] : []), ...classified.add.map(page => page.markdown)]
  const markers = list => list.map(page => pageMarker(page.page_id))
  const report = { appended: [...(addIndex ? [indexMarker(hash)] : []), ...markers(classified.add)], skipped: [...(addIndex ? [] : [indexMarker(hash)]), ...markers(classified.skipped)], stale: markers(classified.stale) }
  if (!sections.length) return { outcome: 'unchanged', ...report, appended: [], bytes: 0 }
  const separator = !source || source.endsWith('\n\n') ? '' : source.endsWith('\n') ? '\n' : '\n\n'
  const text = separator + sections.map(section => section.replace(/\n+$/, '')).join('\n\n') + '\n'
  const bytes = byteLength(text)
  if (bytes > WIKI_MAX_BYTES) return fail('The wiki to append exceeds 4 MiB. Lower the page cap.')
  return { outcome: 'append', text, bytes, ...report }
}

/** POST one wiki action through an injected fetch. Returns {outcome, value}. */
export async function wikiRequest(fetchFn, base, action, body, signal) {
  const response = await fetchFn(`${base}/api/memory/code/wiki/${action}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal })
  const outcome = statusOutcome(response.status)
  if (outcome !== 'ok') return { outcome, status: response.status }
  const value = await response.json()
  if (action === 'page' && (typeof value?.markdown !== 'string' || value.page_id !== body.page.page_id)) return { outcome: 'failed', status: response.status }
  return { outcome, value }
}

/** Dialog. onCaptureDocument() -> handle|null; onInsertWiki(handle, payload) -> Notes result. */
export function openWikiGenerate({ workspaceId, copalWorkspace = '', base = '', isCurrent = () => true, onCaptureDocument, onInsertWiki }) {
  const dialog = document.createElement('dialog'), title = document.createElement('h3'), aborter = new AbortController()
  dialog.className = 'copal-dialog'
  title.textContent = 'Generate code wiki'
  const note = document.createElement('p'), status = document.createElement('p'), planText = document.createElement('pre'), previewText = document.createElement('pre')
  note.textContent = 'Structure only. No model is used. Pages are appended to the Markdown document you have open; nothing existing is changed or removed.'
  for (const block of [planText, previewText]) { block.style.whiteSpace = 'pre-wrap'; block.style.overflowWrap = 'anywhere'; block.style.maxHeight = '16rem'; block.style.overflow = 'auto' }
  const capLabel = document.createElement('label'), cap = document.createElement('input')
  cap.type = 'number'; cap.min = '1'; cap.max = String(WIKI_MAX_PAGES); cap.value = '25'; capLabel.append('Module page cap ', cap)
  const algorithmLabel = document.createElement('label'), algorithm = document.createElement('select')
  for (const [value, name] of [['label_propagation','Label propagation'], ['modularity_refinement','Modularity refinement']]) {
    const option = document.createElement('option'); option.value = value; option.textContent = name; algorithm.append(option)
  }
  algorithmLabel.append('Community grouping ', algorithm)
  const planButton = document.createElement('button'), previewButton = document.createElement('button'), writeButton = document.createElement('button'), closeButton = document.createElement('button')
  for (const button of [planButton, previewButton, writeButton, closeButton]) { button.type = 'button'; button.className = 'copal-btn' }
  planButton.textContent = 'Plan'; previewButton.textContent = 'Preview against open document'; writeButton.textContent = 'Write to document'; closeButton.textContent = 'Close'
  previewButton.disabled = true; writeButton.disabled = true
  let plan = null, previewed = null, serial = 0, disposed = false, busy = false
  const current = () => !disposed && !aborter.signal.aborted && isCurrent()
  const reset = () => { serial++; previewed = null; previewText.textContent = ''; writeButton.disabled = true }
  const newPlanNeeded = () => { plan = null; reset(); previewButton.disabled = true; planText.textContent = ''; status.textContent = 'The code changed. Create a new plan.' }
  const changePlanSettings = () => {
    plan = null; reset(); previewButton.disabled = true; planText.textContent = ''; status.textContent = 'Plan settings changed. Plan again.'
  }
  cap.addEventListener('change', changePlanSettings)
  algorithm.addEventListener('change', changePlanSettings)

  planButton.onclick = async () => {
    if (!current() || busy) return
    const limit = Number(cap.value)
    if (!Number.isInteger(limit) || limit < 1 || limit > WIKI_MAX_PAGES) { status.textContent = `Cap must be a whole number from 1 to ${WIKI_MAX_PAGES}.`; return }
    busy = true; plan = null; reset(); previewButton.disabled = true; planText.textContent = ''; status.textContent = 'Planning from the code Graph…'
    const ticket = serial
    try {
      const result = await wikiRequest(fetch, base, 'plan', { workspace_id: workspaceId, cap: limit, architecture: true, communities: true,
        ...(algorithm.value !== 'label_propagation' ? {community_algorithm:algorithm.value} : {}),
        ...(copalWorkspace ? {copal_workspace:copalWorkspace} : {}) }, aborter.signal)
      if (!current() || ticket !== serial) return
      if (result.outcome !== 'ok') { status.textContent = result.outcome === 'replan' ? 'The code Graph is unavailable or changed. Try again.' : 'Wiki planning failed.'; return }
      const checked = validatePlan(result.value)
      if (!checked.ok) { status.textContent = checked.message; return }
      plan = result.value
      const lines = [`${plan.pages.length} page(s) planned; cap ${plan.cap}.`]
      for (const [directory, pages] of groupPages(plan.pages)) lines.push('', directory + '/', ...pages.map(page => '  ' + page.rel_path))
      lines.push('', plan.omissions.length ? 'This plan is incomplete.' : 'No omissions were recorded.', ...plan.omissions.map(item => '- ' + item))
      if (typeof plan.architecture_markdown === 'string') lines.push('', plan.architecture_markdown)
      if (typeof plan.communities_markdown === 'string') lines.push('', plan.communities_markdown)
      planText.textContent = lines.join('\n')
      previewButton.disabled = false
      status.textContent = 'Plan ready. Preview it against the open document before writing.'
    } catch { if (current() && ticket === serial) status.textContent = 'Wiki planning failed.' }
    finally { busy = false }
  }

  const describe = (classified, estimate, revision) => {
    const lines = [`Document revision ${revision}.`, `Pages to add: ${classified.add.length}`, `Index: ${classified.index === 'add' ? 'will be added' : 'already in document — skipped'}`, `Estimated bytes to append: ${estimate}`]
    if (classified.skipped.length) lines.push('', 'Already in document — skipped:', ...classified.skipped.map(page => '  ' + page.rel_path))
    if (classified.stale.length) lines.push('', 'Stale — not replaced:', ...classified.stale.map(page => '  ' + page.rel_path))
    if (classified.staleIndexes.length) lines.push('', `Stale — not replaced: ${classified.staleIndexes.length} older index section(s) stay as they are.`)
    return lines.join('\n')
  }
  const payload = () => ({ indexMarkdown: plan.index_markdown, pages: plan.pages.map(({ page_id, file_id, rel_path, bytes_est }) => ({ page_id, file_id, rel_path, bytes_est })) })

  previewButton.onclick = () => {
    if (!current() || !plan || busy) return
    reset(); const ticket = serial
    const handle = onCaptureDocument?.()
    if (!handle) { status.textContent = 'Select a writable Markdown document first. No document is created.'; return }
    let result
    try { result = onInsertWiki(handle, { ...payload(), preview: true }) } catch { result = null }
    if (result?.outcome !== 'preview') { status.textContent = result?.message || 'The document changed or is unavailable.'; return }
    const estimate = estimateAppend(result, plan.index_markdown)
    previewText.textContent = describe(result, estimate, result.revision)
    if (estimate > WIKI_MAX_BYTES) { status.textContent = 'The pages to add would exceed 4 MiB. Lower the cap.'; return }
    previewed = { ticket, handle, revision: result.revision, key: JSON.stringify([result.add.map(p => p.page_id), result.index]) }
    writeButton.disabled = result.add.length === 0 && result.index === 'present'
    status.textContent = writeButton.disabled ? 'Nothing to add; every planned page is already in the document or stale.' : 'Preview shown. Write appends this block in one document change.'
  }

  writeButton.onclick = async () => {
    const shown = previewed
    if (!current() || !plan || !shown || shown.ticket !== serial || busy) return
    busy = true; writeButton.disabled = true; previewButton.disabled = true; planButton.disabled = true
    const ticket = serial, active = plan
    try {
      // Recheck the document against the preview that was shown.
      const again = onInsertWiki(shown.handle, { ...payload(), preview: true })
      if (again?.outcome !== 'preview' || again.revision !== shown.revision || JSON.stringify([again.add.map(p => p.page_id), again.index]) !== shown.key) { status.textContent = 'The document changed since the preview. Preview again.'; reset(); return }
      const fetched = []
      for (const page of again.add) {
        status.textContent = `Fetching page ${fetched.length + 1} of ${again.add.length}…`
        const result = await wikiRequest(fetch, base, 'page', { workspace_id: workspaceId, receipt: active.receipt, ...(copalWorkspace ? {copal_workspace:copalWorkspace} : {}), page: { page_id: page.page_id, file_id: page.file_id, rel_path: page.rel_path, bytes_est: page.bytes_est } }, aborter.signal)
        if (!current() || ticket !== serial) return
        if (result.outcome === 'replan') { newPlanNeeded(); status.textContent = 'The code changed during the run. Nothing was written. Create a new plan.'; return }
        if (result.outcome !== 'ok') { status.textContent = `Page failed: ${page.rel_path}. Nothing was written.`; return }
        fetched.push({ page_id: page.page_id, rel_path: page.rel_path, markdown: result.value.markdown })
      }
      const outcome = onInsertWiki(shown.handle, { indexMarkdown: active.index_markdown, pages: fetched })
      if (outcome?.outcome === 'queued') { status.textContent = `Appended ${outcome.appended.length} section(s), ${outcome.bytes} bytes. The document follows its normal save and History flow.`; reset(); previewButton.disabled = false }
      else if (outcome?.outcome === 'unchanged') { status.textContent = 'Nothing to append; the document already has these sections.'; reset() }
      else { status.textContent = outcome?.message || 'The document changed or insertion was unavailable.'; reset() }
    } catch { if (current() && ticket === serial) { status.textContent = 'Wiki write failed. Nothing was written.'; reset() } }
    finally { busy = false; if (current()) { planButton.disabled = false; if (plan) previewButton.disabled = false } }
  }

  closeButton.onclick = () => dialog.close()
  dialog.addEventListener('close', () => { disposed = true; aborter.abort() }, { once: true })
  const actions = document.createElement('footer'); actions.className = 'copal-dialog-actions'; actions.append(planButton, previewButton, writeButton, closeButton)
  dialog.append(title, note, capLabel, algorithmLabel, actions, status, planText, previewText)
  wireDialog(dialog); document.body.append(dialog); dialog.showModal(); cap.focus()
  return { dispose: closeButton.onclick }
}
