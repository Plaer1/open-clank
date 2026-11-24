import { Effect } from 'effect'
import z from 'zod'
import * as Tool from './tool'
import * as Token from '../util/token'
import { yamlToBoundedJson } from '../util/toon-yaml'
import { decodeToonSubset, encodeToonSubset, encodeToonLines, decodeToonEvents } from '../util/toon-subset'

const segment = z.union([z.string().refine(value => value.isWellFormed() && Buffer.byteLength(value, 'utf8') <= 256), z.number().int().nonnegative().safe()])
const path = z.array(segment).min(1).max(32)
const scalar = z.union([z.null(), z.boolean(), z.number().int().safe().refine(value => !Object.is(value, -0)), z.string().refine(value => value.isWellFormed())])
const projection = z.array(z.discriminatedUnion('op', [
  z.object({ op: z.literal('omit'), path }).strict(),
  z.object({ op: z.literal('replace'), path, value: scalar }).strict(),
])).max(16)

const parameters = z.object({
  direction: z.enum(['encode', 'decode']),
  inputFormat: z.enum(['json', 'yaml']).optional().describe('Encode input format; JSON is the default. YAML is normalized to canonical JSON and comments and style are not preserved.'),
  compare: z.boolean().optional().describe('Encode-only comparison of the same full value against compact JSON; character-based estimates exclude prompt, recovery and cache costs.'),
  output: z.enum(['document', 'events']).optional().describe('Decode-only buffered event list; events are returned only after complete validation.'),
  text: z.string().describe('Explicit input text, at most 51,200 UTF-8 bytes.'),
  options: z.object({ delimiter: z.enum(['comma','tab','pipe']).optional(), indentSize: z.number().int().min(1).max(8).optional() }).strict().optional(),
  projection: projection.optional().describe('Encode-only explicit omit or scalar replacement operations; this is not security redaction.'),
}).strict()

export const ToonPreviewTool = Tool.define('toon_preview', Effect.succeed({
  description: 'Convert explicit JSON or YAML text to TOON, or restricted TOON to JSON. YAML conversion normalizes values to JSON and does not preserve comments or style. Restricted TOON 4.4 subset: ordered JSON objects, primitive arrays and uniform scalar-object and keyed scalar tables, with null, booleans, strings and safe integers. Only canonical subset TOON is decoded; unsupported keyed forms, fractions, exponents, negative zero, unsafe integers, duplicate keys and unsupported forms are refused. No files or network are accessed. Optional delimiter and indentation select the same bounded codec profile; defaults are comma and two spaces. Optional encode-only data operations produce a derived projection and disclose changed values; the original input remains in the request and this is not security redaction. Encode consumes a bounded line iterator; optional decode events consume asynchronous lines but the request and tool result are buffered until validation completes. Reports UTF-8 bytes. An explicit comparison gives a rough character-based estimate against compact JSON, not measured tokens or net savings; prompt, recovery and cache costs are excluded.',
  parameters,
  execute: (args: z.infer<typeof parameters>, ctx: Tool.Context) => Effect.gen(function* () {
    if (ctx.abort.aborted) throw new Error('Cancelled')
    yield* ctx.ask({ permission: 'toon_preview', patterns: ['*'], always: ['*'], metadata: {} })
    if (ctx.abort.aborted) throw new Error('Cancelled')
    const inputBytes = Buffer.byteLength(args.text, 'utf8')
    const effective = args.options === undefined ? {} : { options: { delimiter: args.options.delimiter ?? 'comma', indentSize: args.options.indentSize ?? 2 } }
    if (args.compare === true && (args.direction !== 'encode' || args.output === 'events' || args.projection !== undefined)) return {
      title: 'TOON preview unavailable', output: 'Refused: comparison requires encoding the same complete value without projection.',
      metadata: { state: 'refused', inputBytes, outputBytes: 0, truncated: false, ...effective },
    }
    const yaml = args.inputFormat === 'yaml'
    const jsonInput = yaml && args.direction === 'encode' && inputBytes <= 51200 ? yamlToBoundedJson(args.text) : yaml ? undefined : args.text
    if (ctx.abort.aborted) throw new Error('Cancelled')
    const encoded = jsonInput !== undefined && inputBytes <= 51200 && args.direction === 'encode' && args.output !== 'events' ? encodeToonSubset(jsonInput, args.options, args.projection) : undefined
    let converted: string | undefined
    if (encoded) {
      try {
        const lines: string[] = []
        let total = 0
        for (const line of encodeToonLines(jsonInput!, args.options, args.projection)) {
          if (ctx.abort.aborted) throw new Error('Cancelled')
          total += Buffer.byteLength(line, 'utf8') + (lines.length ? 1 : 0)
          if (total > 51200) throw new Error('Output too large')
          lines.push(line)
        }
        const text = lines.join('\n')
        if (text === encoded.text) converted = text
      } catch { if (ctx.abort.aborted) throw new Error('Cancelled') }
    } else if (!yaml && args.direction === 'decode' && inputBytes <= 51200 && args.projection === undefined) {
      if (args.output !== 'events') converted = decodeToonSubset(args.text, args.options)?.json
      else converted = yield* Effect.promise(async () => {
        try {
          async function* lines() { for (const line of args.text.split('\n')) { if (ctx.abort.aborted) throw new Error('Cancelled'); yield line } }
          const events: unknown[] = []
          let bytes = 2, complete = false
          for await (const event of decodeToonEvents(lines(), args.options, ctx.abort)) {
            if (ctx.abort.aborted || complete) return undefined
            if (event.type === 'complete') complete = true
            const size = Buffer.byteLength(JSON.stringify(event), 'utf8') + (events.length ? 1 : 0)
            bytes += size
            if (bytes > 51200) return undefined
            events.push(event)
          }
          return complete && !ctx.abort.aborted ? JSON.stringify(events) : undefined
        } catch { return undefined }
      })
    }
    if (ctx.abort.aborted) throw new Error('Cancelled')
    if (converted === undefined || Buffer.byteLength(converted, 'utf8') > 51200) return {
      title: 'TOON preview unavailable', output: 'Refused: invalid, unsupported, or oversized input for the restricted TOON profile.',
      metadata: { state: 'refused', inputBytes, outputBytes: 0, truncated: false, ...effective },
    }
    const comparison = args.compare === true && encoded ? (() => {
      const compactJsonEstimatedTokens=Token.estimate(encoded.canonicalJson), toonEstimatedTokens=Token.estimate(converted)
      return {method:'character_heuristic_v1',compactJsonEstimatedTokens,toonEstimatedTokens,
        deltaEstimatedTokens:compactJsonEstimatedTokens-toonEstimatedTokens,
        compactJsonBytes:Buffer.byteLength(encoded.canonicalJson,'utf8'),toonBytes:Buffer.byteLength(converted,'utf8'),
        excluded:['prompt_instructions','recovery','provider_cache'],netSavings:'unknown'}
    })() : undefined
    return { title: 'TOON preview', output: converted,
      metadata: { state: 'preview', inputBytes, outputBytes: Buffer.byteLength(converted, 'utf8'), truncated: false, ...effective, ...(encoded?.projection === undefined ? {} : { projection: encoded.projection }), ...(comparison ? {comparison} : {}), ...(yaml ? { inputNormalization: { from: 'yaml', to: 'canonical_json', commentsAndStylePreserved: false } } : {}) } }
  }),
}))
