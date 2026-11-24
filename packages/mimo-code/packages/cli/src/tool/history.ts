import { Effect } from "effect"
import z from "zod"
import { History } from "@/history"
import { Provider } from "@/provider"
import { routeToolAttachment } from "@/session/tool-attachment"
import { cleanDataUrls } from "@/history/media"
import DESCRIPTION from "./history.txt"
import * as Tool from "./tool"
import { ManagedProvider } from "@/acp/managed-provider"
import { isAdmittedToolTimingTurn } from "@/session/tool-timing"
import type { ActorExecutionIdentity } from "@/actor/turn"
import { queryHistoryOutput } from "./history-output-query"

const KIND = z.enum([
  "user_text",
  "assistant_text",
  "tool_input",
  "tool_error",
  "reasoning",
  "tool_output",
])

// around() output can easily be tens of KB (multi-message contexts with full
// part bodies including reasoning/tool blocks). Capping below the global
// MAX_BYTES nudges agents toward "search → message_id → targeted Read" instead
// of one giant inline dump. Only history.around uses this; other tools keep
// the framework default.
const AROUND_MAX_BYTES = 20 * 1024

const parameters = z.object({
  operation: z.enum(["search", "around", "get", "media", "exact_output", "exact_output_query"]).describe(
    "search: FTS BM25; around: pull message context; get: full part body; media: attachment locators; exact_output: recover an eligible exact tool output page; exact_output_query: literal matches in an eligible exact output",
  ),
  // search params
  query: z.string().optional().describe("FTS query for search, or a literal up to 256 UTF-8 bytes for exact_output_query."),
  scope: z.enum(["project", "global"]).optional().describe("Standalone: project (default) or global. Managed: omit for the bound chat, or use global; project is unavailable."),
  session_id: z.string().optional().describe("Target chat id from a history search result. In managed mode, callback admission still uses the current engine session."),
  kind: z.array(KIND).optional(),
  tool_name: z.string().optional().describe("Filter to a specific tool (e.g. Bash, Read)"),
  time_after: z.number().optional().describe("Unix ms"),
  time_before: z.number().optional(),
  limit: z.number().optional().describe("Max 50, default 10 for search; integer 1–50, default 20 for exact_output_query"),
  // around params
  message_id: z.string().optional().describe("Anchor message id. Required for operation=around/get/media."),
  before: z.number().optional().describe("Default 5"),
  after: z.number().optional().describe("Default 5"),
  // get / media params
  part_id: z.string().optional().describe("Part id. Required for operation=get/media."),
  length: z.number().optional().describe("Max 8000 UTF-16 units for get."),
  offset: z.number().optional().describe("UTF-16 cursor into the part body."),
  revision: z.number().int().positive().optional().describe("Exact canonical part revision for exact_output."),
  content_hash: z.string().regex(/^[a-f0-9]{64}$/).optional().describe("Exact canonical part SHA-256 for exact_output."),
  byte_offset: z.number().int().min(0).max(51200).optional().describe("Exact-output byte offset; for a query, begin on a UTF-8 boundary."),
  byte_limit: z.number().int().min(1).max(8192).optional(),
  attachment: z.string().min(1).optional().describe("Explicit attachment locator returned by local history get."),
})

const BUDGET = 19500
const OMIT = "[More summaries omitted; narrow search/around or call get on the part_id you need.]"
const ANCHOR_TRUNCATED = "[Anchor truncated; remaining parts omitted — call get on the part_id you need.]"

function bounded(lines: string[]) {
  const result: string[] = []
  let bytes = 0
  let truncated = false
  for (const line of lines) {
    if (bytes + Buffer.byteLength(line) + 1 > BUDGET) {
      truncated = true
      break
    }
    result.push(line)
    bytes += Buffer.byteLength(line) + 1
  }
  if (truncated) result.push(OMIT)
  return { text: result.join("\n"), truncated }
}

// Keep the anchor even when it alone exceeds the budget: fully when it fits,
// otherwise partial (message header + as many part lines as fit). Fill the
// remaining budget around it in chronological order.
function aroundBlocks(
  messages: Array<{
    matched: boolean
    message_id: string
    time_created: number
    parts: Array<{ part_id: string; role: string; type: string; text: string }>
  }>,
) {
  const blocks = messages.map((m) => ({
    matched: m.matched,
    lines: [
      `${m.matched ? ">>>" : "---"} message_id=${m.message_id} time=${m.time_created}`,
      ...m.parts.map((p) => `part_id=${p.part_id} ${p.role} ${p.type}\n${p.text}`),
    ],
  }))
  const picked: typeof blocks = []
  let bytes = 0
  const limit = BUDGET - Buffer.byteLength(OMIT) - 1
  const take = (b: (typeof blocks)[number], allowPartial = false) => {
    const size = b.lines.reduce((n, line) => n + Buffer.byteLength(line) + 1, 0)
    if (bytes + size <= limit) {
      picked.push(b)
      bytes += size
      return true
    }
    if (!allowPartial || b.lines.length === 0) return false
    // Partial anchor: keep the message_id header plus every part line that still
    // fits, reserving room for the truncation marker.
    const partial: string[] = []
    let used = 0
    const markerSize = Buffer.byteLength(ANCHOR_TRUNCATED) + 1
    for (const line of b.lines) {
      const lineSize = Buffer.byteLength(line) + 1
      if (bytes + used + lineSize + markerSize > limit) break
      partial.push(line)
      used += lineSize
    }
    if (partial.length === 0) {
      // Even the header does not fit after earlier picks — still force it in
      // so the caller can address this message; drop whatever else was picked.
      const header = b.lines[0]!
      picked.length = 0
      picked.push({ ...b, lines: [header] })
      bytes = Buffer.byteLength(header) + 1
      return true
    }
    picked.push({
      ...b,
      lines: [...partial, ANCHOR_TRUNCATED],
    })
    bytes += used + markerSize
    return true
  }
  const anchor = blocks.find((b) => b.matched)
  if (anchor) take(anchor, true)
  for (const b of blocks) {
    if (b === anchor) continue
    if (!take(b)) break
  }
  const truncated =
    picked.length < blocks.length ||
    (anchor !== undefined && (picked[0] !== anchor || picked[0]!.lines.length !== anchor.lines.length))
  return { picked, truncated }
}

export const HistoryTool = Tool.define(
  "history",
  Effect.gen(function* () {
    const history = yield* History.Service
    const provider = yield* Provider.Service
    return {
      description: DESCRIPTION,
      parameters,
      execute: (args: z.infer<typeof parameters>, ctx) =>
        Effect.gen(function* () {
          if (args.operation === "exact_output_query") {
            const unavailable = () => ({title:"Exact output query unavailable", output:"Exact output unavailable.", metadata:{count:0}})
            const allowed = new Set(["operation","message_id","part_id","revision","content_hash","query","byte_offset","limit"])
            if (Object.keys(args).some(key => !allowed.has(key)) || !ManagedProvider.enabled() || ctx.abort.aborted ||
                !args.message_id || !args.part_id || !args.revision || !args.content_hash || typeof args.query !== "string" ||
                !args.query.isWellFormed() || Buffer.byteLength(args.query,"utf8") < 1 || Buffer.byteLength(args.query,"utf8") > 256 ||
                (args.limit !== undefined && (!Number.isSafeInteger(args.limit) || args.limit < 1 || args.limit > 50))) return unavailable()
            const actor = ctx.extra?.actorExecution as ActorExecutionIdentity | undefined
            if (!actor || actor.sessionID !== ctx.sessionID || !actor.rootOperationID || !actor.actorID ||
                !Number.isSafeInteger(actor.executionRevision) || actor.executionRevision < 0 || !actor.innerTurnID) return unavailable()
            const turn = {sessionID:actor.sessionID,rootOperationID:actor.rootOperationID,actorID:actor.actorID,
              executionRevision:actor.executionRevision,innerTurnID:actor.innerTurnID}
            const issuer=ManagedProvider.currentActorToolTimingIssuer(actor.sessionID,actor.actorID,actor.executionRevision,actor.rootOperationID)
            const scope=ManagedProvider.currentScope(actor.sessionID,actor.actorID,actor.executionRevision)
            const host=ManagedProvider.currentActorHostContext(actor.sessionID,actor.actorID,actor.executionRevision)
            const original=ManagedProvider.currentOriginalPromptHostContext(actor.sessionID,actor.rootOperationID)
            // Host context is intentionally returned as a shallow copy by ManagedProvider.
            const sameHost=(value:typeof host, expected:typeof host)=> {
              if (!value || !expected) return false
              return Object.keys(value).length===Object.keys(expected).length &&
                Object.keys(expected).every(key=>value[key as keyof typeof expected]===expected[key as keyof typeof expected])
            }
            const sameOriginal=()=> {
              const value=ManagedProvider.currentOriginalPromptHostContext(actor.sessionID,actor.rootOperationID!)
              return original === null ? value === null : original !== undefined && sameHost(value ?? undefined,original)
            }
            const current=()=> !ctx.abort.aborted && ManagedProvider.enabled() && !!scope && !!host && !!issuer?.isAlive() &&
              isAdmittedToolTimingTurn(actor,turn) && ManagedProvider.currentScope(actor.sessionID,actor.actorID,actor.executionRevision)===scope &&
              sameHost(ManagedProvider.currentActorHostContext(actor.sessionID,actor.actorID,actor.executionRevision),host) && sameOriginal() &&
              ManagedProvider.currentActorToolTimingIssuer(actor.sessionID,actor.actorID,actor.executionRevision,actor.rootOperationID!)===issuer
            if (!current()) return unavailable()
            const result = yield* Effect.tryPromise(() => queryHistoryOutput({revision:args.revision!,contentHash:args.content_hash!,
              literal:args.query!,start:args.byte_offset ?? 0,limit:args.limit ?? 20,current,
              readPage:(offset,limit)=>ManagedProvider.historyCall(ctx.sessionID,"exact_output",{
                rootOperationID:turn.rootOperationID,actorID:turn.actorID,executionRevision:turn.executionRevision,innerTurnID:turn.innerTurnID,
                messageID:args.message_id,partID:args.part_id,revision:args.revision,contentHash:args.content_hash,offset,limit,
              })})).pipe(Effect.catch(() => Effect.succeed(undefined)))
            if (!result || !current()) return unavailable()
            const output=JSON.stringify({revision:args.revision,contentHash:args.content_hash,outputSHA256:result.outputSHA256,
              outputBytes:result.outputBytes,query:args.query,start:args.byte_offset ?? 0,matches:result.matches,next_offset:result.nextOffset,
              original:{operation:"exact_output",message_id:args.message_id,part_id:args.part_id,revision:args.revision,content_hash:args.content_hash}})
            if (Buffer.byteLength(output,"utf8") > 50000) return unavailable()
            return {title:"Exact output query",output,metadata:{count:result.matches.length,truncated:false} as {count:number}}
          }
          if (args.operation === "exact_output") {
            const actor = ctx.extra?.actorExecution as {sessionID?:unknown; rootOperationID?:unknown; actorID?:unknown; executionRevision?:unknown; innerTurnID?:unknown} | undefined
            if (!ManagedProvider.enabled() || ctx.abort.aborted || !args.message_id || !args.part_id || !args.revision || !args.content_hash ||
                actor?.sessionID !== ctx.sessionID || typeof actor.rootOperationID !== "string" || typeof actor.actorID !== "string" ||
                typeof actor.executionRevision !== "number" || !Number.isSafeInteger(actor.executionRevision) || actor.executionRevision < 0 ||
                typeof actor.innerTurnID !== "string")
              return {title:"Exact output unavailable", output:"Exact output unavailable.", metadata:{count:0}}
            const result = yield* Effect.tryPromise(() => ManagedProvider.historyCall(ctx.sessionID, "exact_output", {
              rootOperationID:actor.rootOperationID, actorID:actor.actorID, executionRevision:actor.executionRevision,
              innerTurnID:actor.innerTurnID, messageID:args.message_id, partID:args.part_id, revision:args.revision,
              contentHash:args.content_hash, offset:args.byte_offset ?? 0, limit:args.byte_limit ?? 8192,
            })).pipe(Effect.catch(() => Effect.succeed(undefined)))
            const value = result as {state?:string; page?:{text?:unknown; revision?:unknown; contentHash?:unknown; outputSHA256?:unknown; outputBytes?:unknown; offset?:unknown; nextOffset?:unknown}} | undefined
            const page=value?.page, bytes=typeof page?.text==='string' ? Buffer.byteLength(page.text,'utf8') : -1
            const offset=args.byte_offset ?? 0, limit=args.byte_limit ?? 8192
            if (ctx.abort.aborted || value?.state !== "ready" || !page || typeof page.text !== "string" ||
                [...page.text].some(char=>{const n=char.codePointAt(0)!;return n>=0xd800 && n<=0xdfff}) ||
                page.revision!==args.revision || page.contentHash!==args.content_hash ||
                typeof page.outputSHA256!=='string' || !/^[a-f0-9]{64}$/.test(page.outputSHA256) ||
                !Number.isSafeInteger(page.outputBytes) || (page.outputBytes as number)<0 || (page.outputBytes as number)>51200 ||
                page.offset!==offset || bytes<0 || bytes>limit || offset+bytes>(page.outputBytes as number) ||
                page.nextOffset!==(offset+bytes < (page.outputBytes as number) ? offset+bytes : null))
              return {title:"Exact output unavailable", output:"Exact output unavailable.", metadata:{count:0}}
            const output=JSON.stringify({revision:page.revision,contentHash:page.contentHash,outputSHA256:page.outputSHA256,
              outputBytes:page.outputBytes,offset:page.offset,nextOffset:page.nextOffset,text:page.text})
            if (Buffer.byteLength(output,'utf8')>50000)
              return {title:"Exact output unavailable", output:"Exact output unavailable.", metadata:{count:0}}
            return {title:"Exact tool output", output, metadata:{count:1,truncated:false} as {count:number}}
          }
          if (args.operation === "search") {
            if (!args.query) {
              return {
                title: "History search: missing query",
                output: "operation=search requires a `query` argument.",
                metadata: { count: 0 },
              }
            }
            const hits = yield* history.search({
              query: args.query,
              scope: args.scope,
              // In managed mode this is the admitted engine session; callers
              // cannot select a different chat or owner through tool args.
              session_id: ManagedProvider.enabled() ? ctx.sessionID : args.session_id,
              kind: args.kind,
              tool_name: args.tool_name,
              time_after: args.time_after,
              time_before: args.time_before,
              limit: args.limit,
            })
            if (hits.length === 0) {
              return {
                title: "History search: 0 matches",
                output: `0 matches for "${args.query}". Try memory search if you haven't, or broaden the query.`,
                metadata: { count: 0 },
              }
            }
            const lines = [`Found ${hits.length} match${hits.length === 1 ? "" : "es"}:`, ""]
            for (const h of hits) {
              const kind = h.kind ?? "indexed part"
              const kindLabel = h.tool_name ? `${kind} · ${h.tool_name}` : kind
              lines.push(`### ${h.session_id} ${h.message_id}  (${kindLabel})`)
              lines.push(`Time: ${new Date(h.time_created).toISOString()}, Score: ${h.score.toFixed(3)}`)
              lines.push(h.snippet)
              lines.push("")
            }
            return {
              title: `History search: ${hits.length} match${hits.length === 1 ? "" : "es"}`,
              output: lines.join("\n"),
              metadata: { count: hits.length },
            }
          }

          if (args.operation === "get") {
            if (!args.message_id || !args.part_id) {
              return {
                title: "History get: missing ids",
                output: "operation=get requires `message_id` and `part_id`.",
                metadata: { count: 0 },
              }
            }
            const part = yield* history.part({
              session_id: ctx.sessionID,
              chat_id: ManagedProvider.enabled() ? args.session_id : undefined,
              message_id: args.message_id,
              part_id: args.part_id,
              length: args.length,
              offset: args.offset,
            })
            if (!part) {
              return {
                title: "History get: not found",
                output: `No part ${args.part_id} on message ${args.message_id}.`,
                metadata: { count: 0 },
              }
            }
            // The scoped part read above is the authority boundary. Bare upstream
            // get is only available locally and only after that binding succeeds.
            let locators: string[] = []
            if (!ManagedProvider.enabled()) {
              const detail = yield* history.get({ part_id: args.part_id, offset: args.offset, length: args.length })
                .pipe(Effect.catch(() => Effect.succeed(undefined)))
              if (detail) {
                locators = detail.attachments.map((item) => `attachment=${item.id} mime=${item.mime}`).slice(0, 64)
                if (args.attachment) {
                  const attachment = detail.attachments.find((item) => item.id === args.attachment)
                  if (!attachment || /^file:/i.test(attachment.url))
                    return { title: "History attachment unavailable", output: "Attachment unavailable; file URLs require normal read permission.", metadata: { count: 0 } }
                  const ref = [...ctx.messages].reverse().map((m) => m.info).find((m) => m.role === "user")
                  const model = (ctx.extra?.model as Provider.Model | undefined) ?? (ref?.role === "user"
                    ? yield* provider.getModel(ref.model.providerID, ref.model.modelID).pipe(Effect.catchDefect(() => Effect.succeed(undefined)))
                    : undefined)
                  if (!model || routeToolAttachment({ model, attachment, allowNative: true }) === "placeholder")
                    return { title: "History attachment unavailable", output: "Current model cannot display this attachment.", metadata: { count: 0 } }
                  return { title: "History attachment", output: "Requested attachment supplied through the media channel.", metadata: { count: 1 },
                    attachments: [{ type: "file" as const, mime: attachment.mime, url: attachment.url, filename: attachment.filename }] }
                }
              }
            } else if (args.attachment) {
              return { title: "History attachment unavailable", output: "Use media for Host-owned attachment locators.", metadata: { count: 0 } }
            }
            return {
              title: `History get ${args.part_id}`,
              output:
                `${part.type}${part.tool_name ? ` (${part.tool_name})` : ""}:\n${cleanDataUrls(part.text)}` +
                (part.has_more ? `\n\n[continued at offset ${part.next_offset}]` : "") +
                (locators.length ? `\n\nAttachments: ${locators.join(", ")}` : ""),
              metadata: { count: 1 },
            }
          }

          if (args.operation === "media") {
            if (!args.message_id || !args.part_id) {
              return {
                title: "History media: missing ids",
                output: "operation=media requires `message_id` and `part_id`.",
                metadata: { count: 0 },
              }
            }
            const attachments = yield* history.media({
              session_id: ctx.sessionID,
              chat_id: ManagedProvider.enabled() ? args.session_id : undefined,
              message_id: args.message_id,
              part_id: args.part_id,
            })
            if (attachments.length === 0) {
              return {
                title: "History media: none",
                output: "That part has no attachment locators.",
                metadata: { count: 0 },
              }
            }
            return {
              title: `History media: ${attachments.length} attachment(s)`,
              output: attachments
                .map(
                  (a) =>
                    `- ${a.filename ?? a.asset_id} (${a.mime_type ?? "unknown"}, ${a.byte_size ?? "?"} bytes) id=${a.asset_id}`,
                )
                .join("\n"),
              metadata: { count: attachments.length },
            }
          }

          // operation=around
          if (!args.message_id) {
            return {
              title: "History around: missing message_id",
              output: "operation=around requires a `message_id` argument.",
              metadata: { count: 0 },
            }
          }
          const around = yield* history.around({
            session_id: ctx.sessionID,
            chat_id: ManagedProvider.enabled() ? args.session_id : undefined,
            message_id: args.message_id,
            before: args.before,
            after: args.after,
          })
          if (around.messages.length === 0) {
            return {
              title: "History around: anchor not found",
              output: `No message with id ${args.message_id}.`,
              metadata: { count: 0 },
            }
          }
          const { picked, truncated } = aroundBlocks(around.messages)
          return {
            title: `History around ${args.message_id}`,
            output: [`Session ${around.session_id}. Summaries only; use history operation=get for full details.`,
              ...picked.flatMap((block) => block.lines), ...(truncated ? [OMIT] : [])].join("\n"),
            metadata: { count: around.messages.length, truncated },
          }
        }),
    }
  }),
)
