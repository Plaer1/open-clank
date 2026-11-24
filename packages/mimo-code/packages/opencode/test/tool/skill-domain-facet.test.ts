import {test,expect,mock} from 'bun:test'
import {Effect,Stream} from 'effect'
import {createHash} from 'node:crypto'
import {promises as fs} from 'node:fs'
import os from 'node:os'
import path from 'node:path'
const actual=await import('../../src/acp/managed-provider')
let ready=true,calls=0
mock.module('../../src/acp/managed-provider',()=>({...actual,enabled:()=>true,admitManagedSessionBinding:async()=>({owner:'alice'}),managedHostCall:async()=>{calls++;return {state:ready?'ready':'unavailable'}}}))
const {Skill}=await import('../../src/skill')
const {Ripgrep}=await import('../../src/file/ripgrep')
const {SkillTool}=await import('../../src/tool/skill')
const {Agent}=await import('../../src/agent/agent')
const Truncate=await import('../../src/tool/truncate')
const {Instance}=await import('../../src/project/instance')
test('qualified skill domain uses original resolution and existing current metadata load gate',async()=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'skill-domain-'))
 try {
  const file=path.join(dir,'SKILL.md'),body='---\nname: example\n---\nbody\n';await fs.writeFile(file,body);const stat=await fs.stat(file)
  const info={name:'example',description:'test',location:file,content:'body',sourceFileHash:createHash('sha256').update(body).digest('hex'),sourceFileDevice:stat.dev,sourceFileInode:stat.ino,metadataClaim:true,metadataReceiptSHA256:'a'.repeat(64),metadataSnapshotSHA256:'b'.repeat(64),skillID:'skill',revision:2,contentHash:'hash',status:'published',domainRefs:[{block_id:'domain',revision:3}]}
  const svc={getForInvocation:()=>Effect.succeed({ok:true,info}),reload:()=>Effect.void,all:()=>Effect.succeed([info])}
  const definition=await Effect.runPromise(SkillTool.pipe(Effect.provideService(Skill.Service,svc as any),Effect.provideService(Ripgrep.Service,{files:()=>Stream.empty} as any),Effect.provideService(Agent.Service,{get:()=>Effect.succeed({})} as any),Effect.provideService(Truncate.Service,{output:(content:string)=>Effect.succeed({content,truncated:false})} as any)))
  const tool=await Effect.runPromise(definition.init())
  const ctx={sessionID:'session',messageID:'message',callID:'call',agent:'build',messages:[],abort:new AbortController().signal,ask:()=>Effect.void,metadata:()=>Effect.void,extra:{actorExecution:{sessionID:'session',rootOperationID:'root',actorID:'main',executionRevision:1,innerTurnID:'turn'}}} as any
  await Instance.provide({directory:dir,fn:async()=>{
  const result=await Effect.runPromise(tool.execute({name:'example',domain:{block_id:'domain',revision:3}},ctx));expect(result.metadata.skillUse.domain).toEqual({block_id:'domain',revision:3});expect(calls).toBe(1)
  await expect(Effect.runPromise(tool.execute({name:'example',domain:{block_id:'domain',revision:4}},ctx))).rejects.toThrow();expect(calls).toBe(1)
  ready=false;await expect(Effect.runPromise(tool.execute({name:'example',domain:{block_id:'domain',revision:3}},ctx))).rejects.toThrow();expect(calls).toBe(2)
  const catalog=Skill.fmt([info],{verbose:true});expect(catalog).toContain('revalidated on load');expect(catalog).toContain('domain')
  }})
 } finally {await fs.rm(dir,{recursive:true,force:true})}
})
