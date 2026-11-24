import { Effect, Option } from 'effect'
import { createHash } from 'node:crypto'
import { WriterService as HistoryWriterService } from '@/history'
import { gcfSessionViewAdmission } from '../session/gcf-policy'
import { openGcfSessionView, inspectGcfSessionView, resetGcfSessionView } from '../session/gcf-session-view'
import z from 'zod'
import * as Tool from './tool'
import * as ManagedProvider from '../acp/managed-provider'
import type { ActorExecutionIdentity } from '../actor/turn'
import { isAdmittedToolTimingTurn } from '../session/tool-timing'
import { encodeRowDelta, applyRowDelta } from '../session/gcf-row-delta'
import { encodeGenericStream, encodeGraphStream, graphStreamAvailable, decodeGraphStream } from '../session/gcf-native'
import { parseJsonInput, type Node } from '../util/toon-subset'

const text = z.string().describe('Explicit input text, at most 51,200 UTF-8 bytes.')
const parameters = z.discriminatedUnion('direction', [
  z.object({ direction: z.literal('encode'), text }).strict(),
  z.object({ direction: z.literal('encode_stream'), text }).strict(),
  z.object({ direction: z.literal('encode_graph_stream'), text }).strict(),
  z.object({ direction: z.literal('decode_graph_stream'), text }).strict(),
  z.object({ direction: z.literal('decode'), text }).strict(),
  z.object({ direction: z.literal('delta'), text, base_text: text }).strict(),
  z.object({ direction: z.literal('apply_delta'), text, base_text: text }).strict(),
  z.object({ direction: z.literal('session_open'), message_id:z.string().min(1).max(256), part_id:z.string().min(1).max(256) }).strict(),
  z.object({ direction: z.literal('session_inspect') }).strict(),
  z.object({ direction: z.literal('session_reset') }).strict(),
])
const policy = 'gcf-generic-v3-safe-integers-v1'

type StreamEvent = {op:'kv';name:string;value:null|boolean|number|string} | {op:'begin';name:string;fields:string[]} | {op:'row';values:(null|boolean|number|string)[]} | {op:'end'}
function streamEvents(text: string): StreamEvent[] {
  const root=parseJsonInput(text)
  if(root.k!=='o') throw new Error('unsupported')
  const result:StreamEvent[]=[]
  for(const [name,value] of root.v){
    if(value.k==='a'){result.push({op:'kv',name,value:value.v});continue}
    if(value.k!=='l'||!value.v.length||value.v[0].k!=='o'||!value.v[0].v.length)throw new Error('unsupported')
    const fields=value.v[0].v.map(([key])=>key)
    result.push({op:'begin',name,fields})
    for(const row of value.v){
      if(row.k!=='o'||row.v.length!==fields.length||row.v.some(([key,cell],i)=>key!==fields[i]||cell.k!=='a'))throw new Error('unsupported')
      result.push({op:'row',values:row.v.map(([,cell])=>(cell as Extract<Node,{k:'a'}>).v)})
    }
    result.push({op:'end'})
  }
  return result
}

const graphPolicy = 'gcf-graph-stream-v1'
// Input is {"symbols":[{id,kind,name,file,line,signature}...],"edges":[{from,to,relation,line}...]}; the native writer validates every field.
function graphEvents(text: string): Record<string, unknown>[] {
  const root = parseJsonInput(text)
  if (root.k !== 'o' || root.v.length !== 2 || root.v[0][0] !== 'symbols' || root.v[1][0] !== 'edges') throw new Error('unsupported')
  const result: Record<string, unknown>[] = []
  for (const [list, op] of [[root.v[0][1], 'symbol'], [root.v[1][1], 'edge']] as const) {
    if (list.k !== 'l') throw new Error('unsupported')
    for (const item of list.v) {
      if (item.k !== 'o' || item.v.some(([key, cell]) => key === 'op' || cell.k !== 'a')) throw new Error('unsupported')
      result.push({ op, ...Object.fromEntries(item.v.map(([key, cell]) => [key, (cell as Extract<Node, { k: 'a' }>).v])) })
    }
  }
  return result
}

export const GcfPreviewTool = Tool.define('gcf_preview', Effect.succeed({
  description: 'Preview explicit text with the verified GCF generic JSON codec. Encoding accepts its bounded safe-integer JSON subset; explicit output may be larger, and older codecs may bypass nonshrinking inputs. Decoding accepts its canonical bounded format. Explicit delta and apply_delta require the base text and preserve canonical typed values, not original JSON bytes; sources remain unchanged. encode_stream buffers the request and result while using the native row stream; encode_graph_stream does the same for an explicit {symbols,edges} graph; decode_graph_stream independently validates and decodes that graph stream; both refuse unknown fields, kinds, numbers or edge endpoints; neither is model or HTTP streaming. Session views require separate current admission and reanchor only to an authorized stored original; they never assert remote state. No files or network are accessed by explicit codec modes; byte counts are not token or cost savings.',
  parameters,
  execute: (args: z.infer<typeof parameters>, ctx: Tool.Context) => Effect.gen(function* () {
    if (ctx.abort.aborted) throw new Error('Cancelled')
    if (args.direction === 'session_open' || args.direction === 'session_inspect' || args.direction === 'session_reset') {
      const unavailable = () => ({title:'GCF session view unavailable',output:'Unavailable: reanchor from a current authorized original.',metadata:{state:'unavailable',truncated:false}})
      const turn=ctx.extra?.actorExecution as ActorExecutionIdentity | undefined
      if (!turn || turn.sessionID!==ctx.sessionID || !turn.rootOperationID || !turn.innerTurnID) return unavailable()
      const original=ManagedProvider.currentOriginalPromptHostContext(turn.sessionID,turn.rootOperationID)
      const actor=ManagedProvider.currentActorHostContext(turn.sessionID,turn.actorID,turn.executionRevision)
      const route=ManagedProvider.currentToolViewRoute(turn.sessionID,turn.actorID,turn.executionRevision)
      const scope=ManagedProvider.currentScope(turn.sessionID,turn.actorID,turn.executionRevision)
      const issuer=ManagedProvider.currentActorToolTimingIssuer(turn.sessionID,turn.actorID,turn.executionRevision,turn.rootOperationID)
      const artifact=original?.gcfArtifact, artifactID=artifact?.identity(), history=ManagedProvider.historySourceGeneration()
      let admitted: ReturnType<typeof gcfSessionViewAdmission>
      try {
        const raw=JSON.parse(original?.gcfAdmission ?? '') as {providerID?:unknown;modelID?:unknown}
        if (typeof raw.providerID!=='string'||typeof raw.modelID!=='string'||!route || route.rootOperationID!==turn.rootOperationID || route.modelID!==raw.modelID || (route.providerID!==raw.providerID && route.connectionID!==raw.providerID)) return unavailable()
        admitted=gcfSessionViewAdmission(original?.gcfAdmission,turn.sessionID,turn.rootOperationID,{providerID:raw.providerID,modelID:raw.modelID})
      } catch {return unavailable()}
      const current=()=>{try{return !ctx.abort.aborted && !!scope && !!issuer?.isAlive() && !!artifact && !!artifactID && !!history && !!admitted &&
        isAdmittedToolTimingTurn(turn,{sessionID:turn.sessionID,actorID:turn.actorID,executionRevision:turn.executionRevision,rootOperationID:turn.rootOperationID!,innerTurnID:turn.innerTurnID!}) &&
        ManagedProvider.currentScope(turn.sessionID,turn.actorID,turn.executionRevision)===scope && ManagedProvider.historySourceGeneration()===history &&
        ManagedProvider.currentActorToolTimingIssuer(turn.sessionID,turn.actorID,turn.executionRevision,turn.rootOperationID!)===issuer &&
        ManagedProvider.currentOriginalPromptHostContext(turn.sessionID,turn.rootOperationID!)?.gcfAdmission===original?.gcfAdmission &&
        ManagedProvider.currentOriginalPromptHostContext(turn.sessionID,turn.rootOperationID!)?.gcfArtifact===artifact &&
        ManagedProvider.currentToolViewRoute(turn.sessionID,turn.actorID,turn.executionRevision)?.rootOperationID===route?.rootOperationID &&
        ManagedProvider.currentToolViewRoute(turn.sessionID,turn.actorID,turn.executionRevision)?.modelID===route?.modelID &&
        ManagedProvider.currentToolViewRoute(turn.sessionID,turn.actorID,turn.executionRevision)?.providerID===route?.providerID &&
        ManagedProvider.currentToolViewRoute(turn.sessionID,turn.actorID,turn.executionRevision)?.connectionID===route?.connectionID &&
        ManagedProvider.currentToolViewRoute(turn.sessionID,turn.actorID,turn.executionRevision)?.modelRouteID===route?.modelRouteID &&
        ManagedProvider.currentActorHostContext(turn.sessionID,turn.actorID,turn.executionRevision)?.gcfAdmission===original?.gcfAdmission &&
        ManagedProvider.currentActorHostContext(turn.sessionID,turn.actorID,turn.executionRevision)?.ownerAccountID===actor?.ownerAccountID &&
        ManagedProvider.currentActorHostContext(turn.sessionID,turn.actorID,turn.executionRevision)?.gcfArtifact===artifact && artifact.identity()===artifactID && artifact.availability()==='available'}catch{return false}}
      if (!actor?.ownerAccountID || !admitted || !original?.gcfAdmission || !artifactID || !current()) return unavailable()
      yield* ctx.ask({permission:'gcf_preview',patterns:['*'],always:['*'],metadata:{}})
      if (!current()) return unavailable()
      const writer=Option.getOrUndefined(yield* Effect.serviceOption(HistoryWriterService)), services=yield* Effect.context()
      if (!writer || !history) return unavailable()
      const owner={scope:{accountID:actor.ownerAccountID,sessionID:turn.sessionID,actorID:turn.actorID,executionRevision:turn.executionRevision,
        rootOperationID:turn.rootOperationID,admission:createHash('sha256').update(original.gcfAdmission).digest('hex'),artifactID}, current,
        sourceAck:(actorID:string,part:import('../session/message-v2').ToolPart)=>Effect.runPromiseWith(services)(writer.sourceAckForPart(actorID,part)),
        checkSources:async(sources:import('../acp/openclank-protocol').HistorySourceRef[])=>{let timer:ReturnType<typeof setTimeout>|undefined
          try{return await Promise.race([ManagedProvider.historySourcesCurrent({sessionID:turn.sessionID,operation:'check_sources',rootOperationID:turn.rootOperationID!,actorID:turn.actorID,executionRevision:turn.executionRevision,innerTurnID:turn.innerTurnID!,sources,sessionViewAdmission:admitted}),new Promise<false>(resolve=>{timer=setTimeout(()=>resolve(false),750)})])===true && current()}
          catch{return false}finally{if(timer)clearTimeout(timer)}}}
      if (args.direction==='session_reset') {const result=resetGcfSessionView(owner);return {title:'GCF session view',output:result.state==='cleared'?'Scoped descriptor cleared.':'Unavailable.',metadata:{...result,truncated:false}}}
      if (args.direction==='session_open') {const result=yield* Effect.promise(()=>openGcfSessionView(owner,args.message_id,args.part_id));return {title:'GCF session view',output:result.state==='opened'?'Scoped original descriptor opened; reanchor requires a fresh source check.':'Unavailable: reanchor from a current authorized original.',metadata:{...result,truncated:false}}}
      const result=yield* Effect.promise(()=>inspectGcfSessionView(owner))
      if (result.state!=='reanchor' || !current()) return unavailable()
      return {title:'GCF authorized original reanchor',output:result.original,metadata:{state:'reanchor',sourceSHA256:result.source.outputSHA256,sourceBytes:result.source.outputBytes,sourceRevision:result.source.revision,truncated:false}}
    }
    const inputBytes = Buffer.byteLength(args.text, 'utf8')
    const deltaMode = args.direction === 'delta' || args.direction === 'apply_delta'
    const baseBytes = deltaMode ? Buffer.byteLength(args.base_text, 'utf8') : 0
    const deltaMeta = deltaMode ? { profile: 'open-clank.gcf-row-delta/v1', baseRequired: true, sourceUnchanged: true, baseBytes } : {}
    if (!args.text.isWellFormed()) return { title: 'GCF preview refused', output: 'Refused: input contains malformed Unicode.', metadata: { direction: args.direction, policy, state: 'refused', reason: 'invalid', inputBytes, outputBytes: 0, truncated: false, ...deltaMeta } }
    if (deltaMode && !args.base_text.isWellFormed()) return { title: 'GCF preview refused', output: 'Refused: base contains malformed Unicode.', metadata: { direction: args.direction, policy, state: 'refused', reason: 'invalid', inputBytes, outputBytes: 0, truncated: false, ...deltaMeta } }
    const unavailable = () => ({ title: 'GCF preview unavailable', output: 'Verified GCF preview is unavailable for this context.', metadata: { direction: args.direction, policy, state: 'unavailable', inputBytes, outputBytes: 0, truncated: false, ...deltaMeta } })
    if (deltaMode && (baseBytes > 51200 || inputBytes + baseBytes > 102400)) return { title: 'GCF preview refused', output: 'Refused: input exceeds the bounded profile.', metadata: { direction: args.direction, policy, state: 'refused', reason: 'too_large', inputBytes, outputBytes: 0, truncated: false, ...deltaMeta } }
    if (inputBytes > 51200) return { title: 'GCF preview refused', output: 'Refused: input exceeds 51,200 UTF-8 bytes.', metadata: { direction: args.direction, policy, state: 'refused', reason: 'too_large', inputBytes, outputBytes: 0, truncated: false, ...deltaMeta } }
    const id = ctx.extra?.actorExecution as ActorExecutionIdentity | undefined
    if (!id || id.sessionID !== ctx.sessionID || !id.rootOperationID || !id.innerTurnID) return unavailable()
    const turn = { sessionID: id.sessionID, actorID: id.actorID, executionRevision: id.executionRevision, rootOperationID: id.rootOperationID, innerTurnID: id.innerTurnID }
    const issuer = ManagedProvider.currentActorToolTimingIssuer(id.sessionID, id.actorID, id.executionRevision, id.rootOperationID)
    const scope = ManagedProvider.currentScope(id.sessionID, id.actorID, id.executionRevision)
    const original = ManagedProvider.currentOriginalPromptHostContext(id.sessionID, id.rootOperationID)
    const actor = ManagedProvider.currentActorHostContext(id.sessionID, id.actorID, id.executionRevision)
    const artifact = original?.gcfArtifact
    const identity = artifact?.identity()
    const current = () => !ctx.abort.aborted && !!scope && !!issuer?.isAlive() && !!artifact && !!identity &&
      isAdmittedToolTimingTurn(id, turn) && ManagedProvider.currentScope(id.sessionID, id.actorID, id.executionRevision) === scope &&
      ManagedProvider.currentActorToolTimingIssuer(id.sessionID, id.actorID, id.executionRevision, id.rootOperationID!) === issuer &&
      ManagedProvider.currentOriginalPromptHostContext(id.sessionID, id.rootOperationID!)?.gcfArtifact === artifact &&
      ManagedProvider.currentActorHostContext(id.sessionID, id.actorID, id.executionRevision)?.gcfArtifact === artifact &&
      artifact.identity() === identity && artifact.availability() === 'available' && (!deltaMode || artifact.decodeAvailability() === 'available')
    if (!artifact || actor?.gcfArtifact !== artifact || !current()) return unavailable()
    yield* ctx.ask({ permission: 'gcf_preview', patterns: ['*'], always: ['*'], metadata: {} })
    if (ctx.abort.aborted) throw new Error('Cancelled')
    if (!current()) return unavailable()
    if (args.direction === 'delta' || args.direction === 'apply_delta') {
      const result = args.direction === 'delta' ? encodeRowDelta(args.base_text, args.text, artifact) : applyRowDelta(args.base_text, args.text, artifact)
      if (ctx.abort.aborted) throw new Error('Cancelled')
      if (!current()) return unavailable()
      if (result.state !== 'success') return { title: 'GCF row delta ' + result.state, output: 'GCF row delta ' + result.state + ': ' + result.reason + '.', metadata: { direction: args.direction, policy, state: result.state, reason: result.reason, inputBytes, outputBytes: 0, truncated: false, ...deltaMeta } }
      return { title: 'GCF row delta preview', output: result.output, metadata: { direction: args.direction, policy, state: 'preview', inputBytes, outputBytes: Buffer.byteLength(result.output, 'utf8'), truncated: false, ...deltaMeta, baseSHA256: result.baseSHA256, resultSHA256: result.resultSHA256, count: result.count, changes: result.changes } }
    }
    if (args.direction === 'encode_stream') {
      let events:StreamEvent[]
      try { events=streamEvents(args.text) } catch { return { title:'GCF preview refused', output:'Refused: input is outside the streaming profile.', metadata:{direction:args.direction,policy:'gcf-generic-v3-stream-safe-integers-v1',state:'refused',reason:'invalid',inputBytes,outputBytes:0,truncated:false} } }
      const streamed = yield* Effect.promise(async () => {
        const chunks:Buffer[]=[];let size=0,complete=false
        try {
          async function* source(){for(const event of events) yield event}
          for await (const item of encodeGenericStream(source(),artifact,{signal:ctx.abort})) {
            if(ctx.abort.aborted) throw new Error('Cancelled')
            if(!current()||complete||!Buffer.isBuffer(item.chunk)) return undefined
            size+=item.chunk.length;if(size>51200) return undefined
            chunks.push(item.chunk)
            if(item.state==='complete') complete=true
            else if(item.state!=='provisional') return undefined
          }
          if(!complete||!current()) return undefined
          return Buffer.concat(chunks,size)
        } catch { if(ctx.abort.aborted) throw new Error('Cancelled'); return undefined }
      })
      if(ctx.abort.aborted) throw new Error('Cancelled')
      if(!streamed||!current()) return unavailable()
      try {
        const output=new TextDecoder('utf-8',{fatal:true}).decode(streamed)
        if(!current())return unavailable()
        return {title:'GCF stream preview',output,metadata:{direction:args.direction,policy:'gcf-generic-v3-stream-safe-integers-v1',state:'preview',inputBytes,outputBytes:streamed.length,truncated:false,buffered:true}}
      } catch { return unavailable() }
    }
    if (args.direction === 'decode_graph_stream') {
      const decoded=decodeGraphStream(Buffer.from(args.text,'utf8'),artifact)
      if (ctx.abort.aborted) throw new Error('Cancelled')
      if (!current() || decoded.state==='unavailable') return unavailable()
      if (decoded.state==='refused') return {title:'GCF preview refused',output:'Refused: input is outside the graph stream profile.',metadata:{direction:args.direction,policy:graphPolicy,state:'refused',reason:'invalid',inputBytes,outputBytes:0,truncated:false}}
      if (decoded.state!=='decoded') return unavailable()
      try {
        const output=new TextDecoder('utf-8',{fatal:true}).decode(decoded.output)
        if (!current()) return unavailable()
        return {title:'GCF graph stream preview',output,metadata:{direction:args.direction,policy:graphPolicy,state:'preview',inputBytes,outputBytes:decoded.output.length,truncated:false,buffered:true}}
      } catch { return unavailable() }
    }
    if (args.direction === 'encode_graph_stream') {
      if (!graphStreamAvailable(artifact)) return unavailable()
      const refused = () => ({ title:'GCF preview refused', output:'Refused: input is outside the graph stream profile.', metadata:{direction:args.direction,policy:graphPolicy,state:'refused',reason:'invalid',inputBytes,outputBytes:0,truncated:false} })
      let events: Record<string, unknown>[]
      try { events = graphEvents(args.text) } catch { return refused() }
      let failed = false
      const streamed = yield* Effect.promise(async () => {
        const chunks:Buffer[]=[];let size=0,complete=false
        try {
          async function* source(){for(const event of events) yield event}
          for await (const item of encodeGraphStream(source(),artifact,{signal:ctx.abort})) {
            if(ctx.abort.aborted) throw new Error('Cancelled')
            if(!current()||complete||!Buffer.isBuffer(item.chunk)) return undefined
            size+=item.chunk.length;if(size>51200) return undefined
            chunks.push(item.chunk)
            if(item.state==='complete') complete=true
          }
          return complete&&current() ? Buffer.concat(chunks,size) : undefined
        } catch { if(ctx.abort.aborted) throw new Error('Cancelled'); failed=true; return undefined }
      })
      if(ctx.abort.aborted) throw new Error('Cancelled')
      if(!current()) return unavailable()
      if(!streamed) return failed ? refused() : unavailable()
      try {
        const output=new TextDecoder('utf-8',{fatal:true}).decode(streamed)
        if(!current())return unavailable()
        return {title:'GCF graph stream preview',output,metadata:{direction:args.direction,policy:graphPolicy,state:'preview',inputBytes,outputBytes:streamed.length,truncated:false,buffered:true}}
      } catch { return unavailable() }
    }
    const input = Buffer.from(args.text, 'utf8')
    const result = args.direction === 'encode'
      ? (artifact.previewEncodeAvailability?.() === 'available' && artifact.previewEncode ? artifact.previewEncode(input, policy) : artifact.encode(input, policy))
      : artifact.decode(input, policy)
    if (ctx.abort.aborted) throw new Error('Cancelled')
    if (!current()) return unavailable()
    if (result.state === 'bypass' || result.state === 'refused') return {
      title: 'GCF preview ' + (result.state === 'bypass' ? 'bypassed' : 'refused'),
      output: 'GCF ' + result.state + ': ' + result.reason + '.',
      metadata: { direction: args.direction, policy, state: result.state, reason: result.reason, inputBytes, outputBytes: 0, truncated: false },
    }
    try {
      const output = new TextDecoder('utf-8', { fatal: true }).decode(result.output)
      if (!current()) return unavailable()
      return { title: 'GCF preview', output, metadata: { direction: args.direction, policy, state: 'preview', inputBytes, outputBytes: result.output.length, truncated: false } }
    } catch { return unavailable() }
  }),
}))
