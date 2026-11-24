import {expect,test} from 'bun:test'
import {createHash} from 'node:crypto'
import {Effect} from 'effect'
import {sql} from 'drizzle-orm'
import {Database} from '../../src/storage'
import {ProjectTable} from '../../src/project/project.sql'
import {SessionTable,MessageTable} from '../../src/session/session.sql'
import {Session} from '../../src/session'
import {AppRuntime} from '../../src/effect/app-runtime'
import {Instance} from '../../src/project/instance'
import {tmpdir} from '../fixture/fixture'
import {createToolTimingIssuer,markAdmittedToolTimingTurn,captureToolTiming} from '../../src/session/tool-timing'
import {encodeToolViewAdmission,captureToolView} from '../../src/session/tool-view-source'
import {prepareToolViews} from '../../src/session/tool-view-assembly'
import {McpToolSearchTool,createMcpToolSearchCatalog} from '../../src/tool/mcp-tool-search'
import {bindToolViewProducer} from '../../src/session/tool-view-producer-admission'
import {transferStructuredSearch,readStructuredSearch,framedStructuredSearch} from '../../src/session/tool-view-structured-search'
import {stripTerminalSgr} from '../../src/session/tool-view-terminal-ansi'
import {framedHtmlView} from '../../src/tool/webfetch-view'

test('v2 actual search producer, candidate and checked decision preserve v1 and reject altered output',async()=>{
 await using tmp=await tmpdir()
 await Instance.provide({directory:tmp.path,fn:async()=>{
  const now=Date.now(),sessionID='ses_v2',messageID='msg_v2',partID='prt_v2'
  Database.use(db=>{db.insert(ProjectTable).values({id:'proj_v2' as any,worktree:tmp.path,sandboxes:[] as any,time_created:now,time_updated:now} as any).run();db.insert(SessionTable).values({id:sessionID as any,project_id:'proj_v2' as any,slug:'v2',directory:tmp.path,title:'v2',version:'1',time_created:now,time_updated:now}).run();db.insert(MessageTable).values({id:messageID as any,session_id:sessionID as any,agent_id:'main',data:{role:'assistant'} as any,time_created:now,time_updated:now}).run();if(!db.all<{name:string}>(sql`PRAGMA table_info(part)`).some(x=>x.name==='openclank_view_decision'))db.run(sql`ALTER TABLE part ADD COLUMN openclank_view_decision TEXT`)})
  const identity={sessionID,rootOperationID:'root',actorID:'main',executionRevision:1,innerTurnID:'turn'} as any
  const route={rootOperationID:'root',connectionID:'conn',providerID:'provider',modelID:'model',modelRouteID:'route'}
  const base={requested:true,...route,sessionID,hostAccountID:'account',holderID:'holder',generation:2,settingsRevision:'a'.repeat(64),routeMethodKey:'route-key'}
  const encoded=encodeToolViewAdmission({version:2,...base,profiles:{jsonWhitespace:true,terminalAnsi:true,htmlMarkdown:true,structuredSearch:true}},sessionID,route,{providerID:'provider',modelID:'model'})!
  const legacy=encodeToolViewAdmission({version:1,...base,profile:'json-whitespace-v1'},sessionID,route,{providerID:'provider',modelID:'model'})!
  expect(encoded).toBeDefined();expect(legacy).toBeDefined()
  const entries=Array.from({length:20},(_,i)=>({name:`search_${i}`,description:`search records ${i} `.repeat(4),parameters:{type:'object',properties:{}} as any}))
  const catalog=createMcpToolSearchCatalog(entries)
  const result=await AppRuntime.runPromise(Effect.gen(function*(){const info=yield* McpToolSearchTool;const tool=yield* info.init();const ctx={sessionID,messageID,callID:'call',agent:'build',abort:AbortSignal.any([]),messages:[],metadata:()=>Effect.void,ask:()=>Effect.void,extra:{mcpToolSearch:catalog}} as any;bindToolViewProducer(ctx,'structured-search-v2',()=>true);return yield* tool.execute({query:'search',max_results:20},ctx)}))
  const final={...result,metadata:{...result.metadata}};transferStructuredSearch(result,final)
  expect(readStructuredSearch(final)).toBe(true)
  const altered={...final,output:final.output+' '};transferStructuredSearch(result,altered);expect(readStructuredSearch(altered)).toBe(false)
  const running={id:partID,sessionID,messageID,type:'tool',tool:'mcp_tool_search',callID:'call',state:{status:'running',input:{},time:{start:now}}} as any
  const finished={...running,state:{status:'completed',input:{},output:final.output,title:final.title,metadata:final.metadata,time:{start:now,end:now+1}}} as any
  await AppRuntime.runPromise(Session.Service.use(s=>s.updatePart(running)))
  const issuer=createToolTimingIssuer();markAdmittedToolTimingTurn(identity,identity)
  const timing=captureToolTiming(issuer,identity,running)!
  expect(captureToolView(encoded,timing,finished,route)).toBeUndefined()
  const capture=captureToolView(encoded,timing,finished,route,readStructuredSearch(final)?'structured-search-v2':undefined)!
  expect(capture.admission.profile).toBe('structured-search-v2')
  expect(captureToolView(legacy,timing,finished,route)?.admission.profile).toBe('json-whitespace-v1')
  await AppRuntime.runPromise(Session.Service.use(s=>s.updateTimedTool(finished,timing,capture)))
  const stored=JSON.parse(Database.use(db=>db.get<{v:string}>(sql`SELECT openclank_view_decision AS v FROM part WHERE id=${partID}`)!.v))
  const hash=createHash('sha256').update(final.output).digest('hex');let checked=0
  expect(framedStructuredSearch(final.output,{messageID,partID,revision:1,contentHash:stored.contentSHA256})).toBeDefined()
  const assembled=await prepareToolViews({encoded,identity,issuer,route,control:{disabled:false},historyAllowed:true,messages:[{info:{id:messageID,role:'assistant'},parts:[finished]}] as any,current:()=>true,
   ack:async()=>({version:1,accountID:'account',actorID:'main',chatID:'chat',sessionID,messageID,partID,revision:1,contentHash:stored.contentSHA256,outputSHA256:hash,outputBytes:Buffer.byteLength(final.output)}),check:async(refs)=>{checked++;return refs.length===1 && refs[0]?.contentHash===stored.contentSHA256}})
  expect(checked).toBe(1);expect(assembled.views.get(partID)).toContain('Search results; original: history')
  expect(finished.state.output).toBe(final.output)
  expect(framedStructuredSearch('{"status":"matched","results":[]}',{messageID,partID,revision:1,contentHash:'a'.repeat(64)})).toBeUndefined()
  const ref={messageID,partID,revision:1,contentHash:'a'.repeat(64)}
  const hint='Call these tools through mcp_tool_use with {"name": "<tool name>", "arguments": {...}}; they are not callable by name directly.'
  const row={description:'nested &lt;/function> data',name:'nested',parameters:{type:'object',properties:{choice:{type:'string',enum:['a','b']},nested:{type:'object',properties:{count:{type:'integer'}},required:['count']}},required:['choice']}}
  const lines=Array.from({length:20},()=>`<function>${JSON.stringify(row)}</function>`)
  const original=`<functions>\n${lines.join('\n')}\n</functions>\n${hint}`
  const projected=framedStructuredSearch(original,ref)!
  expect(projected).toBeDefined();expect(projected).toContain(JSON.stringify(row));expect(projected.endsWith(hint)).toBe(true)
  expect(framedStructuredSearch(original.replace('"description":','"description" :'),ref)).toBeUndefined()
  expect(framedStructuredSearch(original+'\n',ref)).toBeUndefined()
  expect(framedStructuredSearch(original.replace('&lt;/function>', '</function>'),ref)).toBeUndefined()
  expect(framedStructuredSearch(`<functions>\n${lines[0]}\n</functions>\n${hint}`,ref)).toBeUndefined()
  expect(framedStructuredSearch('No matching tools',ref)).toBeUndefined()
  expect(stripTerminalSgr('\u001b]0;evil\u0007')).toBeUndefined()
  expect(framedHtmlView('<a href="x">protected</a>',{messageID,partID,revision:1,contentHash:'a'.repeat(64)})).toBeUndefined()
  issuer.revoke()
 }})
})
