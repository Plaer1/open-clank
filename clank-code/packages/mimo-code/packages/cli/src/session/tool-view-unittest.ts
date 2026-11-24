/** Recognize only the complete supported unittest-style success output. */
export function summarizeUnittest(input: string): {body: string; omitted: number} | undefined {
  if (typeof input !== 'string' || Buffer.byteLength(input, 'utf8') > 51200) return
  const match = /^(\.+)\n(-{70}\nRan ([1-9][0-9]*) (test|tests) in ((?:0|[1-9][0-9]*)\.[0-9]{3}) ?s\n\nOK\n)$/.exec(input)
  if (!match || match[0] !== input || !Number.isFinite(Number(match[5]))) return
  const omitted = match[1].length
  if (match[3] !== String(omitted) || match[4] !== (omitted === 1 ? 'test' : 'tests')) return
  return {body: match[2], omitted}
}
