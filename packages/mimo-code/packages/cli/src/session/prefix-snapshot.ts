import { createHash } from "node:crypto"
import type { Tool as AITool } from "ai"
import { jsonSchema, tool } from "ai"
import { asSchema } from "@ai-sdk/provider-utils"
import { Effect } from "effect"
import { and, Database, eq, lt, sql } from "@/storage"
import type { Permission } from "@/permission"
import type { MessageID, SessionID } from "./schema"
import { SessionPrefixSnapshotTable, type SessionPrefixToolSnapshot } from "./session.sql"
import { transferDetachedTool } from "./tool-definition-provenance"
import { valid as validProvenance, sameReceipt, type PrefixProvenance } from "./prefix-provenance"
import { projectSchemaAnnotations } from "./tool-schema-annotations"

export type Info = typeof SessionPrefixSnapshotTable.$inferSelect

type Profile = {
  providerID: string
  modelID: string
  agent: string
  agentID: string
  harness: string
  systemMode: string
  system: string
  format?: unknown
  permission: Permission.Ruleset
  gcfViewIdentity?: string
  toolViewIdentity?: string
  schemaViewIdentity?: string
}

function stableStringify(value: unknown): string {
  if (value === null || typeof value === "string" || typeof value === "boolean") return JSON.stringify(value)
  if (typeof value === "number") return Number.isFinite(value) ? JSON.stringify(value) : "null"
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`
  if (typeof value !== "object") return "null"
  return `{${Object.keys(value)
    .toSorted()
    .flatMap((key) => {
      const item = (value as Record<string, unknown>)[key]
      if (item === undefined || typeof item === "function" || typeof item === "symbol") return []
      return [`${JSON.stringify(key)}:${stableStringify(item)}`]
    })
    .join(",")}}`
}

function hash(value: unknown) {
  return createHash("sha256").update(stableStringify(value)).digest("hex")
}

export function profileKey(input: Profile) {
  return hash(input)
}

export function systemHash(system: string[]) {
  return hash(system)
}

export function isCurrent(snapshot: Pick<Info, "system_hash" | "tools_hash">, system: string[], toolsHash: string) {
  return snapshot.system_hash === systemHash(system) && snapshot.tools_hash === toolsHash
}

export function toolsHash(items: SessionPrefixToolSnapshot[]) {
  return hash(items)
}

export async function resolveTools(tools: Record<string, AITool>, activeTools: string[], annotation?: {eligible:(name:string)=>boolean}) {
  const resolved = await Promise.all(
    activeTools.flatMap((name) => {
      const item = tools[name]
      if (!item) return []
      const schema = asSchema(item.inputSchema)
      return [Promise.resolve(schema.jsonSchema).then((schemaValue) => {
        // Match the actual JSON-persisted representation and detach it from a
        // lazy provider's mutable schema object before sharing it with the SDK.
        const input_schema = JSON.parse(JSON.stringify(schemaValue)) as typeof schemaValue
        const current = { ...item, inputSchema: jsonSchema(input_schema, { validate: schema.validate }) } as AITool
        transferDetachedTool(item, current, input_schema)
        return {
          snapshot: { name, description: item.description, input_schema } satisfies SessionPrefixToolSnapshot,
          current,
        }
      })]
    }),
  )
  const snapshots=resolved.map((item) => item.snapshot)
  const currentTools=Object.fromEntries(resolved.map((item) => [item.snapshot.name, item.current]))
  if (!annotation) return {snapshots,tools:currentTools}
  try {
    if (snapshots.length>128 || Buffer.byteLength(JSON.stringify(snapshots),'utf8')>524288) return {snapshots,tools:currentTools}
    let changed=false
    const projected=snapshots.map(snapshot=>{
      if (!annotation.eligible(snapshot.name)) return snapshot
      const value=projectSchemaAnnotations(snapshot.input_schema)
      if (value===undefined) return snapshot
      changed=true
      return {...snapshot,input_schema:value as typeof snapshot.input_schema}
    })
    if (!changed) return {snapshots,tools:currentTools}
    const tools=Object.fromEntries(projected.map(snapshot=>{
      const canonical=currentTools[snapshot.name]!
      if (snapshots.find(item=>item.name===snapshot.name)===snapshot) return [snapshot.name,canonical]
      const schema=asSchema(canonical.inputSchema)
      const current={...canonical,inputSchema:jsonSchema(snapshot.input_schema,{validate:schema.validate})} as AITool
      return [snapshot.name,current]
    }))
    return {snapshots:projected,tools,canonicalSnapshots:snapshots,canonicalTools:currentTools}
  } catch { return {snapshots,tools:currentTools} }
}

export async function snapshotTools(tools: Record<string, AITool>, activeTools: string[]) {
  return (await resolveTools(tools, activeTools)).snapshots
}

/**
 * Restore the frozen advertised tool prefix for compaction. These are
 * intentionally schema-only: normal turns keep their current execute-bearing
 * tools, while a compactor can reproduce the persisted prefix without gaining
 * a second execution authority.
 */
export function restoreTools(items: SessionPrefixToolSnapshot[]) {
  return Object.fromEntries(
    items.map((item) => [
      item.name,
      tool({
        description: item.description,
        inputSchema: jsonSchema(item.input_schema),
      }),
    ]),
  )
}

export const get = Effect.fn("SessionPrefixSnapshot.get")(function* (sessionID: SessionID, key: string) {
  return yield* Effect.sync(() =>
    Database.use((db) => {
      const hasProvenance = db.all<{name:string}>("PRAGMA table_info('session_prefix_snapshot')").some((column) => column.name === "provenance")
      const row = db
        .select({ session_id:SessionPrefixSnapshotTable.session_id, profile_key:SessionPrefixSnapshotTable.profile_key,
          system:SessionPrefixSnapshotTable.system, system_hash:SessionPrefixSnapshotTable.system_hash,
          tools_hash:SessionPrefixSnapshotTable.tools_hash, tools:SessionPrefixSnapshotTable.tools,
          watermark_message_id:SessionPrefixSnapshotTable.watermark_message_id, revision:SessionPrefixSnapshotTable.revision,
          created_at:SessionPrefixSnapshotTable.created_at, updated_at:SessionPrefixSnapshotTable.updated_at,
          provenance:hasProvenance ? sql<string | null>`CASE WHEN length(provenance) <= 65536 THEN provenance ELSE NULL END` : sql<string | null>`NULL` })
        .from(SessionPrefixSnapshotTable)
        .where(
          and(eq(SessionPrefixSnapshotTable.session_id, sessionID), eq(SessionPrefixSnapshotTable.profile_key, key)),
        )
        .get()
      if (!row) return row
      let provenance: PrefixProvenance | null = null
      try {
        const value = row.provenance ? JSON.parse(row.provenance) : null
        if (validProvenance(value)) provenance = value
      } catch { /* malformed historical evidence cannot authorize reuse */ }
      return { ...row, provenance, provenanceSupported:hasProvenance }
    }),
  )
})

export const pin = Effect.fn("SessionPrefixSnapshot.pin")(function* (input: {
  sessionID: SessionID
  profileKey: string
  system: string[]
  toolsHash: string
  tools: SessionPrefixToolSnapshot[]
  provenance?: PrefixProvenance
  watermarkMessageID: MessageID
}) {
  const now = Date.now()
  yield* Effect.sync(() =>
    Database.use((db) => {
      const hasProvenance = db.all<{name:string}>("PRAGMA table_info('session_prefix_snapshot')").some((column) => column.name === "provenance")
      if (!hasProvenance) {
        // Drizzle can enumerate all mapped columns on insert, including the
        // optional column absent in the recognized legacy profile.
        return db.run(sql`INSERT INTO session_prefix_snapshot
          (session_id, profile_key, system, system_hash, tools_hash, tools, watermark_message_id, revision, created_at, updated_at)
          VALUES (${input.sessionID}, ${input.profileKey}, ${JSON.stringify(input.system)}, ${systemHash(input.system)},
            ${input.toolsHash}, ${JSON.stringify(input.tools)}, ${input.watermarkMessageID}, 1, ${now}, ${now})
          ON CONFLICT DO NOTHING`)
      }
      return db
        .insert(SessionPrefixSnapshotTable)
        .values({
          session_id: input.sessionID,
          profile_key: input.profileKey,
          system: input.system,
          system_hash: systemHash(input.system),
          tools_hash: input.toolsHash,
          tools: input.tools,
          ...(input.provenance ? {provenance: input.provenance} : {}),
          watermark_message_id: input.watermarkMessageID,
          revision: 1,
          created_at: now,
          updated_at: now,
        })
        .onConflictDoNothing()
        .run()
    }),
  )
  const snapshot = yield* get(input.sessionID, input.profileKey)
  if (!snapshot) return yield* Effect.die(new Error("Failed to read pinned session prefix snapshot"))
  return snapshot
})

export const rotate = Effect.fn("SessionPrefixSnapshot.rotate")(function* (input: {
  sessionID: SessionID
  profileKey: string
  system: string[]
  toolsHash: string
  tools: SessionPrefixToolSnapshot[]
  provenance?: PrefixProvenance
  watermarkMessageID: MessageID
}) {
  // Rotations can race when two run-loop workers observe a changed capability
  // set. The revision predicate makes every write compare-and-swap; a losing
  // worker rereads and either observes its desired value or retries from the
  // newer revision. That prevents a stale read from silently overwriting a
  // newer frozen prefix.
  for (;;) {
    const current = yield* get(input.sessionID, input.profileKey)
    if (!current) return yield* pin(input)
    if (isCurrent(current, input.system, input.toolsHash) && sameReceipt(current.provenance, input.provenance, current.provenanceSupported)) return current
    const updated = yield* Effect.sync(() =>
      Database.use((db) => {
        const hasProvenance = db.all<{name:string}>("PRAGMA table_info('session_prefix_snapshot')").some((column) => column.name === "provenance")
        return db
          .update(SessionPrefixSnapshotTable)
          .set({
            system: input.system,
            system_hash: systemHash(input.system),
            tools_hash: input.toolsHash,
            tools: input.tools,
            ...(hasProvenance ? {provenance: input.provenance ?? null} : {}),
            watermark_message_id: input.watermarkMessageID,
            revision: current.revision + 1,
            updated_at: Date.now(),
          })
          .where(
            and(
              eq(SessionPrefixSnapshotTable.session_id, input.sessionID),
              eq(SessionPrefixSnapshotTable.profile_key, input.profileKey),
              eq(SessionPrefixSnapshotTable.revision, current.revision),
            ),
          )
          .returning({ revision: SessionPrefixSnapshotTable.revision })
          .get()
      }),
    )
    if (updated) {
      const snapshot = yield* get(input.sessionID, input.profileKey)
      if (snapshot) return snapshot
    }
  }
})

export const advance = Effect.fn("SessionPrefixSnapshot.advance")(function* (input: {
  sessionID: SessionID
  profileKey: string
  revision: number
  watermarkMessageID: MessageID
}) {
  yield* Effect.sync(() =>
    Database.use((db) =>
      db
        .update(SessionPrefixSnapshotTable)
        .set({ watermark_message_id: input.watermarkMessageID, updated_at: Date.now() })
        .where(
          and(
            eq(SessionPrefixSnapshotTable.session_id, input.sessionID),
            eq(SessionPrefixSnapshotTable.profile_key, input.profileKey),
            eq(SessionPrefixSnapshotTable.revision, input.revision),
            lt(SessionPrefixSnapshotTable.watermark_message_id, input.watermarkMessageID),
          ),
        )
        .run(),
    ),
  )
})

export * as SessionPrefixSnapshot from "./prefix-snapshot"
