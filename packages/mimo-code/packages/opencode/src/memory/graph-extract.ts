import { generateObject } from "ai"
import z from "zod"
import { Effect } from "effect"
import { Provider } from "../provider"
import type { ProviderID, ModelID } from "../provider/schema"
import { Config } from "../config"
import { Log } from "../util"
import { getSharedMcpClient } from "./mcp-client"

const log = Log.create({ service: "memory.graph" })

// Canonical tag vocabulary — .futures/frankenmemory-update/tag-vocabulary.md
// (finalized 2026-07-08). The prompt instructs reuse-before-mint; fm's future
// groom tag_normalize op (G2) merges strays back into these.
const CANONICAL_TAGS = [
  "is", "has", "uses", "makes", "runs", "talks_to", "lives_in", "made_by",
  "works_on", "wants", "likes", "dislikes", "before", "blocks", "fixes", "about",
  "imports", "calls", "defines", "extends", "tests", "configures",
]

const NODE_KINDS = ["person", "project", "tool", "concept", "place", "event", "topic", "file"]

// Forgiving wire schema: small models drift — "type" for "kind", "nodes"
// for "entities", "source"/"target" for "src"/"dst", bare name strings for
// endpoints (all observed live from deepseek-v4-flash). Accept every
// observed alias, normalize below.
const EdgeSide = z.union([z.string(), z.object({ kind: z.string().optional(), name: z.string() })])
const Entity = z.object({
  name: z.string(),
  kind: z.string().optional().describe(`one of: ${NODE_KINDS.join(", ")}`),
  type: z.string().optional(),
})
const Edge = z.object({
  src: EdgeSide.optional(),
  source: EdgeSide.optional(),
  dst: EdgeSide.optional(),
  target: EdgeSide.optional(),
  dest: EdgeSide.optional(),
  tag: z.string(),
  fact: z.string().optional().describe("one plain sentence stating the relation"),
  cues: z.array(z.string()).optional(),
})
const ExtractionSchema = z.object({
  entities: z.array(Entity).optional(),
  nodes: z.array(Entity).optional(),
  cues: z
    .array(z.string())
    .optional()
    .describe("verbatim keywords/aliases from the text that someone might search later"),
  edges: z.array(Edge).optional(),
})

function normalizeSide(
  side: z.infer<typeof EdgeSide> | undefined,
  kinds: Map<string, string>,
): { kind: string; name: string } | undefined {
  if (side === undefined) return undefined
  const name = typeof side === "string" ? side : side.name
  const kind = (typeof side === "string" ? undefined : side.kind) ?? kinds.get(name.toLowerCase()) ?? "concept"
  return { kind, name }
}

const SYSTEM = `You extract a small knowledge graph from one chat turn.

Rules:
- Only extract what the text actually states. No speculation, no world knowledge.
- Entities: concrete people, projects, tools, places, events, concepts. Skip generic words.
- Edge tags: snake_case verb phrases, read left to right (src TAG dst).
  Use one of these canonical tags whenever one fits: ${CANONICAL_TAGS.join(", ")}.
  Mint a new tag ONLY when none of them fits.
- Do not store inverses ("blocked_by"); flip src/dst and use the canonical tag.
- Each edge carries "fact": one short plain sentence a stranger would understand.
- Cues: verbatim words/phrases from the text useful as future search entry points.
- Empty arrays are fine. A turn with nothing memorable yields nothing.
- Respond with a single JSON object EXACTLY in this shape (field names matter):
  {"entities":[{"kind":"person","name":"Ada"}],"cues":["ada","loom project"],"edges":[{"src":{"kind":"person","name":"Ada"},"tag":"works_on","dst":{"kind":"project","name":"loom"},"fact":"Ada works on the loom project."}]}`

/** Pick the cheapest configured NON-THINKING model by input cost
 * (management ruling 2026-07-09: reasoning models burn most of the
 * extraction spend on thinking tokens and garble structured output).
 * Falls back to cheapest-overall, then the session default.
 * `memory.graph.model` ("provider/model") overrides everything. */
export function resolveExtractionModel(cfg: {
  memory?: { graph?: { model?: string } }
  provider?: Record<string, { models?: Record<string, { cost?: { input?: number }; reasoning?: boolean }> }>
}): { providerID: ProviderID; modelID: ModelID } | undefined {
  const explicit = cfg.memory?.graph?.model
  if (explicit?.includes("/")) {
    const [providerID, ...rest] = explicit.split("/")
    return { providerID: providerID as ProviderID, modelID: rest.join("/") as ModelID }
  }
  let best: { providerID: string; modelID: string; cost: number; reasoning: boolean } | undefined
  for (const [pid, p] of Object.entries(cfg.provider ?? {})) {
    for (const [mid, m] of Object.entries(p.models ?? {})) {
      const cost = m.cost?.input
      if (cost === undefined) continue
      const reasoning = m.reasoning === true
      const better =
        !best ||
        // non-thinking beats thinking regardless of price...
        (!reasoning && best.reasoning) ||
        // ...and within the same class, cheaper wins.
        (reasoning === best.reasoning && cost < best.cost)
      if (better) best = { providerID: pid, modelID: mid, cost, reasoning }
    }
  }
  return best ? { providerID: best.providerID as ProviderID, modelID: best.modelID as ModelID } : undefined
}

/** Per-session throttle state (management ruling 2026-07-09: frequency is
 * tunable). Pure decision function so the policy is unit-testable. */
type ThrottleState = { turns: number; lastExtractMs: number }
const throttle = new Map<string, ThrottleState>()

export function shouldExtract(
  cfg: { every_n_turns?: number; min_interval_seconds?: number },
  state: ThrottleState,
  nowMs: number,
): boolean {
  const everyN = cfg.every_n_turns ?? 1
  if (state.turns % everyN !== 0) return false
  const minGapMs = (cfg.min_interval_seconds ?? 0) * 1000
  if (minGapMs > 0 && state.lastExtractMs > 0 && nowMs - state.lastExtractMs < minGapMs) return false
  return true
}

/** One extraction call + graph_upsert. Failures log and return — capture of
 * the raw/curated record must never depend on extraction succeeding. */
export const extractAndUpsert = Effect.fn("MemoryGraph.extract")(function* (input: {
  userText: string
  assistantText: string
  sessionID: string
  workspaceId: string
  workspacePath: string
}) {
  const config = yield* Config.Service
  const provider = yield* Provider.Service
  const cfg = yield* config.get()
  if (cfg.memory?.provider !== "frankenmemory") return
  if (cfg.memory?.graph?.enabled === false) return

  const state = throttle.get(input.sessionID) ?? { turns: 0, lastExtractMs: 0 }
  const go = shouldExtract(cfg.memory?.graph ?? {}, state, Date.now())
  state.turns += 1
  throttle.set(input.sessionID, state)
  if (!go) {
    log.info("graph extraction throttled", {
      sessionID: input.sessionID,
      turns: state.turns,
      everyN: cfg.memory?.graph?.every_n_turns ?? 1,
    })
    return
  }
  state.lastExtractMs = Date.now()

  const selection = resolveExtractionModel(cfg) ?? (yield* provider.defaultModel())
  const resolved = yield* provider.getModel(selection.providerID, selection.modelID)
  const language = yield* provider.getLanguage(resolved)

  const text = `USER:\n${input.userText}\n\nASSISTANT:\n${input.assistantText}`
  const result = yield* Effect.promise(() =>
    generateObject({
      model: language,
      temperature: 0,
      schema: ExtractionSchema,
      messages: [
        { role: "system", content: SYSTEM },
        { role: "user", content: text },
      ],
    }),
  )

  // Usage is TRACKED, never throttled (management ruling 2026-07-08).
  // The E1 gate report greps these lines for the token-burn numbers.
  log.info("graph extraction usage", {
    model: `${selection.providerID}/${selection.modelID}`,
    inputTokens: result.usage?.inputTokens,
    outputTokens: result.usage?.outputTokens,
    entities: (result.object.entities ?? result.object.nodes)?.length ?? 0,
    edges: result.object.edges?.length ?? 0,
    cues: result.object.cues?.length ?? 0,
  })

  const payload = result.object
  const rawEntities = payload.entities ?? payload.nodes ?? []
  const rawEdges = payload.edges ?? []
  const rawCues = payload.cues ?? []
  if (!rawEntities.length && !rawEdges.length && !rawCues.length) return

  const entities = rawEntities.map((e) => ({ kind: e.kind ?? e.type ?? "concept", name: e.name }))
  const kinds = new Map(entities.map((e) => [e.name.toLowerCase(), e.kind]))
  const edges = rawEdges.flatMap((e) => {
    const src = normalizeSide(e.src ?? e.source, kinds)
    const dst = normalizeSide(e.dst ?? e.target ?? e.dest, kinds)
    if (!src || !dst) return []
    return [{ src, tag: e.tag, dst, fact: e.fact ?? "" }]
  })
  const cues = [...rawCues, ...rawEdges.flatMap((e) => e.cues ?? [])]

  // Cues attach to the first extracted entity when present — entry points
  // need a node to land on. Turns with edges but no entities still work:
  // edge endpoints auto-create nodes fm-side.
  const anchor = entities[0] ?? edges[0]?.src
  const client = yield* Effect.promise(() => getSharedMcpClient())
  yield* Effect.promise(() =>
    client.callTool({
      name: "graph_upsert",
      arguments: {
        nodes: entities,
        edges,
        cues: anchor ? cues.map((cue) => ({ cue, node: anchor })) : [],
      },
    }),
  )
})

export const extractSafely = (input: Parameters<typeof extractAndUpsert>[0]) =>
  extractAndUpsert(input).pipe(
    Effect.catchCause((cause) =>
      Effect.sync(() => log.warn("graph extraction failed", { error: String(cause).slice(0, 500) })),
    ),
  )
