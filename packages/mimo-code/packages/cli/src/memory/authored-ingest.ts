import { RecoverableError } from "@/tool/recoverable"

export type AuthoredSection = { anchor: string; content: string }
export type FmIngestScope = { owner: string; workspaceId: string }

// Dumb and stable beats clever and flappy: split on ## headings, whole-file
// fallback when there are none. Each section's content carries its heading
// so the record reads standalone; the engine hashes content, so renaming a
// heading is an edit like any other.
export function splitSections(body: string): AuthoredSection[] {
  const sections: AuthoredSection[] = []
  let anchor = ""
  let buf: string[] = []
  const flush = () => {
    const text = buf.join("\n").trim()
    if (text) sections.push({ anchor, content: anchor ? `${anchor}\n${text}` : text })
    buf = []
  }
  for (const line of body.split("\n")) {
    const m = line.match(/^##\s+(.+?)\s*$/)
    if (m) {
      flush()
      anchor = m[1]
    } else {
      buf.push(line)
    }
  }
  flush()
  return sections
}

/** Project one authored memory file into fm. body === null wipes the
 * file's projection (the file left the disk). Failures are logged and
 * swallowed — ingest is a maintenance mirror, never a search blocker. */
export async function ingestAuthoredFile(
  _absPath: string,
  _body: string | null,
  _scope: FmIngestScope,
): Promise<boolean> {
  throw new RecoverableError("Legacy authored-file memory ingest requires an admitted Host source owner")
}
