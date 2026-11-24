import { Effect } from 'effect'
import z from 'zod'
import * as Tool from './tool'
import { compactJsonTokens } from '../session/tool-view-json'

const parameters = z.object({
  json: z.string().describe('Explicit JSON text to preview, at most 51,200 UTF-8 bytes.'),
}).strict()

export const JsonPreviewTool = Tool.define('json_preview', Effect.succeed({
  description: 'Preview lossless JSON whitespace removal on explicitly supplied text. Preserves JSON lexemes; no files or history are read. Reports UTF-8 byte counts, not token or cost savings.',
  parameters,
  execute: (args: z.infer<typeof parameters>, ctx: Tool.Context) => Effect.gen(function* () {
    if (ctx.abort.aborted) throw new Error('Cancelled')
    yield* ctx.ask({ permission: 'json_preview', patterns: ['*'], always: ['*'], metadata: {} })
    if (ctx.abort.aborted) throw new Error('Cancelled')
    const inputBytes = Buffer.byteLength(args.json, 'utf8')
    const preview = compactJsonTokens(args.json)
    if (ctx.abort.aborted) throw new Error('Cancelled')
    const outputBytes = preview === undefined ? inputBytes : Buffer.byteLength(preview, 'utf8')
    if (preview === undefined || outputBytes >= inputBytes) return {
      title: 'JSON preview bypassed',
      output: preview === undefined ? 'Bypassed: invalid, unsupported, or oversized JSON.' : 'Bypassed: no whitespace reduction.',
      metadata: { state: 'bypass', inputBytes, outputBytes: inputBytes, truncated: false },
    }
    return { title: 'JSON whitespace preview', output: preview,
      metadata: { state: 'preview', inputBytes, outputBytes, truncated: false } }
  }),
}))
