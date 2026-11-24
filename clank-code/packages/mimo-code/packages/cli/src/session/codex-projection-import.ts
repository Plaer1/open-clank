/** Host-private selected Codex v6 import. ExternalImportTable remains the receipt authority. */
import { createHash } from "node:crypto"
import path from "node:path"
import { and, eq, gte, lt } from "drizzle-orm"
import { Database } from "../storage"
import { ProjectTable } from "../project/project.sql"
import { ProjectID } from "../project/schema"
import { SessionTable, MessageTable, PartTable } from "./session.sql"
import type { MessageV2 } from "./message-v2"
import { ExternalImportTable } from "./external-import.sql"
import { SessionID, MessageID, PartID } from "./schema"
import { ProviderID, ModelID } from "../provider/schema"
import { Slug } from "@mimo-ai/shared/util/slug"

const SOURCE = "codex-projection" as const
const hashBytes = (value: string) => createHash("sha256").update(value, "utf8").digest("hex")
const stable = (value: any): any => Array.isArray(value) ? value.map(stable) : value && typeof value === "object"
  ? Object.fromEntries(Object.keys(value).sort().map(k => [k, stable(value[k])])) : value
const hash = (value: unknown) => hashBytes(JSON.stringify(stable(value)))
const str = (x: unknown, limit = 512): x is string => typeof x === "string" && !!x && Buffer.byteLength(x, "utf8") <= limit && !/[\u0000-\u001f\u007f]/.test(x)
const hex = (x: unknown): x is string => typeof x === "string" && /^[a-f0-9]{64}$/.test(x)
type Item = {turnID:string; itemID:string; ordinal:number; kind:string; role:"user"|"assistant"; text:string}
type Envelope = {version:1; phase:"reserve"|"issue"|"read"|"ack_host"|"claim_map"|"ack_map"; intent:any; claim?:any}

function checked(raw: any) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw) || Object.keys(raw).sort().join(",") !== "items,key,prefix,provenance,workspacePath") throw new Error("invalid selected import")
  const p = raw.provenance, items = raw.items
  const keys = ["account","coverage","destinationName","hostChatID","hostMessageIDs","nativeMessageIDs","nativePartIDs","nativeSessionID","ordering","owner","resourceID","selection","selectionSHA256","snapshotSHA256","sourceKey","sourceTime","threadID","unsupportedCount","version","workspaceID","workspacePathSHA256"]
  if (!p || typeof p !== "object" || Array.isArray(p) || Object.keys(p).sort().join(",") !== keys.sort().join(",") || p.version !== 1
      || ![p.account,p.owner,p.resourceID,p.threadID,p.workspaceID,p.destinationName].every((x:any)=>str(x))
      || !hex(p.snapshotSHA256) || p.coverage !== "selected_partial" || p.ordering !== "import_order" || p.sourceTime !== "unknown" || p.unsupportedCount !== null
      || !path.isAbsolute(raw.workspacePath) || p.workspacePathSHA256 !== hashBytes(raw.workspacePath) || !Array.isArray(items) || items.length < 1 || items.length > 100
      || !Array.isArray(p.selection) || p.selection.length !== items.length) throw new Error("invalid selected import")
  let previous = -1, total = 0
  const seen = new Set<string>()
  const selection = items.map((item:Item) => {
    if (!item || Object.keys(item).sort().join(",") !== "itemID,kind,ordinal,role,text,turnID" || !str(item.turnID,128) || !str(item.itemID,128)
      || !Number.isSafeInteger(item.ordinal) || item.ordinal <= previous || !["user","assistant"].includes(item.role) || typeof item.text !== "string") throw new Error("invalid selected item")
    const identity = JSON.stringify([item.turnID,item.itemID]); if (seen.has(identity)) throw new Error("duplicate item"); seen.add(identity); previous=item.ordinal
    total += Buffer.byteLength(item.text,"utf8")
    return {turnID:item.turnID,itemID:item.itemID,ordinal:item.ordinal,role:item.role,sha256:hashBytes(item.text)}
  })
  if (items[0].role !== "user" || total > 1024*1024 || JSON.stringify(stable(selection)) !== JSON.stringify(stable(p.selection))) throw new Error("invalid selected text")
  const prefix="v1:"+hash([p.account,p.resourceID,p.threadID])+":"
  const selectionSHA256=hash(selection), key=prefix+selectionSHA256, root=hash(["selected-history-v1",key])
  const hostIDs=items.map((_:unknown,i:number)=>"import-msg-"+hash([root,i]).slice(0,40))
  const nativeIDs=items.map((_:unknown,i:number)=>"msg_g"+(i+1).toString(16).padStart(16,"0")+hash([root,i]).slice(0,9))
  const partIDs=items.map((_:unknown,i:number)=>"prt_g"+(i+1).toString(16).padStart(16,"0")+hash(["part",root,i]).slice(0,9))
  if (raw.prefix!==prefix || raw.key!==key || p.sourceKey!==key || p.selectionSHA256!==selectionSHA256 || p.hostChatID!=="import-"+root.slice(0,40)
      || p.nativeSessionID!=="ses_"+root.slice(0,26) || JSON.stringify(p.hostMessageIDs)!==JSON.stringify(hostIDs)
      || JSON.stringify(p.nativeMessageIDs)!==JSON.stringify(nativeIDs) || JSON.stringify(p.nativePartIDs)!==JSON.stringify(partIDs)) throw new Error("selected import identity changed")
  return {p,items,prefix,key}
}

export function selectedHistory(request: Envelope) {
  if (!request || !["intent,phase,version", "claim,intent,phase,version"].includes(Object.keys(request).sort().join(",")) || request.version !== 1 || !["reserve","issue","read","ack_host","claim_map","ack_map"].includes(request.phase)) throw new Error("invalid selected import phase")
  const {p,items,prefix,key}=checked(request.intent)
  const claim = request.claim
  if (["reserve","issue","claim_map"].includes(request.phase)) {
    const shape = request.phase === "claim_map" ? "binding,nonce,runtimeSHA256" : "nonce,runtimeSHA256"
    if (!claim || Object.keys(claim).sort().join(",") !== shape || !hex(claim.nonce) || !hex(claim.runtimeSHA256)) throw new Error("invalid selected import claim")
    if (request.phase === "claim_map") {
      const b = claim.binding
      if (!b || Object.keys(b).sort().join(",") !== "authorityWorkspaceID,copalWorkspace,engineAliases,engineSessionID,mapRevision,mappingRevision,memoryEnabled,memoryWorkspaceID,owner,physicalCwd,stableChatID,transition,workspaceRevision"
          || b.owner !== p.owner || b.stableChatID !== p.hostChatID || b.engineSessionID !== p.nativeSessionID
          || b.authorityWorkspaceID !== p.workspaceID || b.physicalCwd !== request.intent.workspacePath
          || b.memoryEnabled !== false || b.transition !== null || !Array.isArray(b.engineAliases) || b.engineAliases.length !== 0
          || !str(b.copalWorkspace) || !str(b.memoryWorkspaceID) || b.workspaceRevision !== 0
          || !Number.isSafeInteger(b.mapRevision) || b.mapRevision < 1 || !Number.isSafeInteger(b.mappingRevision) || b.mappingRevision !== 1) throw new Error("invalid map binding")
    }
  } else if (request.phase === "ack_map") {
    if (!claim || Object.keys(claim).sort().join(",") !== "mapRevision,mappingRevision"
        || !Number.isSafeInteger(claim.mapRevision) || claim.mapRevision < 1 || claim.mappingRevision !== 1) throw new Error("invalid map receipt")
  } else if (claim !== undefined) throw new Error("unexpected selected import claim")
  return Database.transaction((tx) => {
    const matches=tx.select().from(ExternalImportTable).where(and(eq(ExternalImportTable.source,SOURCE),gte(ExternalImportTable.source_key,prefix),lt(ExternalImportTable.source_key,prefix+"~"))).limit(2).all()
    if (matches.length > 1 || (matches.length && matches[0].source_key !== key)) return {state:"conflict" as const}
    let record: any
    if (matches.length) {
      try { record=JSON.parse(matches[0].source_path) } catch { return {state:"unknown" as const} }
      if (!record || record.version !== 1 || JSON.stringify(stable(record.provenance)) !== JSON.stringify(stable(p))) return {state:"conflict" as const}
      if (record.phase !== "reserved") {
        const native=tx.select({id:SessionTable.id}).from(SessionTable).where(eq(SessionTable.id,SessionID.make(p.nativeSessionID))).get()
        if (!native) return {state:"unknown" as const}
        for (let i=0;i<items.length;i++) {
          const message=tx.select().from(MessageTable).where(eq(MessageTable.id,MessageID.make(p.nativeMessageIDs[i]))).get()
          const part=tx.select().from(PartTable).where(eq(PartTable.id,PartID.make(p.nativePartIDs[i]))).get()
          if (!message || message.session_id !== p.nativeSessionID || (message.data as any).role !== items[i].role || !part || part.session_id !== p.nativeSessionID || part.message_id !== p.nativeMessageIDs[i] || (part.data as any).type !== "text" || hashBytes((part.data as any).text ?? "") !== p.selection[i].sha256) return {state:"unknown" as const}
        }
      }
    } else if (request.phase !== "reserve") return {state:"unknown" as const}
    if (!matches.length) {
      const reservation={version:1,phase:"reserved",provenance:p,issueClaimSHA256:hash([claim.nonce,claim.runtimeSHA256])}
      tx.insert(ExternalImportTable).values({source:SOURCE,source_key:key,session_id:SessionID.make(p.nativeSessionID),source_path:JSON.stringify(reservation),source_mtime:0,time_imported:Date.now(),message_ids:[]}).run()
      return {state:"reserved" as const,fresh:true,provenance:p}
    }
    if (record.phase === "reserved" && request.phase === "issue") {
      if (record.issueClaimSHA256 !== hash([claim.nonce,claim.runtimeSHA256])) return {state:"unknown" as const}
      const sessionID=SessionID.make(p.nativeSessionID), now=Date.now()
      if (tx.select({id:SessionTable.id}).from(SessionTable).where(eq(SessionTable.id,sessionID)).get()) return {state:"conflict" as const}
      for (const id of p.nativeMessageIDs) if (tx.select({id:MessageTable.id}).from(MessageTable).where(eq(MessageTable.id,MessageID.make(id))).get()) return {state:"conflict" as const}
      for (const id of p.nativePartIDs) if (tx.select({id:PartTable.id}).from(PartTable).where(eq(PartTable.id,PartID.make(id))).get()) return {state:"conflict" as const}
      tx.insert(ProjectTable).values({id:ProjectID.global,worktree:request.intent.workspacePath,vcs:null,sandboxes:[],time_created:now,time_updated:now}).onConflictDoNothing().run()
      tx.insert(SessionTable).values({id:sessionID,project_id:ProjectID.global,slug:Slug.create(),directory:request.intent.workspacePath,title:p.destinationName,title_source:"user",title_revision:0,version:"selected-codex-v6",time_created:now,time_updated:now}).run()
      let parent: string | undefined
      for (let i=0;i<items.length;i++) {
        const item=items[i], messageID=MessageID.make(p.nativeMessageIDs[i]), partID=PartID.make(p.nativePartIDs[i]), timestamp=now+i
        if (item.role === "user") parent=messageID
        const data:any = item.role === "user"
          ? {role:"user",agent:"main",time:{created:timestamp},model:{providerID:ProviderID.make("unknown"),modelID:ModelID.make("unknown")}}
          : {role:"assistant",agent:"main",mode:"build",time:{created:timestamp,completed:timestamp},parentID:parent,providerID:ProviderID.make("unknown"),modelID:ModelID.make("unknown"),path:{cwd:request.intent.workspacePath,root:request.intent.workspacePath},cost:0,tokens:{input:0,output:0,reasoning:0,cache:{read:0,write:0}}}
        tx.insert(MessageTable).values({id:messageID,session_id:sessionID,agent_id:"main",time_created:timestamp,time_updated:timestamp,data}).run()
        const partData: Omit<MessageV2.TextPart, "id" | "sessionID" | "messageID"> = {type:"text", text:item.text}
        tx.insert(PartTable).values({id:partID,message_id:messageID,session_id:sessionID,time_created:timestamp,time_updated:timestamp,data:partData}).run()
      }
      record={version:1,phase:"host_issuing",provenance:p,issueClaimSHA256:record.issueClaimSHA256}
      tx.update(ExternalImportTable).set({source_path:JSON.stringify(record),time_imported:now,message_ids:p.nativeMessageIDs.map((id:string)=>MessageID.make(id))}).where(and(eq(ExternalImportTable.source,SOURCE),eq(ExternalImportTable.source_key,key),eq(ExternalImportTable.source_path,matches[0].source_path))).run()
      return {state:"host_issuing" as const,fresh:true,provenance:p}
    }
    const old=record.phase
    let next=old
    if (request.phase === "ack_host" && old === "host_issuing") next="host_committed"
    if (request.phase === "claim_map" && old === "host_committed") { next="map_issuing"; record={...record,mapClaimSHA256:hash([claim.nonce,claim.runtimeSHA256,claim.binding]),mapBinding:claim.binding} }
    if (request.phase === "ack_map" && old === "map_issuing") {
      if (!record.mapBinding || claim.mapRevision < record.mapBinding.mapRevision || claim.mappingRevision !== record.mapBinding.mappingRevision) return {state:"unknown" as const}
      next="complete"; record={...record,mapReceipt:claim}
    }
    if (next !== old) tx.update(ExternalImportTable).set({source_path:JSON.stringify({...record,phase:next})}).where(and(eq(ExternalImportTable.source,SOURCE),eq(ExternalImportTable.source_key,key),eq(ExternalImportTable.source_path,matches[0].source_path))).run()
    return {state:next,fresh:request.phase === "claim_map" && old === "host_committed",provenance:p,...(record.mapBinding ? {mapBinding:record.mapBinding} : {}),...(record.mapReceipt ? {mapReceipt:record.mapReceipt} : {})}
  }, {behavior:"immediate"})
}

/** Content-free recovery from the existing row, never a create or transition. */
export function selectedHistoryRead(request: any) {
  if (!request || Object.keys(request).sort().join(",") !== "account,intentSHA256,phase,selectionSHA256,snapshotSHA256,sourceKey,version"
      || request.version !== 1 || request.phase !== "reconcile" || !str(request.account)
      || !hex(request.selectionSHA256) || !hex(request.snapshotSHA256) || !hex(request.intentSHA256)
      || typeof request.sourceKey !== "string" || !/^v1:[a-f0-9]{64}:[a-f0-9]{64}$/.test(request.sourceKey)) throw new Error("invalid selected receipt")
  return Database.transaction((db) => {
    const row = db.select().from(ExternalImportTable).where(and(eq(ExternalImportTable.source,SOURCE),eq(ExternalImportTable.source_key,request.sourceKey))).get()
    if (!row) return {state:"unknown" as const}
    let record: any
    if (typeof row.source_path !== "string" || Buffer.byteLength(row.source_path, "utf8") > 131072) return {state:"unknown" as const}
    try { record = JSON.parse(row.source_path) } catch { return {state:"unknown" as const} }
    const p = record?.provenance
    const provenanceKeys = ["account","coverage","destinationName","hostChatID","hostMessageIDs","nativeMessageIDs","nativePartIDs","nativeSessionID","ordering","owner","resourceID","selection","selectionSHA256","snapshotSHA256","sourceKey","sourceTime","threadID","unsupportedCount","version","workspaceID","workspacePathSHA256"]
    const recordKeys = Object.keys(record || {}).sort().join(",")
    if (!["issueClaimSHA256,phase,provenance,version", "issueClaimSHA256,mapBinding,mapClaimSHA256,phase,provenance,version", "issueClaimSHA256,mapBinding,mapClaimSHA256,mapReceipt,phase,provenance,version"].includes(recordKeys)
        || !p || Object.keys(p).sort().join(",") !== provenanceKeys.sort().join(",") || record.version !== 1 || p.version !== 1 || p.account !== request.account || p.sourceKey !== request.sourceKey
        || p.snapshotSHA256 !== request.snapshotSHA256 || p.selectionSHA256 !== request.selectionSHA256 || hash(p) !== request.intentSHA256
        || hash(p.selection) !== p.selectionSHA256 || !Array.isArray(p.selection) || p.selection.length < 1 || p.selection.length > 100
        || !Array.isArray(p.nativeMessageIDs) || !Array.isArray(p.nativePartIDs)
        || p.nativeMessageIDs.length !== p.selection.length || p.nativePartIDs.length !== p.selection.length
        || !["reserved","host_issuing","host_committed","map_issuing","complete"].includes(record.phase)) return {state:"unknown" as const}
    if (record.phase !== "reserved") {
      if (!db.select({id:SessionTable.id}).from(SessionTable).where(eq(SessionTable.id,SessionID.make(p.nativeSessionID))).get()) return {state:"unknown" as const}
      for (let i=0;i<p.selection.length;i++) {
        const message=db.select().from(MessageTable).where(eq(MessageTable.id,MessageID.make(p.nativeMessageIDs[i]))).get()
        const part=db.select().from(PartTable).where(eq(PartTable.id,PartID.make(p.nativePartIDs[i]))).get()
        if (!message || message.session_id !== p.nativeSessionID || (message.data as any).role !== p.selection[i].role || !part
            || part.session_id !== p.nativeSessionID || part.message_id !== p.nativeMessageIDs[i]
            || (part.data as any).type !== "text" || hashBytes((part.data as any).text ?? "") !== p.selection[i].sha256) return {state:"unknown" as const}
      }
    }
    if (record.mapBinding && (typeof record.mapBinding !== "object" || Array.isArray(record.mapBinding)
        || Object.keys(record.mapBinding).sort().join(",") !== "authorityWorkspaceID,copalWorkspace,engineAliases,engineSessionID,mapRevision,mappingRevision,memoryEnabled,memoryWorkspaceID,owner,physicalCwd,stableChatID,transition,workspaceRevision"
        || record.mapBinding.owner !== p.owner || record.mapBinding.stableChatID !== p.hostChatID
        || record.mapBinding.engineSessionID !== p.nativeSessionID || record.mapBinding.authorityWorkspaceID !== p.workspaceID
        || hashBytes(record.mapBinding.physicalCwd) !== p.workspacePathSHA256)) return {state:"unknown" as const}
    if (record.phase === "complete" && (!record.mapReceipt || !Number.isSafeInteger(record.mapReceipt.mapRevision) || record.mapReceipt.mapRevision < record.mapBinding.mapRevision || record.mapReceipt.mappingRevision !== record.mapBinding.mappingRevision)) return {state:"unknown" as const}
    return {state:record.phase,provenance:p,...(record.mapBinding ? {mapBinding:record.mapBinding} : {}),...(record.mapReceipt ? {mapReceipt:record.mapReceipt} : {})}
  }, {behavior:"deferred"})
}
