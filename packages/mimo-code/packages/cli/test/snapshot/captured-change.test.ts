import { test, expect } from 'bun:test'
import { spawn } from 'node:child_process'
import net from 'node:net'
import * as fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

test('human exact captured action reads its original versions, not the current file', async () => {
  const binary=process.env.P05_HISTORY_BIN
  if (!binary || !process.env.XDG_DATA_HOME || !process.env.XDG_CONFIG_HOME || !process.env.XDG_CACHE_HOME || !process.env.XDG_STATE_HOME) throw new Error('isolated fixture required')
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'p05-selector-')), socket=path.join(root,'history.sock')
  const service=spawn(binary,[socket,path.join(root,'catalog'),path.join(root,'lore'),'fixture-account',root],{env:{...process.env,OPENCLANK_HISTORY_TOKEN:'fixture-token'},stdio:['ignore','ignore','pipe']})
  let errors=''; service.stderr.on('data',chunk=>{if(errors.length<16384)errors+=String(chunk).slice(0,16384-errors.length)})
  const call=(value:unknown)=>new Promise<any>((resolve,reject)=>{
    const client=net.createConnection(socket);let data='';client.setEncoding('utf8');client.on('error',reject)
    client.on('connect',()=>client.end(JSON.stringify(value)+'\n'))
    client.on('data',chunk=>data+=chunk);client.on('end',()=>{try{resolve(JSON.parse(data))}catch(error){reject(error)}})
  })
  try {
    for(let i=0;i<300;i++){if(service.exitCode!==null||service.signalCode!==null)throw new Error(errors);if(await fs.stat(socket).then(()=>true,()=>false))break;if(i===299)throw new Error('service unavailable');await new Promise(resolve=>setTimeout(resolve,20))}
    const [{Effect,Layer},{NodeFileSystem},{AppFileSystem}]=await Promise.all([import('effect'),import('@effect/platform-node'),import('@mimo-ai/shared/filesystem')])
    const target=path.join(root,'example.txt');await fs.writeFile(target,'before')
    const origin={version:1 as const,host_chat_id:'chat',engine_session_id:'engine',root_operation_id:'root',actor_id:'main',execution_revision:1,inner_turn_id:'turn',tool_call_id:'call',assistant_message_id:'message'}
    const history:any={actorId:'fixture-actor',accountId:'fixture-account',workspaceId:'fixture-workspace',workspaceRoot:root,socketPath:socket,token:'fixture-token',managedToolOrigin:origin,status:{status:'paused',history_status:'paused',capture_phase:'unavailable',durable:false,coverage:'NoCapture'}}
    await Effect.runPromise(Effect.gen(function*(){const files=yield* AppFileSystem.Service;yield* files.atomicWrite({path:target,content:'after',actionId:'fixture-action',history})}).pipe(Effect.provide(AppFileSystem.layer.pipe(Layer.provide(NodeFileSystem.layer)))))
    const locator=history.status.managed_action_ref as string;expect(locator.startsWith('oc-history-action:v1:')).toBe(true)
    const value=JSON.parse(Buffer.from(locator.slice('oc-history-action:v1:'.length),'base64url').toString())
    await fs.writeFile(target,'later current content')
    const auth={actor_id:'fixture-human',account_id:'fixture-account',token:'fixture-token'}
    const envelope={protocol_version:1,auth,action_id:value.actionID}
    const request={envelope,resource_id:value.resourceID,before_version_id:value.beforeVersionID,after_version_id:value.afterVersionID,origin:value.origin}
    const response=await call({GetResourceChange:request});const change=response.ResourceChange
    expect(change?.action_id).toBe(value.actionID);expect(change?.before?.version_id).toBe(value.beforeVersionID);expect(change?.after?.version_id).toBe(value.afterVersionID)
    for(const [version,expected] of [[change.before,'before'],[change.after,'after']] as const){
      const result=await call({ReadResourceVersionChunk:{envelope,resource_id:value.resourceID,version_ref:version.id,offset:0,length:1024}})
      expect(Buffer.from(result.ResourceVersionChunk.content,'base64').toString()).toBe(expected)
    }
    expect((await call({GetResourceChange:{...request,before_version_id:'wrong'}})).Unavailable).toBeDefined()
    expect((await call({GetResourceChange:{...request,resource_id:'file:foreign'}})).Unavailable).toBeDefined()
    expect((await call({GetResourceChange:{...request,envelope:{...envelope,auth:{...auth,account_id:'foreign'}}}})).ResourceChange).toBeUndefined()
  } finally {
    if(service.exitCode===null&&service.signalCode===null)service.kill('SIGTERM')
    await new Promise<void>(resolve=>{if(service.exitCode!==null||service.signalCode!==null)return resolve();const timer=setTimeout(()=>service.kill('SIGKILL'),1000);service.once('exit',()=>{clearTimeout(timer);resolve()})})
    await fs.rm(root,{recursive:true,force:true})
  }
},30000)
