import { test, expect, mock } from 'bun:test'
import { Effect, Fiber } from 'effect'
import { createHash } from 'node:crypto'
import { promises as fs } from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import * as Spawner from '../../src/effect/cross-spawn-spawner'

const source=Buffer.from('benign-source\n'), sha=createHash('sha256').update(source).digest('hex')
let current=true, finalized=0
const actual=await import('../../src/acp/managed-provider')
mock.module('../../src/acp/managed-provider',()=>({...actual,managedHostCall:async (_method:string,request:any)=>{
  if(request.operation==='capture') return {state:'captured',captureToken:'a'.repeat(64),sourceSHA256:sha,revision:{kind:'v',value:'1'},size:source.length,bytesBase64:source.toString('base64')}
  finalized++; return {state:current?'current':'unavailable'}
}}))
const {nativeAnalysisInit}=await import('../../src/tool/native-analysis')

test('configured disposable process, source evidence, stale and cancel',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'ma1-test-'))
  const old={...process.env}
  try {
    const exe=path.join(dir,'fake-rizin')
    async function configure(body:string) {
      await fs.writeFile(exe,`#!/bin/sh\n${body}\n`,{mode:0o700})
      process.env.OPEN_CLANK_RIZIN_EXECUTABLE=exe
      process.env.OPEN_CLANK_RIZIN_VERSION='0.8.1'
      process.env.OPEN_CLANK_RIZIN_SHA256=createHash('sha256').update(await fs.readFile(exe)).digest('hex')
    }
    const def=await Effect.runPromise(Effect.provide(nativeAnalysisInit,Spawner.defaultLayer))
    const args={source_ref:'opaque-ref',revision:{kind:'v',value:'1'},max_functions:1}
    const context=(abort:AbortSignal)=>({sessionID:'session',callID:'call',messageID:'message',agent:'build',abort,messages:[],metadata:()=>Effect.void,ask:()=>Effect.void,extra:{actorExecution:{rootOperationID:'root',actorID:'actor',innerTurnID:'turn',executionRevision:1}}} as any)
    await configure('read line < "$5"; [ "$line" = "benign-source" ] || exit 2; printf \'[{"name":"a","offset":16,"size":4},{"name":"b","offset":32,"size":4}]\'')
    const result=await Effect.runPromise(def.execute(args,context(new AbortController().signal)))
    const parsed=JSON.parse(result.output); expect(parsed.sourceSHA256).toBe(sha); expect(parsed.functions.length).toBe(1); expect(parsed.omitted).toBe(1)
    current=false
    await expect(Effect.runPromise(def.execute(args,context(new AbortController().signal)))).rejects.toThrow()
    current=true
    await configure('exec /bin/sleep 30')
    const controller=new AbortController(); setTimeout(()=>controller.abort(),100)
    await expect(Effect.runPromise(def.execute(args,context(controller.signal)))).rejects.toThrow()
    expect(finalized).toBe(2)
    const marker=path.join(dir,'child-marker')
    await configure(`printf '%s\\n%s' "$5" "$$" > '${marker}'; exec /bin/sleep 30`)
    const fiber=Effect.runFork(def.execute(args,context(new AbortController().signal)))
    for(let i=0;i<200;i++){try{await fs.access(marker);break}catch{await Bun.sleep(10)}}
    const [staged,pid]=String(await fs.readFile(marker)).split('\n')
    await Effect.runPromise(Fiber.interrupt(fiber))
    expect(()=>process.kill(Number(pid),0)).toThrow()
    await fs.rm(path.dirname(staged!),{recursive:true,force:true})
  } finally { process.env=old; await fs.rm(dir,{recursive:true,force:true}) }
})
