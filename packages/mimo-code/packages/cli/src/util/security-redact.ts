const PATTERNS: Array<[RegExp, string]> = [
  [/\b(Bearer|Token)\s+[A-Za-z0-9._\-+/=]{16,}/gi, "$1 <redacted>"],
  [/\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}/g, "<redacted-jwt>"],
  [/\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/g, "<redacted-aws-key>"],
  [/\bgh[pousr]_[A-Za-z0-9]{20,}\b/g, "<redacted-gh-token>"],
  [/\bsk-ant-[A-Za-z0-9_\-]{20,}\b/g, "<redacted-anthropic-key>"],
  [/\bsk-[A-Za-z0-9_\-]{20,}\b/g, "<redacted-openai-key>"],
  [/\bxox[abprs]-[A-Za-z0-9\-]{10,}\b/g, "<redacted-slack-token>"],
  [
    /\b((?:api|access|refresh|secret|client|auth)[_-]?(?:key|token|secret|password))(\s*[:=]\s*)["']?[A-Za-z0-9._\-+/=]{12,}["']?/gi,
    "$1$2<redacted>",
  ],
]

const PEM_BLOCK = /-----BEGIN [A-Z ]+-----[\s\S]*?-----END [A-Z ]+-----/g
const PEM_BEGIN = /-----BEGIN [A-Z ]+-----/
const PEM_END = /-----END [A-Z ]+-----/

/** Mandatory sink-boundary redaction. This intentionally has no raw-mode bypass. */
export function securityRedact(text: string): string {
  return PATTERNS.reduce(
    (current, [pattern, replacement]) => current.replace(pattern, replacement),
    text.replace(PEM_BLOCK, "<redacted-pem-block>"),
  )
}

export class StreamingSecurityRedactor {
  private pending = ""
  private inPem = false
  private droppingLongToken = false

  constructor(private readonly holdback = 512) {}

  push(value: string, final = false): string {
    this.pending += value
    const output: string[] = []
    const holdback = Math.max(0, this.holdback)
    const maxBuffer = Math.max(64 * 1024, holdback * 2)
    while (this.pending) {
      if (this.droppingLongToken) {
        const boundary = this.pending.search(/[ \t\r\n]/)
        if (boundary < 0) {
          this.pending = ""
          if (final) this.droppingLongToken = false
          break
        }
        this.pending = this.pending.slice(boundary)
        this.droppingLongToken = false
        continue
      }

      if (this.inPem) {
        const end = PEM_END.exec(this.pending)
        if (!end) {
          if (final) {
            this.pending = ""
            this.inPem = false
          } else if (this.pending.length > 128) {
            this.pending = this.pending.slice(-128)
          }
          break
        }
        this.pending = this.pending.slice(end.index + end[0].length)
        this.inPem = false
        continue
      }

      const begin = PEM_BEGIN.exec(this.pending)
      if (begin) {
        output.push(securityRedact(this.pending.slice(0, begin.index)))
        output.push("<redacted-pem-block>")
        this.pending = this.pending.slice(begin.index + begin[0].length)
        this.inPem = true
        continue
      }

      const marker = "-----BEGIN "
      let partial = this.pending.lastIndexOf(marker)
      if (partial < 0) {
        const limit = Math.min(marker.length - 1, this.pending.length)
        for (let length = limit; length > 0; length--) {
          if (marker.startsWith(this.pending.slice(-length))) {
            partial = this.pending.length - length
            break
          }
        }
      }
      if (partial >= 0) {
        output.push(securityRedact(this.pending.slice(0, partial)))
        this.pending = this.pending.slice(partial)
        if (final) {
          output.push("<redacted-pem-block>")
          this.pending = ""
        } else if (this.pending.length > maxBuffer) {
          output.push("<redacted-pem-block>")
          this.pending = this.pending.slice(-128)
          this.inPem = true
        }
        break
      }

      if (final) {
        output.push(securityRedact(this.pending))
        this.pending = ""
        break
      }
      const newline = Math.max(this.pending.lastIndexOf("\n"), this.pending.lastIndexOf("\r"))
      if (newline >= 0) {
        output.push(securityRedact(this.pending.slice(0, newline + 1)))
        this.pending = this.pending.slice(newline + 1)
        continue
      }
      const target = this.pending.length - holdback
      if (target <= 0) break
      const split = Math.max(
        this.pending.lastIndexOf(" ", target),
        this.pending.lastIndexOf("\t", target),
        this.pending.lastIndexOf("\r", target),
        this.pending.lastIndexOf("\n", target),
      )
      if (split < 0) {
        if (this.pending.length > maxBuffer) {
          output.push("<redacted-long-token>")
          this.pending = ""
          this.droppingLongToken = true
        }
        break
      }
      output.push(securityRedact(this.pending.slice(0, split + 1)))
      this.pending = this.pending.slice(split + 1)
    }
    return output.join("")
  }

  finish() {
    return this.push("", true)
  }
}
