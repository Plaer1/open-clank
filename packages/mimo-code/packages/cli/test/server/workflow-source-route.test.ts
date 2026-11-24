import { expect, test } from 'bun:test'
import { ManagedRuntime } from 'effect'
import { TaskRegistry } from '../../src/task/registry'
import { createHash } from 'node:crypto'
import { Server } from '../../src/server/server'
import { Instance } from '../../src/project/instance'
import { Database, sql } from '../../src/storage'
import { ManagedProvider } from '../../src/acp/managed-provider'
import { installManagedSessionBinding } from '../../src/memory/session-scope'
import { createToolTimingIssuer, installToolTimingIssuer, markAdmittedToolTimingTurn, retireAdmittedToolTimingTurn } from '../../src/session/tool-timing'
import { captureWorkflowSource, bindWorkflowChild, claimWorkflowWriter, releaseWorkflowSource, bindWorkflowTaskAssignment, releaseWorkflowTaskAssignment } from '../../src/session/workflow-source'
import { MessageID, PartID } from '../../src/session/schema'
import { tmpdir } from '../fixture/fixture'

test('actual workflow source route proves persisted started fresh part', async () => {
 await using tmp=await tmpdir()
 const app=Server.Default().app, query=`?directory=${encodeURIComponent(tmp.path)}`
 const post=(url:string,body:unknown)=>app.request(url+query,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)})
 const root=await (await post('/session',{title:'source'})).json() as any
 const child=await (await post('/session',{title:'writer',parentID:root.id})).json() as any
 expect(root.id).toBeTruthy();expect(child.parentID).toBe(root.id)
 await Instance.provide({directory:tmp.path,fn:async()=>{
  process.env.OPEN_CLANK_MANAGED='1';ManagedProvider.resetForTest()
  const host={async extMethod(method:string,params:any) {
   if (method.endsWith('/account/bind')) return {bindingID:`binding-${params.actorID}`,bindingRevision:1,rootOperationID:'root-op',
    connectionID:'local',providerID:'ollama',billingLane:'local',modelID:'model',modelRouteID:'route',scopeKind:'actor_execution',
    sessionID:params.sessionID,actorID:params.actorID,executionRevision:params.executionRevision,credentialRequired:false,source:'keyless',attempt:1,committed:false}
   if (method.endsWith('/logging/v1/admit')) return null
   if (method.endsWith('/session/v1/binding/read')) return {engineSessionID:root.id,stableChatID:'chat',owner:'alice',canonicalCwd:tmp.path,
    workspaceRevision:1,authorityWorkspaceID:'workspace',memoryWorkspaceID:'memory',copalWorkspace:'workspace',memoryEnabled:true,engineAliases:[],mapRevision:1,mappingRevision:1}
   throw Error(`unexpected ${method}`)
  }} as any
  ManagedProvider.installHostConnection(host);const issuer=createToolTimingIssuer();installToolTimingIssuer(host,issuer)
  installManagedSessionBinding(root.id,{owner:'alice',stableChatID:'chat',engineSessionID:root.id,engineAliases:[],memoryWorkspaceID:'memory',authorityWorkspaceID:'workspace',copalWorkspace:'workspace',physicalCwd:tmp.path,bindingRevision:1,mapRevision:1,mappingRevision:1,memoryEnabled:true,transition:null})
  const parent={sessionID:root.id,actorID:'main',executionRevision:1,rootOperationID:'root-op',innerTurnID:'root-turn'} as any
  const writer={sessionID:child.id,actorID:'writer',executionRevision:1,rootOperationID:'root-op',innerTurnID:'writer-turn'} as any
  const preflight=(id:any)=>({bootstrap:{stableChatID:'chat',owner:'alice',rootSessionID:root.id,sessionID:id.sessionID,actorID:id.actorID,rootOperationID:'root-op',expectedExecutionRevision:0,mapRevision:1,mappingRevision:1,workspaceRevision:1,modelRouteID:'route'},connectionID:'local',providerID:'ollama',billingLane:'local' as const,modelID:'model',modelRouteID:'route'})
  const context={ownerAccountID:'account',chatID:'chat',workspaceID:'workspace'}
  const parentLease=await ManagedProvider.activateActorOperation(parent,preflight(parent),{providerID:'local',modelID:'model'},context)
  const writerLease=await ManagedProvider.activateActorOperation(writer,preflight(writer),{providerID:'local',modelID:'model'},context)
  markAdmittedToolTimingTurn(parent,parent);markAdmittedToolTimingTurn(writer,writer)
  const handle=captureWorkflowSource(parent)!;expect(bindWorkflowChild(handle,child.id,'writer')).toBe(true)
  const grant=claimWorkflowWriter(writer)!;expect(grant).toBeDefined()
  const msg=MessageID.ascending(), part=PartID.ascending(), call='tool-call', args={operation:'write',role:'metaplan',name:null,expectedRevision:null,text:'draft'}
  const sha=(v:unknown)=>createHash('sha256').update(JSON.stringify(v),'utf8').digest('hex')
  const op='workflowop_'+sha(['openclank.workflow.operation.v1','account',child.id,'root-op','writer',1,'writer-turn',call])
  const digest=sha(['openclank.workflow-artifact.v1','write','metaplan',null,null,'draft'])
  const request={sourceToken:grant.sourceToken,messageID:msg,toolCallID:call,argsSHA256:digest}
  try {
   Database.use(db=>{
    for(const [sid,aid,mode,par,agent] of [[root.id,'main','main',null,'build'],[child.id,'writer','subagent','main','checkpoint-writer']])
     db.run(sql`INSERT OR REPLACE INTO actor_registry (session_id,actor_id,mode,parent_actor_id,status,execution_revision,lifecycle,agent,description,context_mode,background,last_turn_time,turn_count,instance_id,time_created,time_updated) VALUES (${sid},${aid},${mode},${par},'running',1,'ephemeral',${agent},'test','none',1,1,0,'test',1,1)`)
    db.run(sql`INSERT INTO message (id,session_id,agent_id,time_created,time_updated,data) VALUES (${msg},${child.id},'writer',1,1,${JSON.stringify({role:'assistant',metadata:{managedExecution:writer}})})`)
    db.run(sql`INSERT INTO part (id,session_id,message_id,time_created,time_updated,data) VALUES (${part},${child.id},${msg},1,1,${JSON.stringify({type:'tool',tool:'workflow_artifact',callID:call,execution:'not-started',state:{status:'running',input:args,metadata:{workflowArtifactOperationID:op}}})})`)
   })
   ManagedProvider.markFreshTaskCall({...writer,messageID:msg,partID:part,toolCallID:call,toolName:'workflow_artifact'})
   expect(await ManagedProvider.claimFreshTaskCall({...writer,messageID:msg,toolCallID:call,toolName:'workflow_artifact'})).toBe(true)
   expect((await post(`/session/${root.id}/workflow-source/read`,request)).status).toBe(409)
   Database.use(db=>db.run(sql`UPDATE part SET data=json_set(data,'$.execution','started') WHERE id=${part}`))
   const response=await post(`/session/${root.id}/workflow-source/read`,request)
   expect(response.status).toBe(200);expect(await response.json()).toMatchObject({state:'current',source:{sessionID:root.id},writer:{sessionID:child.id},argsSHA256:digest})
   expect((await post(`/session/${root.id}/workflow-source/read`,{...request,argsSHA256:'0'.repeat(64)})).status).toBe(409)
   const readPart=PartID.ascending(), readCall='read-call', readArgs={operation:'read',role:'slice',name:'scope'}
   const readDigest=sha(['openclank.workflow-artifact.v1','read','slice','scope',null,null])
   Database.use(db=>db.run(sql`INSERT INTO part (id,session_id,message_id,time_created,time_updated,data) VALUES (${readPart},${child.id},${msg},1,1,${JSON.stringify({type:'tool',tool:'workflow_artifact',callID:readCall,execution:'started',state:{status:'running',input:readArgs}})})`))
   ManagedProvider.markFreshTaskCall({...writer,messageID:msg,partID:readPart,toolCallID:readCall,toolName:'workflow_artifact'})
   expect(await ManagedProvider.claimFreshTaskCall({...writer,messageID:msg,toolCallID:readCall,toolName:'workflow_artifact'})).toBe(true)
   const readResponse=await post(`/session/${root.id}/workflow-source/read`,{sourceToken:grant.sourceToken,messageID:msg,toolCallID:readCall,argsSHA256:readDigest})
   expect(readResponse.status).toBe(200);expect(await readResponse.json()).toMatchObject({state:'current',argsSHA256:readDigest})
   expect((await post(`/session/${root.id}/workflow-source/read`,{sourceToken:grant.sourceToken,messageID:msg,toolCallID:readCall,argsSHA256:digest})).status).toBe(409)
   // Root-self uses the real admitted main turn, never a fabricated task or child.
   const rootGrant=claimWorkflowWriter(parent)!
   expect(rootGrant.source.source).toEqual({kind:'root-operation',rootOperationID:'root-op'})
   const rootMsg=MessageID.ascending(),rootPart=PartID.ascending(),rootCall='root-call'
   const rootArgs={operation:'read',role:'metaplan',name:null}
   const rootDigest=sha(['openclank.workflow-artifact.v1','read','metaplan',null,null,null])
   Database.use(db=>{
    db.run(sql`INSERT INTO message (id,session_id,agent_id,time_created,time_updated,data) VALUES (${rootMsg},${root.id},'main',1,1,${JSON.stringify({role:'assistant',metadata:{managedExecution:parent}})})`)
    db.run(sql`INSERT INTO part (id,session_id,message_id,time_created,time_updated,data) VALUES (${rootPart},${root.id},${rootMsg},1,1,${JSON.stringify({type:'tool',tool:'workflow_artifact',callID:rootCall,execution:'started',state:{status:'running',input:rootArgs}})})`)
   })
   ManagedProvider.markFreshTaskCall({...parent,messageID:rootMsg,partID:rootPart,toolCallID:rootCall,toolName:'workflow_artifact'})
   expect(await ManagedProvider.claimFreshTaskCall({...parent,messageID:rootMsg,toolCallID:rootCall,toolName:'workflow_artifact'})).toBe(true)
   const rootRequest={sourceToken:rootGrant.sourceToken,messageID:rootMsg,toolCallID:rootCall,argsSHA256:rootDigest}
   const rootResponse=await post(`/session/${root.id}/workflow-source/read`,rootRequest)
   expect(rootResponse.status).toBe(200);expect(await rootResponse.json()).toMatchObject({source:{actorID:'main',source:{kind:'root-operation',rootOperationID:'root-op'}},writer:{sessionID:root.id,actorID:'main',innerTurnID:'root-turn'}})
   expect((await post(`/session/${root.id}/workflow-source/read`,{...rootRequest,argsSHA256:'0'.repeat(64)})).status).toBe(409)
   Database.use(db=>db.run(sql`UPDATE part SET data=json_set(data,'$.state.input.role','checkpoint') WHERE id=${rootPart}`))
   const checkpointDigest=sha(['openclank.workflow-artifact.v1','read','checkpoint',null,null,null])
   expect((await post(`/session/${root.id}/workflow-source/read`,{...rootRequest,argsSHA256:checkpointDigest})).status).toBe(409)
   const taskRuntime=ManagedRuntime.make(TaskRegistry.defaultLayer)
   try {
    const task=await taskRuntime.runPromise(TaskRegistry.Service.use(s=>s.create({session_id:root.id,summary:'progress'})))
    const started=await taskRuntime.runPromise(TaskRegistry.Service.use(s=>s.start({session_id:root.id,id:task.id,owner:'main'})))
    expect(bindWorkflowTaskAssignment({...parent,taskID:task.id},started)).toBe(true)
    const taskGrant=claimWorkflowWriter(parent)!;expect(taskGrant.source.source).toMatchObject({kind:'native-task',sessionID:root.id,taskID:task.id})
    const progressMsg=MessageID.ascending(), progressPart=PartID.ascending(), progressCall='progress-call'
    const progressArgs={operation:'read',role:'progress',name:null}
    const progressDigest=sha(['openclank.workflow-artifact.v1','read','progress',null,null,null])
    Database.use(db=>{
     db.run(sql`INSERT INTO message (id,session_id,agent_id,time_created,time_updated,data) VALUES (${progressMsg},${root.id},'main',1,1,${JSON.stringify({role:'assistant',metadata:{managedExecution:parent}})})`)
     db.run(sql`INSERT INTO part (id,session_id,message_id,time_created,time_updated,data) VALUES (${progressPart},${root.id},${progressMsg},1,1,${JSON.stringify({type:'tool',tool:'workflow_artifact',callID:progressCall,execution:'started',state:{status:'running',input:progressArgs}})})`)
    })
    ManagedProvider.markFreshTaskCall({...parent,messageID:progressMsg,partID:progressPart,toolCallID:progressCall,toolName:'workflow_artifact'})
    expect(await ManagedProvider.claimFreshTaskCall({...parent,messageID:progressMsg,toolCallID:progressCall,toolName:'workflow_artifact'})).toBe(true)
    const progressRequest={sourceToken:taskGrant.sourceToken,messageID:progressMsg,toolCallID:progressCall,argsSHA256:progressDigest}
    const progress=await post(`/session/${root.id}/workflow-source/read`,progressRequest)
    expect(progress.status).toBe(200);expect(await progress.json()).toMatchObject({source:{source:{kind:'native-task',taskID:task.id}},writer:{actorID:'main'}})
    await taskRuntime.runPromise(TaskRegistry.Service.use(s=>s.start({session_id:root.id,id:task.id,owner:'other'})))
    await taskRuntime.runPromise(TaskRegistry.Service.use(s=>s.start({session_id:root.id,id:task.id,owner:'main'})))
    expect((await post(`/session/${root.id}/workflow-source/read`,progressRequest)).status).toBe(409)
    releaseWorkflowTaskAssignment(root.id,'main',1)
   } finally {await taskRuntime.dispose()}

  } finally {releaseWorkflowSource(handle);retireAdmittedToolTimingTurn(parent);retireAdmittedToolTimingTurn(writer);await writerLease.release();await parentLease.release();ManagedProvider.resetForTest();delete process.env.OPEN_CLANK_MANAGED}
 }})
 await Instance.disposeAll()
},30000)
