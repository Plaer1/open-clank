import { test, expect, mock } from 'bun:test'
import { Effect } from 'effect'
import { createHash } from 'node:crypto'
import { promises as fs } from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import * as Spawner from '../../src/effect/cross-spawn-spawner'

const source=Buffer.from('benign-source\n'), sha=createHash('sha256').update(source).digest('hex')
let current=true
const actual=await import('../../src/acp/managed-provider')
mock.module('../../src/acp/managed-provider',()=>({...actual,managedHostCall:async (_method:string,request:any)=>request.operation==='capture'
  ? {state:'captured',captureToken:'a'.repeat(64),sourceSHA256:sha,revision:{kind:'v',value:'1'},size:source.length,bytesBase64:source.toString('base64')}
  : {state:current?'current':'unavailable'}}))
const {nativeAnalysisInit}=await import('../../src/tool/native-analysis')

test('fixed info and strings project exact bounded provider evidence',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'ma1-info-test-')), old={...process.env}
  try {
    const exe=path.join(dir,'fake-rizin')
    async function configure(expected:string, output:string, raw=false) {
      const body=raw ? output : `printf '%s' '${output.replaceAll("'", "'\\''")}'`
      await fs.writeFile(exe,`#!/bin/sh\n[ "$4" = '${expected}' ] || exit 9\n${body}\n`,{mode:0o700})
      process.env.OPEN_CLANK_RIZIN_EXECUTABLE=exe; process.env.OPEN_CLANK_RIZIN_VERSION='0.8.1'
      process.env.OPEN_CLANK_RIZIN_SHA256=createHash('sha256').update(await fs.readFile(exe)).digest('hex')
    }
    const def=await Effect.runPromise(Effect.provide(nativeAnalysisInit,Spawner.defaultLayer))
    const context={sessionID:'session',callID:'call',messageID:'message',agent:'build',abort:new AbortController().signal,messages:[],metadata:()=>Effect.void,ask:()=>Effect.void,extra:{actorExecution:{rootOperationID:'root',actorID:'actor',innerTurnID:'turn',executionRevision:1}}} as any
    const base={source_ref:'opaque-ref',revision:{kind:'v',value:'1'},max_functions:1}
    await configure('aa;aflj','[{"name":"f","offset":3,"size":2}]')
    expect(JSON.parse((await Effect.runPromise(def.execute(base,context))).output).functions[0].address).toBe(3)
    await configure('ij','{"core":{"file":"secret-stage"},"bin":{"arch":"x86","bits":64,"endian":"LE","baddr":9007199254740991.1}}')
    const info=JSON.parse((await Effect.runPromise(def.execute({...base,operation:'info'},context))).output)
    expect(info.info.baseAddress).toBeNull(); expect(info.info.bits).toBe(64); expect(JSON.stringify(info)).not.toContain('secret-stage')
    await configure('ij','{"core":{}}')
    expect(JSON.parse((await Effect.runPromise(def.execute({...base,operation:'info'},context))).output).info.architecture).toBeNull()
    const row='{"string":"abc","type":"ascii","section":"data","size":3,"length":3,"paddr":1,"vaddr":9007199254740993}'
    await configure('izj',`[${row},${row}]`)
    const strings=JSON.parse((await Effect.runPromise(def.execute({...base,operation:'strings',max_strings:1},context))).output)
    expect(strings.strings[0].physicalAddress).toBe(1); expect(strings.strings[0].virtualAddress).toBeNull(); expect(strings.omitted).toBe(1)
    const long=JSON.stringify(Array.from({length:128},()=>({string:'x'.repeat(900),type:'ascii',section:'',size:1,length:1,paddr:1,vaddr:1})))
    await configure('izj',long)
    const capped=(await Effect.runPromise(def.execute({...base,operation:'strings'},context))).output
    expect(Buffer.byteLength(capped)).toBeLessThanOrEqual(50*1024); expect(JSON.parse(capped).omitted).toBeGreaterThan(0)
    current=false
    await expect(Effect.runPromise(def.execute({...base,operation:'strings'},context))).rejects.toThrow()
    current=true
    await configure('izj',"printf '\\377'",true)
    await expect(Effect.runPromise(def.execute({...base,operation:'strings'},context))).rejects.toThrow()
  } finally {process.env=old; await fs.rm(dir,{recursive:true,force:true})}
})

test('string controls round-trip without active controls in tool output',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'ma1-string-test-')), old={...process.env}
  try {
    const exe=path.join(dir,'fake-rizin'), text='line\n\t\x1b\u202eend'
    const provider=JSON.stringify([{string:text,type:'ascii',section:'',size:1,length:1,paddr:1,vaddr:1}])
    await fs.writeFile(exe,`#!/bin/sh\n[ "$4" = 'izj' ] || exit 9\nprintf '%s' '${provider}'\n`,{mode:0o700})
    process.env.OPEN_CLANK_RIZIN_EXECUTABLE=exe; process.env.OPEN_CLANK_RIZIN_VERSION='0.8.1'
    process.env.OPEN_CLANK_RIZIN_SHA256=createHash('sha256').update(await fs.readFile(exe)).digest('hex')
    const def=await Effect.runPromise(Effect.provide(nativeAnalysisInit,Spawner.defaultLayer))
    const context={sessionID:'session',callID:'call',messageID:'message',agent:'build',abort:new AbortController().signal,messages:[],metadata:()=>Effect.void,ask:()=>Effect.void,extra:{actorExecution:{rootOperationID:'root',actorID:'actor',innerTurnID:'turn',executionRevision:1}}} as any
    const output=(await Effect.runPromise(def.execute({source_ref:'opaque-ref',revision:{kind:'v',value:'1'},max_functions:1,operation:'strings'},context))).output
    expect(JSON.parse(output).strings[0].text).toBe(text)
    expect(output).not.toMatch(/[\n\t\x1b\u202e]/)
    expect(Buffer.byteLength(output)).toBeLessThanOrEqual(50*1024)
  } finally {process.env=old; await fs.rm(dir,{recursive:true,force:true})}
})
