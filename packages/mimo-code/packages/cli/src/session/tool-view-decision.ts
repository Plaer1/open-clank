/** Inactive T04 decision storage. No model-facing caller or candidate issuer exists here. */
import { createHash } from "node:crypto"
import { sql } from "drizzle-orm"
import { Database } from "../storage"

export type ToolViewAdmission = Readonly<{on:boolean; hostAccountID:string; holderID:string; clockID:string; providerID:string; modelID:string; routeMethodKey:string; connectionID:string; modelRouteID:string; profile:'source-outline-v1' | 'toon-v1' | 'json-whitespace-v1' | 'terminal-ansi-v1' | 'html-markdown-v1' | 'structured-search-v2' | 'repeated-lines-v1' | 'unified-diff-v1' | 'tap13-v1' | 'unittest-success-v1' | 'cargo-artifacts-v1' | 'untracked-status-v1' | 'base64-lines-v1' | 'grep-rows-v1' | 'ndjson-logs-v1' | 'line-cap-v1' | 'json-shape-v1' | 'read-unnumbered-v1' | 'progress-redraw-v1' | 'npm-lockfile-versions-v1' | 'markdown-slim-v1' | 'read-json-whitespace-v1' | 'read-repeated-lines-v1'; lineCapRows?:number; sessionID:string; messageID:string; partID:string; rootOperationID:string; actorID:string; innerTurnID:string; generation:number; settingsRevision:string}>
type Candidate = Omit<ToolViewAdmission,'on'> & {version:1|2; state:"candidate"; dataSHA256:string; contentSHA256:string}
type Raw = Omit<Candidate,"state"> & {state:"raw"}
type View = Omit<Candidate,"state"> & {state:"view"; sourceRevision:number; sourceHash:string; viewSHA256:string}
type Decision = Candidate | Raw | View
export type ToolViewDecision = Decision
const scopeKeys = ['hostAccountID','holderID','clockID','providerID','modelID','routeMethodKey','connectionID','modelRouteID','profile','sessionID','messageID','partID','rootOperationID','actorID','innerTurnID','generation','settingsRevision'] as const
const candidateKeys = [...scopeKeys,'dataSHA256','contentSHA256','state','version'].sort()
const validCap=(v:unknown):v is number=>Number.isSafeInteger(v) && (v as number)>=1 && (v as number)<=100
const validAdmission=(v:ToolViewAdmission)=>v.profile==='line-cap-v1' ? validCap(v.lineCapRows) : v.lineCapRows===undefined
const digest = (s:string) => createHash('sha256').update(s,'utf8').digest('hex')
const hash = (v:unknown):v is string => typeof v === 'string' && /^[a-f0-9]{64}$/.test(v)
function parseMarker(raw:unknown):Decision | undefined {
  if (typeof raw !== 'string' || Buffer.byteLength(raw,'utf8')>4096) return
  try {
    const v=JSON.parse(raw)
    const baseKeys=v?.profile==='line-cap-v1' ? [...candidateKeys,'lineCapRows'].sort() : candidateKeys
    const keys=v?.state==='view' ? [...baseKeys,'sourceRevision','sourceHash','viewSHA256'].sort() : baseKeys
    if (!v || typeof v!=='object' || Array.isArray(v) || Object.keys(v).sort().join(',')!==keys.join(',') ||
        (v.profile==='line-cap-v1' ? v.version!==2 || !validCap(v.lineCapRows) : v.version!==1) || !['candidate','raw','view'].includes(v.state) || !hash(v.dataSHA256) || !hash(v.contentSHA256) ||
        !hash(v.settingsRevision) || !Number.isSafeInteger(v.generation) || v.generation<0 ||
        !['source-outline-v1','toon-v1','json-whitespace-v1','terminal-ansi-v1','html-markdown-v1','structured-search-v2','repeated-lines-v1','unified-diff-v1','tap13-v1','unittest-success-v1','cargo-artifacts-v1','untracked-status-v1','base64-lines-v1','grep-rows-v1','ndjson-logs-v1','line-cap-v1','json-shape-v1','read-unnumbered-v1','progress-redraw-v1','npm-lockfile-versions-v1','markdown-slim-v1','read-json-whitespace-v1','read-repeated-lines-v1'].includes(v.profile) ||
        !/^[a-f0-9]{32}$/.test(v.clockID) ||
        ['hostAccountID','holderID','providerID','modelID','routeMethodKey','connectionID','modelRouteID','sessionID','messageID','partID','rootOperationID','actorID','innerTurnID'].some(k=>typeof v[k]!=='string' || !v[k] || v[k].length>256)) return
    if (v.state==='view' && (!Number.isSafeInteger(v.sourceRevision) || v.sourceRevision<1 || !hash(v.sourceHash) || !hash(v.viewSHA256))) return
    return v as Decision
  } catch { return }
}
function read(raw:unknown, admission:ToolViewAdmission, data:string):Decision | undefined {
  const v=parseMarker(raw)
  return v && validAdmission(admission) && v.lineCapRows===admission.lineCapRows && !scopeKeys.some(k=>v[k]!==admission[k]) && v.dataSHA256===digest(data) ? v : undefined
}
/** An On-only bounded read; the caller still establishes current source authority. */
export function readToolView(admission:ToolViewAdmission):Decision | undefined {
  if (admission?.on !== true || !validAdmission(admission)) return
  try {
    return Database.use(db => {
      const prior=db.get<{timeout:number}>(sql`PRAGMA busy_timeout`)?.timeout
      if (!Number.isSafeInteger(prior) || prior! < 0) return
      db.run(sql`PRAGMA busy_timeout=0`)
      try {
        if (!db.all<{name:string}>(sql`PRAGMA table_info('part')`).some(c=>c.name==='openclank_view_decision')) return
        const row=db.get<{data:string;decision:string}>(sql`SELECT data,openclank_view_decision AS decision FROM part WHERE id=${admission.partID} AND message_id=${admission.messageID} AND session_id=${admission.sessionID} AND openclank_view_decision IS NOT NULL AND length(CAST(openclank_view_decision AS BLOB))<=4096 AND length(CAST(data AS BLOB))<=262144`)
        return row && typeof row.data==='string' ? read(row.decision,admission,row.data) : undefined
      } finally { db.run(sql.raw(`PRAGMA busy_timeout=${prior}`)) }
    })
  } catch { return }
}
/** Distinguish a genuinely absent marker from uncertain local state. */
export function inspectToolView(admission:ToolViewAdmission):{state:'missing'} | {state:'unavailable'} | {state:'out_of_scope';priorState:Decision['state']} | {state:'present';decision:Decision} {
  if (admission?.on !== true) return {state:'missing'}
  if (!validAdmission(admission)) return {state:'unavailable'}
  try {
    return Database.use(db => {
      const prior=db.get<{timeout:number}>(sql`PRAGMA busy_timeout`)?.timeout
      if (!Number.isSafeInteger(prior) || prior! < 0) return {state:'unavailable'} as const
      db.run(sql`PRAGMA busy_timeout=0`)
      try {
        if (!db.all<{name:string}>(sql`PRAGMA table_info('part')`).some(c=>c.name==='openclank_view_decision')) return {state:'missing'} as const
        const marker=db.get<{decision:string | null}>(sql`SELECT CASE WHEN length(CAST(openclank_view_decision AS BLOB))<=4096 THEN openclank_view_decision ELSE '' END AS decision FROM part WHERE id=${admission.partID} AND message_id=${admission.messageID} AND session_id=${admission.sessionID}`)
        if (!marker || marker.decision===null) return {state:'missing'} as const
        if (!marker.decision) return {state:'unavailable'} as const
        const closed=parseMarker(marker.decision)
        if (!closed || closed.sessionID!==admission.sessionID || closed.messageID!==admission.messageID || closed.partID!==admission.partID) return {state:'unavailable'} as const
        if (closed.lineCapRows!==admission.lineCapRows || scopeKeys.some(k=>closed[k]!==admission[k])) return {state:'out_of_scope',priorState:closed.state} as const
        const row=db.get<{data:string}>(sql`SELECT data FROM part WHERE id=${admission.partID} AND message_id=${admission.messageID} AND session_id=${admission.sessionID} AND openclank_view_decision=${marker.decision} AND length(CAST(data AS BLOB))<=262144`)
        const decision=row && typeof row.data==='string' ? read(marker.decision,admission,row.data) : undefined
        return decision ? {state:'present',decision} as const : {state:'unavailable'} as const
      } finally { db.run(sql.raw(`PRAGMA busy_timeout=${prior}`)) }
    })
  } catch { return {state:'unavailable'} }
}
/** Caller must provide the private original admission and current B1 authority; this value grants neither. */
export function freezeToolView(admission:ToolViewAdmission, choice:{state:'raw'} | {state:'view';sourceRevision:number;sourceHash:string;viewSHA256:string}):Decision | undefined {
  if (admission?.on !== true || !validAdmission(admission)) return
  try {
    return Database.use(db => {
      const prior=db.get<{timeout:number}>(sql`PRAGMA busy_timeout`)?.timeout
      if (!Number.isSafeInteger(prior) || prior! < 0) return
      db.run(sql`PRAGMA busy_timeout=0`)
      try {
        if (!db.all<{name:string}>(sql`PRAGMA table_info('part')`).some(c=>c.name==='openclank_view_decision')) return
        const row=db.get<{data:string;decision:string}>(sql`SELECT data,openclank_view_decision AS decision FROM part WHERE id=${admission.partID} AND message_id=${admission.messageID} AND session_id=${admission.sessionID} AND openclank_view_decision IS NOT NULL AND length(CAST(openclank_view_decision AS BLOB))<=4096 AND length(CAST(data AS BLOB))<=262144`)
        if (!row || typeof row.data!=='string') return
        const current=read(row.decision,admission,row.data)
        if (!current) return
        if (current.state!=='candidate') return current
        const next={...current,...choice}
        const encoded=JSON.stringify(next)
        if (!read(encoded,admission,row.data)) return
        db.run(sql`UPDATE part SET openclank_view_decision=${encoded} WHERE id=${admission.partID} AND message_id=${admission.messageID} AND session_id=${admission.sessionID} AND data=${row.data} AND openclank_view_decision=${row.decision}`)
        const after=db.get<{decision:string | null}>(sql`SELECT openclank_view_decision AS decision FROM part WHERE id=${admission.partID} AND message_id=${admission.messageID} AND session_id=${admission.sessionID} AND data=${row.data}`)
        return read(after?.decision,admission,row.data)
      } finally { db.run(sql.raw(`PRAGMA busy_timeout=${prior}`)) }
    })
  } catch { return }
}
