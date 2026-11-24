import {expect,test} from 'bun:test'
import {createHash} from 'node:crypto'
import {Effect,Layer,ManagedRuntime} from 'effect'
import {sql} from 'drizzle-orm'
import {AppFileSystem} from '@mimo-ai/shared/filesystem'
import * as CrossSpawnSpawner from '../../src/effect/cross-spawn-spawner'
import {Plugin} from '../../src/plugin'
import {Truncate} from '../../src/tool'
import {Agent} from '../../src/agent/agent'
import {WebFetchTool} from '../../src/tool/webfetch'
import {FetchHttpClient} from 'effect/unstable/http'
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
import {readWebfetchMarkdown,transferWebfetchHtml} from '../../src/tool/webfetch-view'
import {markdownSlim} from '../../src/session/tool-view-markdown-slim'
import {prepareToolViews} from '../../src/session/tool-view-assembly'
const runtime=ManagedRuntime.make(Layer.mergeAll(FetchHttpClient.layer,CrossSpawnSpawner.defaultLayer,AppFileSystem.defaultLayer,Plugin.defaultLayer,Truncate.defaultLayer,Agent.defaultLayer))

test('actual WebFetch Markdown retains History recovery',async()=>{
 await using tmp=await tmpdir()
 await Instance.provide({directory:tmp.path,fn:async()=>{
  const now=Date.now(),sessionID='ses_rle',messageID='msg_rle',partID='prt_rle'
  Database.use(db=>{db.insert(ProjectTable).values({id:'proj_rle' as any,worktree:tmp.path,sandboxes:[] as any,time_created:now,time_updated:now} as any).run();db.insert(SessionTable).values({id:sessionID as any,project_id:'proj_rle' as any,slug:'rle',directory:tmp.path,title:'rle',version:'1',time_created:now,time_updated:now}).run();db.insert(MessageTable).values({id:messageID as any,session_id:sessionID as any,agent_id:'main',data:{role:'assistant'} as any,time_created:now,time_updated:now}).run();if(!db.all<{name:string}>(sql`PRAGMA table_info(part)`).some(x=>x.name==='openclank_view_decision'))db.run(sql`ALTER TABLE part ADD COLUMN openclank_view_decision TEXT`)})
  const identity={sessionID,rootOperationID:'root',actorID:'main',executionRevision:1,innerTurnID:'turn'} as any
  const route={rootOperationID:'root',connectionID:'conn',providerID:'provider',modelID:'model',modelRouteID:'route'}
  const admission={version:2,requested:true,...route,sessionID,hostAccountID:'account',holderID:'holder',generation:2,settingsRevision:'a'.repeat(64),routeMethodKey:'route-key',profiles:{jsonWhitespace:false,terminalAnsi:false,htmlMarkdown:false,structuredSearch:false,repeatedLines:false,unifiedDiff:false,schemaAnnotations:false,toon:false,sourceOutlines:false,tap13:false,base64Lines:false,ndjsonLogs:false,markdownSlim:true}}
  const encoded=encodeToolViewAdmission(admission,sessionID,route,{providerID:'provider',modelID:'model'})!
  const ctx={sessionID,messageID,callID:'call',agent:'build',abort:AbortSignal.any([]),messages:[],metadata:()=>Effect.void,ask:()=>Effect.void} as any
  bindToolViewProducer(ctx,enabledToolViewProfiles(encoded,'webfetch') as any,()=>true)
  const url='https://93.184.216.34/fixture', oldFetch=globalThis.fetch
  let mime='text/markdown', body=('# heading\n'+('<!-- omitted fixture detail -->\n').repeat(25)+'[link](https://example.test)\n\n\n\n```text\n<!-- literal -->\n```\n')
  globalThis.fetch=Object.assign(async()=>{const response=new Response(body,{status:200,headers:{'content-type':mime}});Object.defineProperty(response,'url',{value:url});return response},{preconnect:oldFetch.preconnect})
  const web=await runtime.runPromise(WebFetchTool.pipe(Effect.flatMap(info=>info.init())))
  const result=await runtime.runPromise(web.execute({url,format:'markdown'},ctx))
  expect(readWebfetchMarkdown(result,ctx)).toBeDefined()
  const final={...result,metadata:{...result.metadata}};transferWebfetchHtml(result,final)
  expect(readWebfetchMarkdown(final,ctx)).toBeDefined()
  const slim=markdownSlim(final.output)!;expect(slim.removedCommentLines).toBe(25);expect(slim.removedBlankLines).toBe(1);expect(slim.text).toContain('<!-- literal -->');expect(slim.text).toContain('[link](https://example.test)')
  expect(markdownSlim('<!-- x -->\n<pre>literal</pre>\n')).toBeUndefined();expect(markdownSlim('<!-- x -->\n<https://example.test>\n')).toBeUndefined();expect(markdownSlim('<!-- x -->\n```\nunclosed\n')).toBeUndefined()
  mime='text/html';body='<p>HTML conversion text</p>';const html=await runtime.runPromise(web.execute({url,format:'markdown'},ctx));expect(html.output).toContain('HTML conversion text');expect(readWebfetchMarkdown(html,ctx)).toBeDefined()
  mime='text/plain';body='<!-- not authorized -->\n';const plain=await runtime.runPromise(web.execute({url,format:'markdown'},ctx));expect(readWebfetchMarkdown(plain,ctx)).toBeUndefined()
  const changed={...final,output:final.output+'changed'};transferWebfetchHtml(result,changed);expect(readWebfetchMarkdown(changed,ctx)).toBeUndefined()
  globalThis.fetch=oldFetch
  const running={id:partID,sessionID,messageID,type:'tool',tool:'webfetch',callID:'call',state:{status:'running',input:{url,format:'markdown'},time:{start:now}}} as any
  const finished={...running,state:{status:'completed',input:{url,format:'markdown'},output:final.output,title:final.title,metadata:final.metadata,time:{start:now,end:now+1}}} as any
  await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(running)))
  const issuer=createToolTimingIssuer();markAdmittedToolTimingTurn(identity,identity);const timing=captureToolTiming(issuer,identity,running)!
  const capture=captureToolView(encoded,timing,finished,route,readWebfetchMarkdown(final,ctx)?'markdown-slim-v1':undefined)!
  expect(capture.admission.profile).toBe('markdown-slim-v1')
  await AppRuntime.runPromise(Session.Service.use(s=>s.updateTimedTool(finished,timing,capture)))
  const stored=JSON.parse(Database.use(db=>db.get<{v:string}>(sql`SELECT openclank_view_decision AS v FROM part WHERE id=${partID}`)!.v))
  let checked=0;const digest=createHash('sha256').update(final.output).digest('hex')
  const assembled=await prepareToolViews({encoded,identity,issuer,route,control:{disabled:false},historyAllowed:true,messages:[{info:{id:messageID,role:'assistant'},parts:[finished]}] as any,current:()=>true,ack:async()=>({version:1,accountID:'account',actorID:'main',chatID:'chat',sessionID,messageID,partID,revision:1,contentHash:stored.contentSHA256,outputSHA256:digest,outputBytes:Buffer.byteLength(final.output)}),check:async(refs)=>{checked++;return refs.length===1}})
  expect(checked).toBe(1);expect(assembled.views.get(partID)).toContain('comment lines');expect(assembled.views.get(partID)).toContain('history(');expect(finished.state.output).toBe(final.output)
  expect(enabledToolViewProfiles(undefined,'webfetch')).toEqual([]);issuer.revoke()
 }})
})
