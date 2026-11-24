import { expect, test } from 'bun:test'
import { createHash } from 'node:crypto'
import { Effect } from 'effect'
import { sql } from 'drizzle-orm'
import { Database } from '../../src/storage'
import { ProjectTable } from '../../src/project/project.sql'
import { SessionTable, MessageTable } from '../../src/session/session.sql'
import { Session } from '../../src/session'
import { MessageV2 } from '../../src/session/message-v2'
import { AppRuntime } from '../../src/effect/app-runtime'
import { Instance } from '../../src/project/instance'
import { tmpdir } from '../fixture/fixture'
import { createToolTimingIssuer, markAdmittedToolTimingTurn, captureToolTiming } from '../../src/session/tool-timing'
import { encodeToolViewAdmission, captureToolView } from '../../src/session/tool-view-source'
import { prepareToolViews, restoreContextPreface } from '../../src/session/tool-view-assembly'
import { encodeForkContext, decodeForkContext } from '../../src/actor/fork-context'
import { compactJsonTokens } from '../../src/session/tool-view-json'
import { recordMcpJsonCandidate } from '../../src/session/mcp-json-candidate'
import { gcfCandidate, authenticatedOutline } from '../../src/session/gcf-source-proof'
import { boundedOutlineSidecar } from '../../src/session/source-outline-custody'

test('final producer, synthetic ACK/source check and actual model conversion preserve raw canonical output', async () => {
  await using tmp=await tmpdir()
  await Instance.provide({directory:tmp.path,fn:async()=>{
    const now=Date.now(), sessionID='ses_view',messageID='msg_view',partID='prt_view'
    Database.use(db=>{
      db.insert(ProjectTable).values({id:'proj_view' as any,worktree:tmp.path,sandboxes:[] as any,time_created:now,time_updated:now} as any).run()
      db.insert(SessionTable).values({id:sessionID as any,project_id:'proj_view' as any,slug:'v',directory:tmp.path,title:'v',version:'1',time_created:now,time_updated:now}).run()
      db.insert(MessageTable).values({id:messageID as any,session_id:sessionID as any,agent_id:'main',data:{role:'assistant'} as any,time_created:now,time_updated:now}).run()
      if (!db.all<{name:string}>(sql`PRAGMA table_info(part)`).some(x=>x.name==='openclank_view_decision'))
        db.run(sql`ALTER TABLE part ADD COLUMN openclank_view_decision TEXT`)
    })
    const identity={sessionID,rootOperationID:'root',actorID:'main',executionRevision:1,innerTurnID:'turn'} as any
    const route={rootOperationID:'root',connectionID:'conn',providerID:'provider',modelID:'model',modelRouteID:'route'}
    const original={version:2,requested:true,...route,sessionID,hostAccountID:'account',holderID:'holder',generation:2,settingsRevision:'a'.repeat(64),routeMethodKey:'route-key',profiles:{jsonWhitespace:false,terminalAnsi:false,htmlMarkdown:false,structuredSearch:false,repeatedLines:false,unifiedDiff:false,schemaAnnotations:false,toon:true,sourceOutlines:true}}
    const encoded=encodeToolViewAdmission(original,sessionID,route,{providerID:'provider',modelID:'model'})!
    const sha=(value:string)=>createHash('sha256').update(value).digest('hex')
    const body='export function example() { return 42 }\n'+'// source body\n'.repeat(500), size=Buffer.byteLength(body),hash='a'.repeat(64),id='fileid',path='src/a.ts'
    const receipt={workspace_id:'w',registration_id:'r',source_snapshot_id:'s',checkout_id:'c',head_generation:'g',admission_epoch:1,policy_generation:1,config_fingerprint:'b'.repeat(64)}
    const span={start_byte:0,end_byte:size,start_line:1,start_column:0,end_line:1,end_column:1}
    const read={schema:'open-clank.code-query/v1',operation:'read',receipt,error:null,partial:false,degraded:false,next_cursor:null,source_window:{rel_path:path,start_byte:0,end_byte:size,continuation:null,continuity:'full_sha256'},hits:[{id:`file:${id}`,kind:'raw_window',evidence_state:'current',current_source_verified:true,source:{file_id:id,rel_path:path,source_hash:hash,span},raw_source:{rel_path:path,start_byte:0,end_byte:size,file_size:size,hash_algorithm:'sha256',hash_scope:'file',hash:sha(body),state:'observed_file',continuation:null},properties:{rel_path:path,window_complete:true,indexed_source_hash:hash},excerpt:body}]}
    const outline={schema:read.schema,operation:'outline',receipt,error:null,partial:false,degraded:false,next_cursor:null,omissions:[],counts:[],reasons:[],hits:[{id:'decl:example',kind:'declaration',signature:'function example()',source:{file_id:id,rel_path:path,source_hash:hash,span:{...span,end_byte:20}},properties:{},matches:[],evidence_state:'indexed',current_source_verified:false}]}
    const output=JSON.stringify({output:JSON.stringify(read),exit_code:0})
    const sidecar={version:1,toolCallID:'call',outputSHA256:sha(output),outline,extraCost:{work:1,bytes:1,tokens:1,rpcs:1,files_bytes_read:size,complete:true}}
    const sidecarText=boundedOutlineSidecar(sidecar,'call',output)!
    expect(sidecarText).toBeTruthy()
    let recorded:{value:string; current?:()=>boolean; sidecar?:string}|undefined, originalOn=true
    const producer={callID:'call',output,normalizedOutput:output,gcfOn:false,toonCurrent:()=>originalOn,
      aborted:false,truncated:false,attachmentCount:0,isError:false,content:[{type:'text'}],hasStructuredContent:false,
      record:(_id:string,value:string,current?:()=>boolean,outline?:{sidecar:string})=>{recorded={value,current,sidecar:outline?.sidecar}}}
    recordMcpJsonCandidate({...producer,toonCurrent:undefined,outline:{sidecar:sidecarText,current:()=>originalOn,text:output,toolCallID:'call'}})
    expect(recorded?.sidecar).toBe(sidecarText)
    const running={id:partID,sessionID,messageID,type:'tool',tool:'mcp_tool_search',callID:'call',state:{status:'running',input:{},time:{start:now}}} as any
    const finished={...running,state:{status:'completed',input:{},output,title:'',metadata:{truncated:false,mcp:{isError:false}},time:{start:now,end:now+1}}} as any
    await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(running)))
    const issuer=createToolTimingIssuer();markAdmittedToolTimingTurn(identity,identity)
    const timing=captureToolTiming(issuer,identity,running)!, capture=captureToolView(encoded,timing,finished,route,'source-outline-v1')!
    const issued=await AppRuntime.runPromise(Session.Service.use(s=>s.updateGcfCompleted(finished,timing,capture,recorded?.sidecar)))
    const stored=JSON.parse(Database.use(db=>db.get<{v:string}>(sql`SELECT openclank_view_decision AS v FROM part WHERE id=${partID}`)!.v))
    const reloaded=await AppRuntime.runPromise(Session.Service.use(s=>s.getPart({sessionID:sessionID as any,messageID:messageID as any,partID:partID as any}))) as any
    expect(authenticatedOutline(reloaded)).toBe(sidecarText)
    const tampered=structuredClone(reloaded);tampered.state.metadata.openclankGcfSource.outline='null'
    expect(authenticatedOutline(tampered)).toBeUndefined()
    const messages=[{info:{id:messageID,role:'assistant',providerID:'provider',modelID:'model'},parts:[reloaded]}] as any
    const control:{disabled:boolean;reason?:string}={disabled:false}, digest=createHash('sha256').update(output).digest('hex')
    let checks=0, live=true
    const args={encoded,identity,issuer,route,control,historyAllowed:true,messages,current:()=>live,
      ack:async()=>({version:1 as const,accountID:'account',actorID:'main',chatID:'chat',sessionID,messageID,partID,revision:3,contentHash:stored.contentSHA256,outputSHA256:digest,outputBytes:Buffer.byteLength(output)}),
      check:async(refs:any[])=>{checks++;return refs.length===1 && refs[0].contentHash===stored.contentSHA256}}
    const prepared=await prepareToolViews(args)
    expect(checks).toBe(1);expect(prepared.views.size).toBe(1)
    const model={id:'model',providerID:'provider',api:{npm:'@ai-sdk/anthropic'},capabilities:{input:{image:false}}} as any
    const rendered=await Effect.runPromise(MessageV2.toModelMessagesEffect(messages,model,{toolViews:prepared.views}))
    const wire=JSON.stringify(rendered)
    expect(wire).toContain('file body omitted');expect(wire).toContain('function example()')
    expect(finished.state.output).toBe(output)
    const frozen=JSON.stringify(rendered)
    const inherited=[{role:'user',content:'question'} as any,...rendered]
    const fork=await encodeForkContext({system:['system'],tools:{},parentPermission:[],inheritedMessages:inherited,
      watermarkMsgID:messageID as any,model:{providerID:'provider' as any,modelID:'model' as any}}, {sessionID:sessionID as any,actorID:'main'})
    originalOn=false
    live=false
    const refused=await prepareToolViews(args)
    expect(refused.views.get(partID)).toBe(output);expect(control.disabled).toBe(true);expect(control.reason).toBe('scope_unavailable')
    expect(JSON.stringify(rendered)).toBe(frozen)
    const restored=await decodeForkContext({...fork,sessionID:sessionID as any,actorID:'child',timeCreated:now})
    expect(restored.inheritedMessages).toEqual(inherited)
    const repeated=await encodeForkContext(restored,{sessionID:sessionID as any,actorID:'main'})
    expect(repeated.snapshotJSON).toBe(fork.snapshotJSON)
    issuer.revoke()
  }})
})
