/** Conservative eligibility for lossy views; this is not an XML parser. */
import { parseJsonInput, type Node } from '../util/toon-subset'

const lossy = new Set([
  'json-shape-v1', 'source-outline-v1', 'line-cap-v1', 'unified-diff-v1',
  'html-markdown-v1', 'markdown-slim-v1', 'npm-lockfile-versions-v1',
  'structured-search-v2', 'progress-redraw-v1', 'base64-lines-v1',
  'tap13-v1', 'unittest-success-v1', 'cargo-artifacts-v1',
])
const html = new Set(('a abbr address area article aside audio b base bdi bdo blockquote body br button canvas caption cite code col colgroup data datalist dd del details dfn dialog div dl dt em embed fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 head header hgroup hr html i iframe img input ins kbd label legend li link main map mark menu meta meter nav noscript object ol optgroup option output p picture pre progress q rp rt ruby s samp script search section select slot small source span strong style sub summary sup table tbody td template textarea tfoot th thead time title tr track u ul var video wbr').split(' '))

function markup(value: string): boolean {
  // JSON escaping is conservatively recognized even when this is not a JSON source.
  value = value.replace(/\\u003c/ig, '<').replace(/\\u003e/ig, '>')
  const pattern = /<\/?([A-Za-z][A-Za-z0-9:-]*)(?:\s+(?:[^<>"']|"[^"<>]*"|'[^'<>]*')*)?\s*\/?>/y
  for (let i = value.indexOf('<'); i !== -1; i = value.indexOf('<', i + 1)) {
    pattern.lastIndex = i
    const tag = pattern.exec(value)
    if (!tag || !html.has(tag[1]!.toLowerCase())) return true
    i += tag[0].length - 1
  }
  return false
}
function jsonMarkup(node: Node): boolean {
  if (node.k === 'a') return typeof node.v === 'string' && markup(node.v)
  if (node.k === 'l') return node.v.some(jsonMarkup)
  return node.v.some(([key, value]) => markup(key) || jsonMarkup(value))
}
export function protectedSpanBypass(profile: string, input: string): boolean {
  if (!lossy.has(profile)) return false
  if (Buffer.byteLength(input, 'utf8') > 51200 || !input.isWellFormed() || markup(input)) return true
  if (profile === 'json-shape-v1') {
    try { return jsonMarkup(parseJsonInput(input)) } catch { return true }
  }
  return false
}
