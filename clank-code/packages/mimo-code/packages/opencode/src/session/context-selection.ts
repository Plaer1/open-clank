import { createHash } from "node:crypto"
import { Database, and, desc, eq } from "@/storage"
import { ActorRegistryTable } from "@/actor/actor.sql"
import { MessageTable, PartTable } from "./session.sql"
import { MessageV2 } from "./message-v2"
import { SyncEvent } from "@/sync"
import type { SessionID } from "./schema"

export type SelectionAction = "omit" | "trim" | "restore"
export type SelectionPreview = { version: 1; sessionID: string; executionRevision: number; watermark: string;
  action: SelectionAction; parts: { id: string; messageID: string; digest: string; type: string }[] }
const hash = (value: unknown) => createHash("sha256").update(JSON.stringify(value)).digest("hex")

/** Both guards are acquired from this same native instance before entering here. */
export function selectContext(input: { sessionID: SessionID; stableChatID?: string; action: SelectionAction; partIDs: string[];
  runnerBusy: () => boolean; checkpointBusy: () => boolean; expected?: SelectionPreview }) {
  if (!input.partIDs.length || input.partIDs.length > 16 || new Set(input.partIDs).size !== input.partIDs.length) throw new Error("invalid selection")
  return Database.transaction((db) => {
    if (input.runnerBusy() || input.checkpointBusy()) throw new Error("context owner is busy")
    const actor = db.select().from(ActorRegistryTable).where(and(eq(ActorRegistryTable.session_id, input.sessionID), eq(ActorRegistryTable.actor_id, "main"))).get()
    if (!actor || actor.status !== "idle" || (input.stableChatID !== undefined && actor.chat_id !== input.stableChatID) || actor.last_error?.startsWith("managed execution pending:")) throw new Error("actor execution is unavailable")
    const latest = db.select({id: MessageTable.id, time: MessageTable.time_created}).from(MessageTable)
      .where(and(eq(MessageTable.session_id, input.sessionID), eq(MessageTable.agent_id, "main")))
      .orderBy(desc(MessageTable.time_created), desc(MessageTable.id)).get()
    const watermark = latest ? `${latest.time}:${latest.id}` : "empty"
    const parts = input.partIDs.map((id) => {
      const row = db.select({ part: PartTable, message: MessageTable }).from(PartTable)
        .innerJoin(MessageTable, eq(PartTable.message_id, MessageTable.id))
        .where(and(eq(PartTable.session_id, input.sessionID), eq(PartTable.id, id as typeof PartTable.$inferSelect.id), eq(MessageTable.agent_id, "main"))).get()
      if (!row || row.message.session_id !== input.sessionID) throw new Error("selected part unavailable")
      const parsed = MessageV2.Part.safeParse({ ...row.part.data, id: row.part.id, sessionID: row.part.session_id, messageID: row.part.message_id })
      if (!parsed.success) throw new Error("selected part unavailable")
      const part = parsed.data
      if (input.action === "omit" && !(part.type === "reasoning" || (part.type === "file" && MessageV2.isMedia(part.mime)))) throw new Error("unsupported selection")
      if (input.action === "trim" && !(part.type === "tool" && part.state.status === "completed" && !part.state.time.compacted && part.state.output.length > 4096)) throw new Error("unsupported selection")
      if (input.action === "restore" && !(part.type === "file" || part.type === "reasoning" || part.type === "tool") ) throw new Error("unsupported selection")
      if (input.action === "restore" && (!("contextSelection" in part) || !part.contextSelection || (part.type === "file" && !part.url) || (part.type === "reasoning" && !part.text) || (part.type === "tool" && part.state.status === "completed" && !!part.state.time.compacted))) throw new Error("original is unavailable")
      return {part, ref: { id: part.id, messageID: part.messageID, type: part.type, digest: hash(row.part.data) }}
    })
    const preview: SelectionPreview = {version:1, sessionID:input.sessionID, executionRevision:actor.execution_revision, watermark, action:input.action, parts:parts.map((p) => p.ref)}
    if (input.expected) {
      const expected = input.expected
      if (expected.version !== preview.version || expected.sessionID !== preview.sessionID ||
          expected.executionRevision !== preview.executionRevision || expected.watermark !== preview.watermark ||
          expected.action !== preview.action || expected.parts.length !== preview.parts.length ||
          expected.parts.some((part, index) => {
            const current = preview.parts[index]
            return part.id !== current.id || part.messageID !== current.messageID || part.type !== current.type || part.digest !== current.digest
          })) throw new Error("selection is stale")
      for (const {part} of parts) {
        if (part.type !== "file" && part.type !== "reasoning" && part.type !== "tool") throw new Error("unsupported selection")
        if (input.action === "restore") delete part.contextSelection
        else if (input.action === "omit" && part.type !== "tool") part.contextSelection = {version:1, action:"omit"}
        else if (input.action === "trim" && part.type === "tool") part.contextSelection = {version:1, action:"trim", head:1536, tail:1536}
        SyncEvent.run(MessageV2.Event.PartUpdated, {sessionID:input.sessionID, part, time:Date.now()})
      }
    }
    return preview
  }, {behavior:"immediate"})
}
