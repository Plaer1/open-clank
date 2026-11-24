import { randomUUID } from "node:crypto"
import z from "zod"
import { Effect } from "effect"
import { HttpClient, HttpClientRequest } from "effect/unstable/http"
import * as Tool from "./tool"
import { convertHTMLToMarkdown, markWebfetchHtml, markWebfetchMarkdown } from "./webfetch-view"
import { toolViewProducerCurrent } from "../session/tool-view-producer-admission"
import DESCRIPTION from "./webfetch.txt"
import { isImageAttachment } from "@/util/media"
import { assertSafeUrl } from "@/util/ssrf"

const MAX_RESPONSE_SIZE = 5 * 1024 * 1024 // 5MB
const DEFAULT_TIMEOUT = 30 * 1000 // 30 seconds
const MAX_TIMEOUT = 120 * 1000 // 2 minutes
const PAGE_CHARS = 9_000
const PAGE_LINES = 500
const RETAINED_LIMIT = 4
const RETENTION_MS = 5 * 60 * 1000

type ResponseEvidence = {
  id: string
  final_url: string | null
  fetched_at: string
  media_type: string | null
  kind: "text" | "markdown" | "html" | "image"
}

type RetainedResponse = {
  scope: string
  url: string
  format: z.infer<typeof parameters>["format"]
  title: string
  output: string
  evidence: ResponseEvidence
  expiresAt: number
}

function scopeOf(ctx: Tool.Context): string {
  const execution = ctx.extra?.actorExecution as { rootOperationID?: string; executionRevision?: number } | undefined
  return JSON.stringify([ctx.sessionID, ctx.actorID ?? ctx.agent, execution?.rootOperationID, execution?.executionRevision])
}

function safeFinalUrl(url: string | undefined): string | null {
  if (!url) return null
  const value = new URL(url)
  value.username = ""
  value.password = ""
  // A redirected URL can contain a server-minted credential in its query.
  value.search = ""
  value.hash = ""
  return value.href
}

function pageEnd(output: string, offset: number): number {
  let end = Math.min(output.length, offset + PAGE_CHARS)
  let nextLine = offset
  for (let index = 0; index < PAGE_LINES; index++) {
    const newline = output.indexOf("\n", nextLine)
    if (newline < 0 || newline >= end) break
    nextLine = newline + 1
    if (index === PAGE_LINES - 1) end = nextLine
  }
  const last = output.charCodeAt(end - 1)
  if (end < output.length && last >= 0xd800 && last <= 0xdbff) end--
  return end
}

function resultPage(response: RetainedResponse, offset: number) {
  const end = pageEnd(response.output, offset)
  const continuation = end < response.output.length ? `${response.evidence.id}.${end}` : null
  return {
    title: response.title,
    output: response.output.slice(offset, end) + (
      continuation ? `\n\n[More of this response is available with webfetch continuation ${continuation}.]` : ""
    ),
    metadata: {
      webfetch: {
        ...response.evidence,
        offset,
        next: continuation,
        complete: continuation === null,
        expires_at: continuation ? new Date(response.expiresAt).toISOString() : null,
      },
    },
  }
}

const parameters = z.object({
  url: z.string().describe("The URL to fetch content from"),
  format: z
    .enum(["text", "markdown", "html"])
    .default("markdown")
    .describe("The format to return the content in (text, markdown, or html). Defaults to markdown."),
  timeout: z.number().describe("Optional timeout in seconds (max 120)").optional(),
  continuation: z.string().regex(/^[a-f0-9-]{36}\.[0-9]{1,8}$/).optional()
    .describe("Read more from the same response. Use the same URL and format with the continuation token returned by webfetch."),
})

export const WebFetchTool = Tool.define(
  "webfetch",
  Effect.gen(function* () {
    const http = yield* HttpClient.HttpClient
    const httpOk = HttpClient.filterStatusOk(http)
    const retained = new Map<string, RetainedResponse>()

    return {
      description: DESCRIPTION,
      parameters,
      execute: (params: z.infer<typeof parameters>, ctx: Tool.Context) =>
        Effect.gen(function* () {
          if (!params.url.startsWith("http://") && !params.url.startsWith("https://")) {
            throw new Error("URL must start with http:// or https://")
          }

          if (!params.continuation) yield* Effect.promise(() => assertSafeUrl(params.url))

          yield* ctx.ask({
            permission: "webfetch",
            patterns: [params.url],
            always: ["*"],
            metadata: {
              url: params.url,
              format: params.format,
              timeout: params.timeout,
            },
          })

          if (params.continuation) {
            const [id = "", offsetText = ""] = params.continuation.split(".")
            const response = retained.get(id)
            const offset = Number(offsetText)
            if (!response || response.expiresAt <= Date.now()) {
              retained.delete(id)
              throw new Error("Web response continuation expired or is unavailable. Start a new fetch to read the current page.")
            }
            if (response.scope !== scopeOf(ctx) || response.url !== params.url || response.format !== params.format
                || ctx.abort.aborted || offset < 1 || offset >= response.output.length) {
              throw new Error("Web response continuation is unavailable for this request.")
            }
            return resultPage(response, offset)
          }

          const timeout = Math.min((params.timeout ?? DEFAULT_TIMEOUT / 1000) * 1000, MAX_TIMEOUT)

          // Build Accept header based on requested format with q parameters for fallbacks
          let acceptHeader = "*/*"
          switch (params.format) {
            case "markdown":
              acceptHeader = "text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, text/html;q=0.7, */*;q=0.1"
              break
            case "text":
              acceptHeader = "text/plain;q=1.0, text/markdown;q=0.9, text/html;q=0.8, */*;q=0.1"
              break
            case "html":
              acceptHeader =
                "text/html;q=1.0, application/xhtml+xml;q=0.9, text/plain;q=0.8, text/markdown;q=0.7, */*;q=0.1"
              break
            default:
              acceptHeader =
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
          }
          const headers = {
            "User-Agent":
              "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            Accept: acceptHeader,
            "Accept-Language": "en-US,en;q=0.9",
          }

          const request = HttpClientRequest.get(params.url).pipe(HttpClientRequest.setHeaders(headers))

          // Retry with honest UA if blocked by Cloudflare bot detection (TLS fingerprint mismatch)
          const response = yield* httpOk.execute(request).pipe(
            Effect.catchIf(
              (err) =>
                err.reason._tag === "StatusCodeError" &&
                err.reason.response.status === 403 &&
                err.reason.response.headers["cf-mitigated"] === "challenge",
              () =>
                httpOk.execute(
                  HttpClientRequest.get(params.url).pipe(
                    HttpClientRequest.setHeaders({ ...headers, "User-Agent": "openclank" }),
                  ),
                ),
            ),
            Effect.timeoutOrElse({ duration: timeout, orElse: () => Effect.die(new Error("Request timed out")) }),
          )

          // Block SSRF via redirect: if the response was redirected, validate final URL
          // Effect's interruptible response wraps the actual Fetch response once.
          // Only a real Response supplies a final URL; never infer it from the request.
          const carrier = response as unknown as {source?:unknown;original?:{source?:unknown}}
          const candidate = carrier.source ?? carrier.original?.source
          const source = candidate instanceof Response ? candidate : undefined
          if (source?.url && source.url !== params.url) {
            yield* Effect.promise(() => assertSafeUrl(source.url))
          }

          // Check content length
          const contentLength = response.headers["content-length"]
          if (contentLength && parseInt(contentLength) > MAX_RESPONSE_SIZE) {
            throw new Error("Response too large (exceeds 5MB limit)")
          }

          const arrayBuffer = yield* response.arrayBuffer
          if (arrayBuffer.byteLength > MAX_RESPONSE_SIZE) {
            throw new Error("Response too large (exceeds 5MB limit)")
          }

          const contentType = response.headers["content-type"] || ""
          const mime = contentType.split(";")[0]?.trim().toLowerCase() || ""
          const title = `${params.url} (${contentType})`
          const evidence = {
            id: randomUUID(),
            final_url: safeFinalUrl(source?.url),
            fetched_at: new Date().toISOString(),
            media_type: mime || null,
          }

          if (isImageAttachment(mime)) {
            const base64Content = Buffer.from(arrayBuffer).toString("base64")
            return {
              title,
              output: "Image fetched successfully",
              metadata: { webfetch: { ...evidence, kind: "image" as const, complete: true, next: null } },
              attachments: [
                {
                  type: "file" as const,
                  mime,
                  url: `data:${mime};base64,${base64Content}`,
                },
              ],
            }
          }

          const content = new TextDecoder().decode(arrayBuffer)

          const html = mime === "text/html" || mime === "application/xhtml+xml"
          const markdown = mime === "text/markdown" || mime === "text/x-markdown"
          let output = content
          let kind: ResponseEvidence["kind"] = html ? "html" : markdown ? "markdown" : "text"
          if (html && params.format === "markdown") {
            output = convertHTMLToMarkdown(content)
            kind = "markdown"
          } else if (html && params.format === "text") {
            output = yield* Effect.promise(() => extractTextFromHTML(content))
            kind = "text"
          }
          const captured: RetainedResponse = {
            scope: scopeOf(ctx), url: params.url, format: params.format, title, output,
            evidence: { ...evidence, kind }, expiresAt: Date.now() + RETENTION_MS,
          }
          const result = resultPage(captured, 0)
          if (result.metadata.webfetch.next) {
            // A transformed page may exceed the downloaded-body bound.
            if (Buffer.byteLength(output, "utf8") > MAX_RESPONSE_SIZE) {
              return {
                ...result,
                output: result.output.slice(0, pageEnd(output, 0)) + "\n\n[Remaining text exceeded the retention limit.]",
                metadata: { webfetch: { ...result.metadata.webfetch, next: null, expires_at: null, truncation: "retention_limit" } },
              }
            }
            for (const [id, entry] of retained) {
              if (entry.expiresAt <= Date.now()) retained.delete(id)
            }
            if (retained.size >= RETAINED_LIMIT) {
              const first = retained.keys().next().value
              if (first) retained.delete(first)
            }
            retained.set(evidence.id, captured)
            return result
          }

          if (!source?.url || !ctx.callID) return result
          const witness = { sessionID: ctx.sessionID, messageID: ctx.messageID, callID: ctx.callID,
            requestedURL: params.url, finalURL: source.url }
          if (params.format === "html" && mime === "text/html" && toolViewProducerCurrent(ctx, "html-markdown-v1")) {
            return markWebfetchHtml(result, witness, ctx)
          }
          if (params.format === "markdown" && (mime === "text/html" || mime === "text/markdown" || mime === "text/x-markdown")
              && toolViewProducerCurrent(ctx, "markdown-slim-v1")) {
            return markWebfetchMarkdown(result, witness, mime, ctx)
          }
          return result
        }).pipe(Effect.orDie),
    }
  }),
)

async function extractTextFromHTML(html: string) {
  let text = ""
  let skipContent = false

  const rewriter = new HTMLRewriter()
    .on("script, style, noscript, iframe, object, embed", {
      element() {
        skipContent = true
      },
      text() {
        // Skip text content inside these elements
      },
    })
    .on("*", {
      element(element) {
        // Reset skip flag when entering other elements
        if (!["script", "style", "noscript", "iframe", "object", "embed"].includes(element.tagName)) {
          skipContent = false
        }
      },
      text(input) {
        if (!skipContent) {
          text += input.text
        }
      },
    })
    .transform(new Response(html))

  await rewriter.text()
  return text.trim()
}

