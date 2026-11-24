import { test, expect } from 'bun:test'
import { spawn, spawnSync } from 'node:child_process'
import net from 'node:net'
import * as fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

test('managed resource pages are scoped fragments with exact action locators and stale cursors', async () => {
  const binary=process.env.P05_HISTORY_BIN
  if (!binary || !process.env.XDG_DATA_HOME || !process.env.XDG_CONFIG_HOME || !process.env.XDG_CACHE_HOME || !process.env.XDG_STATE_HOME) throw new Error('isolated fixture required')
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'p05-fragments-'));let socket=path.join(root,'history.sock')
  let service=spawn(binary,[socket,path.join(root,'catalog'),path.join(root,'lore'),'fixture-account',root],{env:{...process.env,OPENCLANK_HISTORY_TOKEN:'fixture-token'},stdio:['ignore','ignore','pipe']})
  let errors='';service.stderr.on('data',chunk=>{if(errors.length<16384)errors+=String(chunk).slice(0,16384-errors.length)})
  const call=(value:unknown)=>new Promise<any>((resolve,reject)=>{const client=net.createConnection(socket);let data='';client.setEncoding('utf8');client.on('error',reject);client.on('connect',()=>client.end(JSON.stringify(value)+'\n'));client.on('data',chunk=>data+=chunk);client.on('end',()=>{try{resolve(JSON.parse(data))}catch(error){reject(error)}})})
  try {
    for(let i=0;i<300;i++){if(service.exitCode!==null||service.signalCode!==null)throw new Error(errors);if(await fs.stat(socket).then(()=>true,()=>false))break;if(i===299)throw new Error('service unavailable');await new Promise(resolve=>setTimeout(resolve,20))}
    const [{Effect,Layer},{NodeFileSystem},{AppFileSystem}]=await Promise.all([import('effect'),import('@effect/platform-node'),import('@mimo-ai/shared/filesystem')])
    const target=path.join(root,'example.txt');await fs.writeFile(target,'before')
    const origin={version:1 as const,host_chat_id:'chat',engine_session_id:'engine',root_operation_id:'root',actor_id:'main',execution_revision:1,inner_turn_id:'turn',tool_call_id:'call',assistant_message_id:'message'}
    let resource=''
    const write=async(id:string,actor:string)=>{const history:any={actorId:'fixture-actor',accountId:'fixture-account',workspaceId:'fixture-workspace',workspaceRoot:root,socketPath:socket,token:'fixture-token',managedToolOrigin:{...origin,actor_id:actor},status:{status:'paused',history_status:'paused',capture_phase:'unavailable',durable:false,coverage:'NoCapture'}};await Effect.runPromise(Effect.gen(function*(){const files=yield* AppFileSystem.Service;yield* files.atomicWrite({path:target,content:id,actionId:id,history})}).pipe(Effect.provide(AppFileSystem.layer.pipe(Layer.provide(NodeFileSystem.layer)))));const locator=history.status.managed_action_ref as string;resource=JSON.parse(Buffer.from(locator.slice('oc-history-action:v1:'.length),'base64url').toString()).resourceID}
    await write('action-1','main');await write('action-2','other');await write('action-3','main')
    const auth={actor_id:'fixture-human',account_id:'fixture-account',token:'fixture-token'},envelope={protocol_version:1,auth,action_id:'read'}
    const samePage=(await call({ListManagedChangeFragments:{envelope,resource_id:resource,limit:3}})).ManagedChangeFragments
    expect(samePage.groups.length).toBe(2);expect(samePage.groups[0].actions.map((item:any)=>item.change.action_id)).toEqual(['action-1','action-3']);expect(samePage.groups[1].actions.map((item:any)=>item.change.action_id)).toEqual(['action-2'])
    const request={envelope,resource_id:resource,limit:2}
    const first=(await call({ListManagedChangeFragments:request})).ManagedChangeFragments
    expect(first.scope).toBe('one_authorized_resource');expect(first.coverage).toBe('page_fragment');expect(first.order).toBe('action_id');expect(first.groups.length).toBe(2);expect(first.next_cursor).toBeTruthy()
    const second=(await call({ListManagedChangeFragments:{...request,cursor:first.next_cursor}})).ManagedChangeFragments
    expect(second.groups.length).toBe(1);expect(second.groups[0].group_id).toBe(first.groups[0].group_id)
    const locator=first.groups[0].actions[0].locator as string
    const value=JSON.parse(Buffer.from(locator.slice('oc-history-action:v1:'.length),'base64url').toString())
    const exact=await call({GetResourceChange:{envelope:{...envelope,action_id:value.actionID},resource_id:value.resourceID,before_version_id:value.beforeVersionID,after_version_id:value.afterVersionID,origin:value.origin}})
    expect(exact.ResourceChange?.action_id).toBe(value.actionID)
    await write('action-4','main')
    expect((await call({ListManagedChangeFragments:{...request,cursor:first.next_cursor}})).Unavailable).toBeDefined()
    expect((await call({ListManagedChangeFragments:{...request,envelope:{...envelope,auth:{...auth,account_id:'foreign'}}}})).ManagedChangeFragments).toBeUndefined()
    expect((await call({ListOrderedManagedChanges:request})).OrderedManagedChanges).toBeUndefined()
    service.kill('SIGTERM');await new Promise<void>(resolve=>service.once('exit',()=>resolve()))
    const preparer=process.env.P05_HISTORY_PREPARER;if(!preparer)throw new Error('isolated preparer required')
    const original=await fs.readFile(path.join(root,'catalog'))
    const preparation=spawnSync(preparer,[path.join(root,'catalog'),path.join(root,'prepared'), '2','--format3'],{encoding:'utf8'})
    if(preparation.status!==0)throw new Error(preparation.stderr)
    expect(await fs.readFile(path.join(root,'catalog'))).toEqual(original)
    socket=path.join(root,'prepared.sock');service=spawn(binary,[socket,path.join(root,'prepared'),path.join(root,'lore'),'fixture-account',root],{env:{...process.env,OPENCLANK_HISTORY_TOKEN:'fixture-token'},stdio:['ignore','ignore','pipe']})
    service.stderr.on('data',chunk=>{if(errors.length<16384)errors+=String(chunk).slice(0,16384-errors.length)})
    for(let i=0;i<300;i++){if(service.exitCode!==null||service.signalCode!==null)throw new Error(errors);if(await fs.stat(socket).then(()=>true,()=>false))break;if(i===299)throw new Error('prepared service unavailable');await new Promise(resolve=>setTimeout(resolve,20))}
    const time=(await call({ListOrderedManagedChanges:{...request,limit:10}})).OrderedManagedChanges
    expect(time.coverage).toBe('recorded_timestamps_only');expect(time.items.length).toBe(4)
    const exactGroup=(await call({ListOrderedManagedChanges:{...request,limit:10,origin_action_id:'action-1'}})).OrderedManagedChanges
    expect(exactGroup.coverage).toBe('recorded_origin_only');expect(exactGroup.items.map((v:any)=>v.change.action_id)).toEqual(['action-1','action-3','action-4'])
    expect((await call({ListOrderedManagedChanges:{...request,origin_action_id:'action-1',resource_id:'file:foreign'}})).OrderedManagedChanges).toBeUndefined()
    expect((await call({ListOrderedManagedChanges:{...request,envelope:{...envelope,auth:{...auth,account_id:'foreign'}}}})).OrderedManagedChanges).toBeUndefined()
  } finally {if(service.exitCode===null&&service.signalCode===null)service.kill('SIGTERM');await new Promise<void>(resolve=>{if(service.exitCode!==null||service.signalCode!==null)return resolve();const timer=setTimeout(()=>service.kill('SIGKILL'),1000);service.once('exit',()=>{clearTimeout(timer);resolve()})});await fs.rm(root,{recursive:true,force:true})}
},30000)
