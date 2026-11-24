import {expect,test} from 'bun:test'
import {createHash} from 'node:crypto'
import {Effect,Layer,ManagedRuntime,Scope,Exit} from 'effect'
import {sql} from 'drizzle-orm'
import {AppFileSystem} from '@mimo-ai/shared/filesystem'
import * as CrossSpawnSpawner from '../../src/effect/cross-spawn-spawner'
import {Agent} from '../../src/agent/agent'
import {Instruction} from '../../src/session/instruction'
import {LSP} from '../../src/lsp'
import {Truncate} from '../../src/tool'
import {ReadTool} from '../../src/tool/read'
import {ProviderTest} from '../fake/provider'
import path from 'node:path'
import {Database} from '../../src/storage'
import {ProjectTable} from '../../src/project/project.sql'
import {SessionTable,MessageTable} from '../../src/session/session.sql'
import {Session} from '../../src/session'
import {AppRuntime} from '../../src/effect/app-runtime'
import {Instance} from '../../src/project/instance'
import {tmpdir} from '../fixture/fixture'
import {createToolTimingIssuer,markAdmittedToolTimingTurn,captureToolTiming} from '../../src/session/tool-timing'
import {encodeToolViewAdmission,captureToolView,enabledToolViewProfiles} from '../../src/session/tool-view-source'
import {bindToolViewProducer} from '../../src/session/tool-view-producer-admission'
import {readReadView,transferReadView,framedReadView} from '../../src/session/tool-view-read'
import {prepareToolViews} from '../../src/session/tool-view-assembly'
const runtime=ManagedRuntime.make(Layer.mergeAll(Agent.defaultLayer,AppFileSystem.defaultLayer,CrossSpawnSpawner.defaultLayer,Instruction.defaultLayer,LSP.defaultLayer,Truncate.defaultLayer,ProviderTest.fake().layer))

test('native read discloses shortened content and preserves History for an unnumbered view',async()=>{
 await using tmp=await tmpdir()
 await Instance.provide({directory:tmp.path,fn:async()=>{
  const now=Date.now(),sessionID='ses_read_view',messageID='msg_read_view',partID='prt_read_view'
  Database.use(db=>{db.insert(ProjectTable).values({id:'proj_read_view' as any,worktree:tmp.path,sandboxes:[] as any,time_created:now,time_updated:now} as any).run();db.insert(SessionTable).values({id:sessionID as any,project_id:'proj_read_view' as any,slug:'read-view',directory:tmp.path,title:'read-view',version:'1',time_created:now,time_updated:now}).run();db.insert(MessageTable).values({id:messageID as any,session_id:sessionID as any,agent_id:'main',data:{role:'assistant'} as any,time_created:now,time_updated:now}).run();if(!db.all<{name:string}>(sql`PRAGMA table_info(part)`).some(x=>x.name==='openclank_view_decision'))db.run(sql`ALTER TABLE part ADD COLUMN openclank_view_decision TEXT`)})
  const identity={sessionID,rootOperationID:'root',actorID:'main',executionRevision:1,innerTurnID:'turn'} as any
  const route={rootOperationID:'root',connectionID:'conn',providerID:'provider',modelID:'model',modelRouteID:'route'}
  const profiles={jsonWhitespace:false,terminalAnsi:false,htmlMarkdown:false,structuredSearch:false,repeatedLines:false,unifiedDiff:false,schemaAnnotations:false,toon:false,sourceOutlines:false,tap13:false,base64Lines:false,ndjsonLogs:false,readUnnumbered:true}
  const admission={version:2,requested:true,...route,sessionID,hostAccountID:'account',holderID:'holder',generation:2,settingsRevision:'a'.repeat(64),routeMethodKey:'route-key',profiles}
  const encoded=encodeToolViewAdmission(admission,sessionID,route,{providerID:'provider',modelID:'model'})!
  const ctx={sessionID,messageID,callID:'call',agent:'build',abort:AbortSignal.any([]),messages:[],metadata:()=>Effect.void,ask:()=>Effect.void} as any
  bindToolViewProducer(ctx,enabledToolViewProfiles(encoded,'read') as any,()=>true)
  const scope=await Effect.runPromise(Scope.make())
  const reader=await runtime.runPromise(ReadTool.pipe(Effect.flatMap(info=>info.init()),Effect.provideService(Scope.Scope,scope)))
  const filename=path.join(tmp.path,'sample.txt'), raw=Array.from({length:200},(_,i)=>`readable fixture line ${i} with enough content`)
  await Bun.write(filename,raw.join('\n')+'\n')
  const result=await runtime.runPromise(Effect.scoped(reader.execute({file_path:filename},ctx)));expect(readReadView(result)).toBe(true)
  const final={...result,metadata:{...result.metadata}};transferReadView(result,final);expect(readReadView(final)).toBe(true)
  const partial=await runtime.runPromise(Effect.scoped(reader.execute({file_path:filename,limit:1},ctx)));expect(readReadView(partial)).toBe(false);expect(partial.metadata.page.has_more).toBe(true)
  const changed={...final,output:final.output+'changed'};transferReadView(result,changed);expect(readReadView(changed)).toBe(false)
  const reminder={...final,metadata:{...final.metadata,loaded:['fixture']}};transferReadView(result,reminder);expect(readReadView(reminder)).toBe(false)
  const offCtx={...ctx,callID:'off'}, long=path.join(tmp.path,'long.txt');await Bun.write(long,'x'.repeat(2100)+'\n')
  const clipped=await runtime.runPromise(Effect.scoped(reader.execute({file_path:long},offCtx)));expect(readReadView(clipped)).toBe(false);expect(clipped.metadata.truncated).toBe(true);expect(clipped.metadata.truncation_reason).toBe('character_limit');expect(clipped.metadata.file.truncation_reason).toBe('character_limit');expect(clipped.metadata.page.has_more).toBe(false);expect(clipped.metadata.page.next_cursor).toBeUndefined();expect(clipped.metadata.file.page.next_cursor).toBeNull();expect(clipped.output).toContain('cannot be recovered by changing the line offset')
  const mixed=path.join(tmp.path,'mixed.txt');await Bun.write(mixed,'x'.repeat(2100)+'\nsecond\n')
  const clippedPage=await runtime.runPromise(Effect.scoped(reader.execute({file_path:mixed,limit:1},offCtx)));expect(clippedPage.metadata.page.has_more).toBe(true);expect(clippedPage.metadata.page.next_cursor).toBe(2);expect(clippedPage.metadata.truncation_reason).toBe('line_limit');expect(clippedPage.metadata.file.diagnostics.some((d:any)=>d.code==='character_limit')).toBe(true)
  const running={id:partID,sessionID,messageID,type:'tool',tool:'read',callID:'call',state:{status:'running',input:{},time:{start:now}}} as any
  const finished={...running,state:{status:'completed',input:{},output:final.output,title:final.title,metadata:final.metadata,time:{start:now,end:now+1}}} as any
  await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(running)))
  const issuer=createToolTimingIssuer();markAdmittedToolTimingTurn(identity,identity);const timing=captureToolTiming(issuer,identity,running)!
  const capture=captureToolView(encoded,timing,finished,route,readReadView(final)?'read-unnumbered-v1':undefined)!
  expect(capture.admission.profile).toBe('read-unnumbered-v1')
  await AppRuntime.runPromise(Session.Service.use(s=>s.updateTimedTool(finished,timing,capture)))
  const stored=JSON.parse(Database.use(db=>db.get<{v:string}>(sql`SELECT openclank_view_decision AS v FROM part WHERE id=${partID}`)!.v));expect(stored.version).toBe(1)
  const digest=createHash('sha256').update(final.output).digest('hex')
  const assembled=await prepareToolViews({encoded,identity,issuer,route,control:{disabled:false},historyAllowed:true,messages:[{info:{id:messageID,role:'assistant'},parts:[finished]}] as any,current:()=>true,ack:async()=>({version:1,accountID:'account',actorID:'main',chatID:'chat',sessionID,messageID,partID,revision:1,contentHash:stored.contentSHA256,outputSHA256:digest,outputBytes:Buffer.byteLength(final.output)}),check:async(refs)=>refs.length===1})
  const frame=assembled.views.get(partID)!;expect(frame).toContain('lines 1-200');expect(frame).toContain('history(');expect(frame).toContain(stored.contentSHA256);expect(frame.slice(frame.indexOf('\n')+1)).toBe(raw.join('\n'));expect(finished.state.output).toBe(final.output)
  const shortPath=path.join(tmp.path,'short.txt');await Bun.write(shortPath,'hello\n')
  const short=await runtime.runPromise(Effect.scoped(reader.execute({file_path:shortPath},ctx)));expect(readReadView(short)).toBe(true);expect(framedReadView(short.output,short.metadata,{messageID,partID,revision:1,contentHash:'a'.repeat(64)})).toBeUndefined()
  const {readUnnumbered:_read,...oldProfiles}=profiles,old={...admission,profiles:oldProfiles};expect(encodeToolViewAdmission(old,sessionID,route,{providerID:'provider',modelID:'model'})).toBe(JSON.stringify(old));expect(enabledToolViewProfiles(undefined,'read')).toEqual([])
  expect(encodeToolViewAdmission({...admission,profiles:{...profiles,readUnnumbered:false}},sessionID,route,{providerID:'provider',modelID:'model'})).toBeUndefined();issuer.revoke();await Effect.runPromise(Scope.close(scope,Exit.void))
 }})
})
