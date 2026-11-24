import { test, expect, mock } from 'bun:test'
import { Effect } from 'effect'
import { createHash } from 'node:crypto'
import { promises as fs } from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import * as Spawner from '../../src/effect/cross-spawn-spawner'
const source=Buffer.from('benign-source\n'), sha=createHash('sha256').update(source).digest('hex')
const revision={kind:'hostFingerprint',value:`sha256:${sha}:${source.length}`}
const selector={name:'sample',address:3,size:8,addressSpace:'provider_virtual' as const}
const target={dossier_id:'first',expected_dossier_revision:revision,selector,max_instructions:2}
const verified={dossierID:'first',dossierRevision:revision,selector,maxInstructions:2,resultSHA256:'c'.repeat(64)}
const actual=await import('../../src/acp/managed-provider')
mock.module('../../src/acp/managed-provider',()=>({...actual,managedHostCall:async (_method:string,r:any)=>r.operation==='eligibility'?{state:'eligible'}:r.operation==='capture'?{state:'captured',captureToken:'a'.repeat(64),sourceSHA256:sha,revision,size:source.length,bytesBase64:source.toString('base64'),functionTarget:verified}:r.operation==='stage'?{state:'staged',provisional:'sealed'}:{state:'current'}}))
const {nativeAnalysisInit}=await import('../../src/tool/native-analysis')
test('bounded verified-entry command and lexical integer refusal',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'ma3-fake-provider-')),old={...process.env}
  try {
    const exe=path.join(dir,'fake-rizin')
    async function configure(json:string){await fs.writeFile(exe,`#!/bin/sh\n[ "$4" = 'pdj 2 @ 0x3' ] || exit 9\nprintf '%s' '${json}'\n`,{mode:0o700});process.env.OPEN_CLANK_RIZIN_EXECUTABLE=exe;process.env.OPEN_CLANK_RIZIN_VERSION='0.8.1';process.env.OPEN_CLANK_RIZIN_SHA256=createHash('sha256').update(await fs.readFile(exe)).digest('hex')}
    const def=await Effect.runPromise(Effect.provide(nativeAnalysisInit,Spawner.defaultLayer))
    const ctx={sessionID:'session',callID:'call',messageID:'message',agent:'build',abort:new AbortController().signal,messages:[],metadata:()=>Effect.void,ask:()=>Effect.void,extra:{actorExecution:{rootOperationID:'root',actorID:'main',innerTurnID:'turn',executionRevision:1}}} as any
    const args={source_ref:'opaque-ref',revision,operation:'disassembly' as const,max_functions:128,max_strings:128,function_target:target}
    await configure('[{"offset":3,"size":1,"opcode":"nop"},{"offset":11,"size":1,"opcode":"outside"}]')
    const output=JSON.parse((await Effect.runPromise(def.execute(args,ctx))).output)
    expect(output.disassembly).toEqual([{address:3,size:1,opcode:'nop',addressSpace:'provider_virtual'}]);expect(output.omitted).toBe(1);expect(output.functionTarget).toEqual(verified)
    await configure('[{"offset":3.1,"size":1,"opcode":"bad"}]')
    await expect(Effect.runPromise(def.execute(args,ctx))).rejects.toThrow()
  } finally {process.env=old;await fs.rm(dir,{recursive:true,force:true})}
})
