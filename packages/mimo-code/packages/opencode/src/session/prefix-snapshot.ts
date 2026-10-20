import { createHash } from "node:crypto"
import { jsonSchema, tool, type Tool as AITool } from "ai"
import { asSchema } from "@ai-sdk/provider-utils"
import { Effect } from "effect"
import { and, Database, eq, lt } from "@/storage"
import type { Permission } from "@/permission"
import type { MessageID, SessionID } from "./schema"
import { SessionPrefixSnapshotTable, type SessionPrefixToolSnapshot } from "./session.sql"

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

export function toolsHash(tools: Record<string, AITool>, activeTools: string[]) {
  return hash(
    activeTools.toSorted().flatMap((name) => {
      const item = tools[name]
      return item ? [{ name, description: item.description, inputSchema: item.inputSchema }] : []
    }),
  )
}

export async function snapshotTools(tools: Record<string, AITool>, activeTools: string[]) {
  return Promise.all(
    activeTools.flatMap((name) => {
      const item = tools[name]
      if (!item) return []
      return [
        Promise.resolve(asSchema(item.inputSchema).jsonSchema).then(
          (input_schema): SessionPrefixToolSnapshot => ({ name, description: item.description, input_schema }),
        ),
      ]
    }),
  )
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
    Database.use((db) =>
      db
        .select()
        .from(SessionPrefixSnapshotTable)
        .where(
          and(
            eq(SessionPrefixSnapshotTable.session_id, sessionID),
            eq(SessionPrefixSnapshotTable.profile_key, key),
          ),
        )
        .get(),
    ),
  )
})

export const pin = Effect.fn("SessionPrefixSnapshot.pin")(function* (input: {
  sessionID: SessionID
  profileKey: string
  system: string[]
  toolsHash: string
  tools: SessionPrefixToolSnapshot[]
  watermarkMessageID: MessageID
}) {
  const now = Date.now()
  yield* Effect.sync(() =>
    Database.use((db) =>
      db
        .insert(SessionPrefixSnapshotTable)
        .values({
          session_id: input.sessionID,
          profile_key: input.profileKey,
          system: input.system,
          system_hash: systemHash(input.system),
          tools_hash: input.toolsHash,
          tools: input.tools,
          watermark_message_id: input.watermarkMessageID,
          revision: 1,
          created_at: now,
          updated_at: now,
        })
        .onConflictDoNothing()
        .run(),
    ),
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
    if (isCurrent(current, input.system, input.toolsHash)) return current
    const updated = yield* Effect.sync(() =>
      Database.use((db) =>
        db
          .update(SessionPrefixSnapshotTable)
          .set({
            system: input.system,
            system_hash: systemHash(input.system),
            tools_hash: input.toolsHash,
            tools: input.tools,
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
          .get(),
      ),
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
