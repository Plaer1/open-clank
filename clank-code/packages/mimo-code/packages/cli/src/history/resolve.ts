import { Effect } from "effect"
import { eq, sql, Database } from "../storage"
import { MessageTable, SessionTable } from "../session/session.sql"
import type { MessageID, SessionID } from "../session/schema"

class LRU<K, V> {
  private map = new Map<K, V>()
  constructor(private readonly max: number) {}
  get(k: K): V | undefined {
    const v = this.map.get(k)
    if (v === undefined) return undefined
    this.map.delete(k)
    this.map.set(k, v)
    return v
  }
  set(k: K, v: V) {
    if (this.map.has(k)) this.map.delete(k)
    this.map.set(k, v)
    if (this.map.size > this.max) {
      const oldest = this.map.keys().next().value
      if (oldest !== undefined) this.map.delete(oldest)
    }
  }
}

export type Resolver = {
  projectID: (sessionID: string) => Effect.Effect<string>
  role: (messageID: string) => Effect.Effect<"user" | "assistant">
  summary: (messageID: string) => Effect.Effect<boolean>
}

export function makeResolver(): Resolver {
  const projectCache = new LRU<string, string>(512)
  const roleCache = new LRU<string, "user" | "assistant">(512)
  const summaryCache = new LRU<string, boolean>(512)

  return {
    projectID: (sessionID) =>
      Effect.sync(() => {
        const cached = projectCache.get(sessionID)
        if (cached) return cached
        const row = Database.use((db) =>
          db
            .select({ project_id: SessionTable.project_id })
            .from(SessionTable)
            .where(eq(SessionTable.id, sessionID as SessionID))
            .get(),
        )
        const projectID = row?.project_id ?? ""
        projectCache.set(sessionID, projectID)
        return projectID
      }),
    role: (messageID) =>
      Effect.sync(() => {
        const cached = roleCache.get(messageID)
        if (cached) return cached
        const row = Database.use((db) =>
          db
            .select({ role: sql<string>`json_extract(${MessageTable.data}, '$.role')` })
            .from(MessageTable)
            .where(eq(MessageTable.id, messageID as MessageID))
            .get(),
        )
        const role = row?.role === "user" ? ("user" as const) : ("assistant" as const)
        roleCache.set(messageID, role)
        return role
      }),
    summary: (messageID) =>
      Effect.sync(() => {
        const cached = summaryCache.get(messageID)
        if (cached !== undefined) return cached
        const row = Database.use((db) =>
          db
            .select({ summary: sql<number>`json_extract(${MessageTable.data}, '$.summary')` })
            .from(MessageTable)
            .where(eq(MessageTable.id, messageID as MessageID))
            .get(),
        )
        const summary = Boolean(row?.summary)
        summaryCache.set(messageID, summary)
        return summary
      }),
  }
}
