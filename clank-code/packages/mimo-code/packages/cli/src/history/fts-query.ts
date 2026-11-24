// Build an FTS5 MATCH expression from a free-form user query.
//
// Dual-column index: body_idx isolates Han from Han/Latin/digit neighbors.
// Query applies the same transform so phrases match mixed CJK (npm打包脚本2).
// Punctuation splits phrases (each phrase = one adjacent FTS phrase, AND-joined).
// Space-separated multi-word = token AND (order-free).
//
// Independent copy from memory/fts-query.ts so the two modules can evolve apart.
export type BuiltFtsQuery = { fts: string | null }

function segmentCjk(text: string): string {
  return text
    .replace(/([\p{Script=Han}])(?=\p{Script=Han})/gu, "$1 ")
    .replace(/([\p{Script=Han}])(?=[\p{L}\p{N}])/gu, "$1 ")
    .replace(/([\p{L}\p{N}])(?=\p{Script=Han})/gu, "$1 ")
    .replace(/\s+/g, " ")
    .trim()
}

function phrase(word: string): string {
  return `"${word.replaceAll('"', '""')}"`
}

const PUNCT = /[、，,。.；;：:！!？?（）()【】\[\]「」“”"'\|/\\]+/

export function buildFtsQuery(raw: string): BuiltFtsQuery {
  const q = (raw || "").toLowerCase().trim()
  if (!q) return { fts: null }

  const segments = q.split(PUNCT).map((s) => s.trim()).filter(Boolean)
  const fts: string[] = []

  const addSegment = (seg: string, adjacent: boolean) => {
    const words = seg.match(/[\p{L}\p{N}_]+/gu)?.filter(Boolean) ?? []
    if (!words.length) return
    if (adjacent) {
      // one adjacent phrase (punct segment / contiguous run)
      const joined = segmentCjk(words.join(" "))
      if (joined) fts.push(phrase(joined))
      return
    }
    // space-separated multi-word (order-free AND)
    for (const w of words) {
      const sw = segmentCjk(w)
      if (sw) fts.push(phrase(sw))
    }
  }

  if (segments.length >= 2) {
    // punct phrases: each segment is one adjacent phrase
    for (const seg of segments) addSegment(seg, true)
  } else {
    const seg = segments[0] ?? q
    addSegment(seg, !/\s/.test(seg))
  }
  return { fts: fts.length ? fts.join(" AND ") : null }
}

/** Back-compat: MATCH string only (null if nothing FTS-usable). */
export function buildFtsMatch(raw: string): string | null {
  return buildFtsQuery(raw).fts
}
