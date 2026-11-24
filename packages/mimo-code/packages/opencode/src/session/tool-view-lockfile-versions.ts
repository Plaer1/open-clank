/** A finite, lossy version-only view of complete npm v2/v3 lockfiles. */
import { parseJsonInput, type Node } from '../util/toon-subset'

export function npmLockfileVersions(input: string): { text: string; lockfileVersion: 2 | 3; rowCount: number } | undefined {
  try {
    const root = parseJsonInput(input)
    if (root.k !== 'o') return
    const field = (node: Extract<Node, {k:'o'}>, key: string) => node.v.find(([name]) => name === key)?.[1]
    const format = field(root, 'lockfileVersion')
    const packages = field(root, 'packages')
    if (format?.k !== 'a' || (format.v !== 2 && format.v !== 3) || packages?.k !== 'o' || !packages.v.length) return
    const safe = (value: string) => !/[\p{Cc}\p{Cf}]/u.test(value)
    const rows: [string, string][] = []
    for (const [path, entry] of packages.v) {
      if (!safe(path) || entry.k !== 'o') return
      const version = field(entry, 'version'), link = field(entry, 'link')
      if (version?.k !== 'a' || typeof version.v !== 'string' || !version.v || !safe(version.v)) return
      if (link !== undefined && (link.k !== 'a' || link.v !== false)) return
      rows.push([path, version.v])
    }
    const text = JSON.stringify(rows)
    if (Buffer.byteLength(text, 'utf8') > 51200) return
    return { text, lockfileVersion: format.v, rowCount: rows.length }
  } catch {
    return
  }
}
