/** Exact local step metadata. The returned observation is never an authority. */
import { Database, and, eq } from "@/storage"
import { sql } from "drizzle-orm"
import { MessageTable, PartTable } from "./session.sql"
import { MessageID, PartID, SessionID } from "./schema"
import { CaptureMembership, matchesActionRef } from "./capture-membership"

export function readObservedStep(input: {sessionID: string; messageID: string; stepID: string; membershipRevision: number; stableChatID: string}) {
  return Database.transaction((db) => {
    const row = db.select({
      part: sql<string | null>`CASE WHEN length(CAST(${PartTable.data} AS BLOB)) <= 49152 THEN ${PartTable.data} ELSE NULL END`,
      message: sql<string | null>`CASE WHEN length(CAST(${MessageTable.data} AS BLOB)) <= 262144 THEN ${MessageTable.data} ELSE NULL END`,
      actor: MessageTable.agent_id,
    }).from(PartTable).innerJoin(MessageTable, and(eq(MessageTable.id,PartTable.message_id),eq(MessageTable.session_id,PartTable.session_id)))
      .where(and(eq(PartTable.id,PartID.make(input.stepID)),eq(PartTable.message_id,MessageID.make(input.messageID)),
        eq(PartTable.session_id,SessionID.make(input.sessionID)))).get()
    if (!row?.part || !row.message) throw new Error("observed step unavailable")
    const part = JSON.parse(row.part), message = JSON.parse(row.message)
    const checked = CaptureMembership.safeParse(part?.captureMembership)
    if (!checked.success || part?.type !== "step-start" || message?.role !== "assistant") throw new Error("observed step unavailable")
    const value = checked.data, execution = message?.metadata?.managedExecution
    if (value.sessionID !== input.sessionID || value.messageID !== input.messageID || value.stepID !== input.stepID
        || value.hostChatID !== input.stableChatID || value.revision !== input.membershipRevision
        || row.actor !== value.actorID || execution?.sessionID !== value.sessionID || execution?.actorID !== value.actorID
        || execution?.executionRevision !== value.executionRevision || execution?.rootOperationID !== value.rootOperationID
        || execution?.innerTurnID !== value.innerTurnID
        || value.members.some((member,index) => member.sequence !== index
          || value.members.findIndex((other) => other.actionID === member.actionID || other.toolCallID === member.toolCallID) !== index
          || (member.state === "complete" ? !member.actionRef || !matchesActionRef(member.actionRef,value,member.actionID,member.toolCallID) : member.actionRef !== undefined)))
      throw new Error("observed step changed")
    return {version:1 as const, assurance:"unverified_local_observation" as const, sessionID:value.sessionID,
      messageID:value.messageID, stepID:value.stepID, membershipRevision:value.revision, accountID:value.accountID,
      hostChatID:value.hostChatID, workspaceID:value.workspaceID, actorID:value.actorID,
      executionRevision:value.executionRevision, rootOperationID:value.rootOperationID, innerTurnID:value.innerTurnID,
      coverage:value.coverage, members:value.members.map((member) => ({sequence:member.sequence,state:member.state,
        ...(member.state === "complete" ? {actionRef:member.actionRef!} : {})}))}
  }, {behavior:"deferred"})
}

/** Bounded same-message candidate discovery; results remain unverified observations. */
export function catalogObservedSteps(input: {sessionID:string; messageID:string; stableChatID:string; actionRef:string}) {
  // Inspect sizes first, then ask SQLite only for the bounded type and step bodies.
  // The aggregate cap bounds JSON parsing work even for unrelated large tool parts.
  const rows = Database.transaction((db) => {
    const metadata = db.select({id:PartTable.id,size:sql<number>`length(CAST(${PartTable.data} AS BLOB))`})
      .from(PartTable).where(and(eq(PartTable.message_id,MessageID.make(input.messageID)),
        eq(PartTable.session_id,SessionID.make(input.sessionID)))).orderBy(PartTable.id).limit(129).all()
    if (metadata.length > 128 || metadata.some((row) => !Number.isSafeInteger(row.size) || row.size < 0)
        || metadata.reduce((total,row) => total + row.size,0) > 16 * 1024 * 1024)
      throw new Error("candidate catalog unavailable")
    const steps: {id:typeof PartTable.$inferSelect.id; data:string}[] = []
    for (const row of metadata) {
      const typed = db.select({type:sql<string | null>`CASE WHEN json_valid(${PartTable.data})
        AND json_type(${PartTable.data}, '$.type') = 'text'
        AND length(json_extract(${PartTable.data}, '$.type')) <= 32
        THEN json_extract(${PartTable.data}, '$.type') ELSE NULL END`})
        .from(PartTable).where(and(eq(PartTable.id,row.id),eq(PartTable.message_id,MessageID.make(input.messageID)),
          eq(PartTable.session_id,SessionID.make(input.sessionID)))).get()
      if (!typed?.type) throw new Error("candidate catalog unavailable")
      if (typed.type !== "step-start") continue
      if (row.size > 49152) throw new Error("candidate catalog unavailable")
      const part = db.select({data:sql<string>`${PartTable.data}`}).from(PartTable)
        .where(and(eq(PartTable.id,row.id),eq(PartTable.message_id,MessageID.make(input.messageID)),
          eq(PartTable.session_id,SessionID.make(input.sessionID)))).get()
      if (!part) throw new Error("candidate catalog unavailable")
      steps.push({id:row.id,data:part.data})
    }
    return steps
  }, {behavior:"deferred"})
  const candidates: {stepID:string; membershipRevision:number; coverage:string; workspaceID:string; accountID:string}[] = []
  for (const row of rows) {
    const data = JSON.parse(row.data!)
    if (data?.type !== "step-start" || data.captureMembership === undefined) continue
    const parsed = CaptureMembership.safeParse(data.captureMembership)
    if (!parsed.success) throw new Error("candidate catalog unavailable")
    if (!parsed.data.members.some((member) => member.state === "complete" && member.actionRef === input.actionRef)) continue
    const observed = readObservedStep({sessionID:input.sessionID,messageID:input.messageID,stepID:row.id,
      membershipRevision:parsed.data.revision,stableChatID:input.stableChatID})
    if (!observed.members.some((member) => member.state === "complete" && member.actionRef === input.actionRef)) throw new Error("candidate changed")
    candidates.push({stepID:observed.stepID,membershipRevision:observed.membershipRevision,coverage:observed.coverage,
      workspaceID:observed.workspaceID,accountID:observed.accountID})
    if (candidates.length > 20) throw new Error("candidate catalog unavailable")
  }
  return {version:1 as const,assurance:"unverified_local_observation" as const,sessionID:input.sessionID,
    messageID:input.messageID,hostChatID:input.stableChatID,candidates}
}

/** Exact current native tail for a Host-owned intent; no file authority. */
export function observedStepHead(input:{sessionID:string;stableChatID:string}) {
  const row=Database.use(db=>db.get(sql`SELECT id,time_created AS t FROM message INDEXED BY message_session_time_created_id_idx WHERE session_id=${input.sessionID} ORDER BY time_created DESC,id DESC LIMIT 1`) as {id:string;t:number}|undefined)
  if (row && !Number.isSafeInteger(row.t)) throw new Error("head unavailable")
  return {version:1 as const,sessionID:input.sessionID,hostChatID:input.stableChatID,head:row?{messageID:row.id,timeCreated:row.t}:null}
}

/** Bounded range of local observed membership; this is not file authority. */
export function rangeObservedSteps(input:{sessionID:string;stableChatID:string;fromMessageID:string;toMessageID:string;cursor?:string}) {
  type Position = [string,string,string,string,number,number,number,string,string]
  const result = Database.transaction((db) => {
    const boundary = (id:string) => db.get(sql`SELECT id,time_created AS t FROM message WHERE session_id=${input.sessionID} AND id=${id}`) as {id:string;t:number}|undefined
    const from=boundary(input.fromMessageID), to=boundary(input.toMessageID)
    const head=db.get(sql`SELECT id,time_created AS t FROM message INDEXED BY message_session_time_created_id_idx WHERE session_id=${input.sessionID} ORDER BY time_created DESC,id DESC LIMIT 1`) as {id:string;t:number}|undefined
    if (!head || head.id!==to?.id || head.t!==to.t) throw new Error("native head changed")
    if (!from || !to || !Number.isSafeInteger(from.t) || !Number.isSafeInteger(to.t) || from.t>to.t || (from.t===to.t && from.id>to.id)) throw new Error("range unavailable")
    let position:Position=[input.sessionID,input.stableChatID,from.id,to.id,from.t,to.t,from.t,from.id,""]
    if (input.cursor) {
      const text=Buffer.from(input.cursor,"base64url").toString("utf8")
      const value:unknown=JSON.parse(text)
      if (Buffer.from(text).toString("base64url")!==input.cursor || !Array.isArray(value) || value.length!==9 ||
          value[0]!==input.sessionID || value[1]!==input.stableChatID || value[2]!==from.id || value[3]!==to.id ||
          value[4]!==from.t || value[5]!==to.t || !Number.isSafeInteger(value[6]) || typeof value[7]!=="string" || typeof value[8]!=="string") throw new Error("range cursor unavailable")
      position=value as Position
      if (position[6]<from.t || position[6]>to.t) throw new Error("range cursor unavailable")
      const current=boundary(position[7]); if (!current || current.t!==position[6]) throw new Error("range cursor unavailable")
    }
    const rows=db.all(sql`SELECT id,time_created AS t FROM message INDEXED BY message_session_time_created_id_idx
      WHERE session_id=${input.sessionID} AND (time_created>${position[6]} OR (time_created=${position[6]} AND id>=${position[7]}))
      AND (time_created<${to.t} OR (time_created=${to.t} AND id<=${to.id})) ORDER BY time_created,id LIMIT 33`) as {id:string;t:number}[]
    const steps:ReturnType<typeof readObservedStep>[]=[]; let scanned=0, scannedBytes=0, unknown=false, more=false
    for (let i=0;i<rows.length;i++) {
      const row=rows[i]!; if (i===32) { more=true; break }
      const after=row.id===position[7] && row.t===position[6] ? position[8] : ""
      const parts=db.all(sql`SELECT id,length(CAST(data AS BLOB)) AS size FROM part INDEXED BY part_message_id_id_idx
        WHERE message_id=${row.id} AND session_id=${input.sessionID} AND id>${after} ORDER BY id LIMIT 129`) as {id:string;size:number}[]
      for (const part of parts) {
        if (scanned>=128 || steps.length>=20) { more=true; break }
        scanned++; position=[input.sessionID,input.stableChatID,from.id,to.id,from.t,to.t,row.t,row.id,part.id]
        if (!Number.isSafeInteger(part.size) || part.size<0 || (scannedBytes+=part.size)>16777216) throw new Error("range unavailable")
        const typed=db.get(sql`SELECT CASE WHEN json_valid(data) AND json_type(data,'$.type')='text' AND length(json_extract(data,'$.type'))<=32 THEN json_extract(data,'$.type') ELSE NULL END AS type FROM part WHERE id=${part.id} AND message_id=${row.id} AND session_id=${input.sessionID}`) as {type:string|null}|undefined
        if (!typed?.type) throw new Error("range unavailable")
        if (typed.type!=="step-start") continue
        if (part.size>49152) throw new Error("range unavailable")
        const saved=db.get(sql`SELECT data FROM part WHERE id=${part.id} AND message_id=${row.id} AND session_id=${input.sessionID}`) as {data:string}|undefined
        if (!saved) throw new Error("range changed")
        const parsed=CaptureMembership.safeParse(JSON.parse(saved.data)?.captureMembership)
        if (!parsed.success) { unknown=true; continue }
        const item=readObservedStep({sessionID:input.sessionID,stableChatID:input.stableChatID,messageID:row.id,stepID:part.id,membershipRevision:parsed.data.revision})
        if (item.members.some(member=>member.state==="pending")) unknown=true
        steps.push(item)
      }
      if (more) break
      position=[input.sessionID,input.stableChatID,from.id,to.id,from.t,to.t,row.t,row.id,"~"]
    }
    return {steps,unknown,more,position,head:{messageID:head.id,timeCreated:head.t}}
  }, {behavior:"deferred"})
  return {version:1 as const,assurance:"unverified_local_observation" as const,coverage:result.steps.length?"partial" as const:"unavailable" as const,
    sessionID:input.sessionID,hostChatID:input.stableChatID,fromMessageID:input.fromMessageID,toMessageID:input.toMessageID,
    head:result.head,unknown:result.unknown,steps:result.steps,...(result.more?{nextCursor:Buffer.from(JSON.stringify(result.position)).toString("base64url")}:{})}
}


/** First-profile bounded same-root recorded steps, never a completeness proof. */
export function scanObservedTurn(input:{sessionID:string;stableChatID:string;rootOperationID:string;headMessageID:string;anchorMessageID:string}) {
  return Database.transaction(db=>{
    const head=db.get(sql`SELECT id,time_created AS t FROM message INDEXED BY message_session_time_created_id_idx WHERE session_id=${input.sessionID} ORDER BY time_created DESC,id DESC LIMIT 1`) as {id:string;t:number}|undefined
    if (!head || head.id!==input.headMessageID || !Number.isSafeInteger(head.t)) throw new Error('observed turn changed')
    const anchor=db.get(sql`SELECT id,time_created AS t,length(CAST(data AS BLOB)) AS size FROM message WHERE session_id=${input.sessionID} AND id=${input.anchorMessageID}`) as {id:string;t:number;size:number}|undefined
    if (!anchor || !Number.isSafeInteger(anchor.t)) throw new Error('observed anchor unavailable')
    const older=db.all(sql`SELECT id,time_created AS t,length(CAST(data AS BLOB)) AS size FROM message INDEXED BY message_session_time_created_id_idx WHERE session_id=${input.sessionID} AND (time_created<${anchor.t} OR (time_created=${anchor.t} AND id<${anchor.id})) ORDER BY time_created DESC,id DESC LIMIT 65`) as {id:string;t:number;size:number}[]
    const newer=db.all(sql`SELECT id,time_created AS t,length(CAST(data AS BLOB)) AS size FROM message INDEXED BY message_session_time_created_id_idx WHERE session_id=${input.sessionID} AND (time_created>${anchor.t} OR (time_created=${anchor.t} AND id>${anchor.id})) ORDER BY time_created,id LIMIT 64`) as {id:string;t:number;size:number}[]
    const rows=[...older.slice(0,64).reverse(),anchor,...newer.slice(0,63)]
    let bytes=0,parts=0,unknown=older.length>64 || newer.length>63; const steps:ReturnType<typeof readObservedStep>[]=[]
    for (const row of rows.slice(0,128)) {
      if (!Number.isSafeInteger(row.size) || row.size<0 || row.size>262144 || (bytes+=row.size)>8388608) { unknown=true; break }
      const raw=db.get(sql`SELECT data FROM message WHERE id=${row.id} AND session_id=${input.sessionID}`) as {data:string}|undefined
      if (!raw) throw new Error('observed turn changed')
      const value=JSON.parse(raw.data); if (value?.role!=='assistant') continue
      const identity=value?.metadata?.managedExecution
      if (!identity || typeof identity.rootOperationID!=='string') { unknown=true; continue }
      if (identity.rootOperationID!==input.rootOperationID) continue
      const selected=db.all(sql`SELECT id,length(CAST(data AS BLOB)) AS size FROM part INDEXED BY part_message_id_id_idx WHERE message_id=${row.id} AND session_id=${input.sessionID} ORDER BY id LIMIT 129`) as {id:string;size:number}[]
      if (selected.length>128) unknown=true
      for (const part of selected.slice(0,128)) {
        if (++parts>256 || !Number.isSafeInteger(part.size) || part.size<0 || (bytes+=part.size)>8388608) { unknown=true; break }
        const typed=db.get(sql`SELECT CASE WHEN json_valid(data) AND json_type(data,'$.type')='text' AND length(json_extract(data,'$.type'))<=32 THEN json_extract(data,'$.type') ELSE NULL END AS type FROM part WHERE id=${part.id} AND message_id=${row.id} AND session_id=${input.sessionID}`) as {type:string|null}|undefined
        if (!typed?.type) { unknown=true; continue } if (typed.type!=='step-start') continue
        if (part.size>49152) { unknown=true; continue }
        const saved=db.get(sql`SELECT data FROM part WHERE id=${part.id} AND message_id=${row.id} AND session_id=${input.sessionID}`) as {data:string}|undefined
        const parsed=CaptureMembership.safeParse(saved && JSON.parse(saved.data)?.captureMembership)
        if (!parsed.success) { unknown=true; continue }
        const item=readObservedStep({sessionID:input.sessionID,stableChatID:input.stableChatID,messageID:row.id,stepID:part.id,membershipRevision:parsed.data.revision})
        if (item.rootOperationID!==input.rootOperationID) throw new Error('observed turn changed')
        if (item.members.some(x=>x.state==='pending')) unknown=true
        if (steps.length>=64) { unknown=true; break } steps.push(item)
      }
      if (parts>256 || bytes>8388608) break
    }
    return {version:1 as const,assurance:'unverified_local_observation' as const,coverage:'partial' as const,sessionID:input.sessionID,
      hostChatID:input.stableChatID,rootOperationID:input.rootOperationID,head:{messageID:head.id,timeCreated:head.t},unknown,steps}
  },{behavior:'deferred'})
}
