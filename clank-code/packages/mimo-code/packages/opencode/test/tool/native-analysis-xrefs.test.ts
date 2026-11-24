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
const target={dossier_id:'first',expected_dossier_revision:revision,selector,max_references:2}
const verified={dossierID:'first',dossierRevision:revision,selector,maxReferences:2,resultSHA256:'c'.repeat(64)}
const actual=await import('../../src/acp/managed-provider')
mock.module('../../src/acp/managed-provider',()=>({...actual,managedHostCall:async (_method:string,r:any)=>r.operation==='eligibility'?{state:'eligible'}:r.operation==='capture'?{state:'captured',captureToken:'a'.repeat(64),sourceSHA256:sha,revision,size:source.length,bytesBase64:source.toString('base64'),functionTarget:verified}:r.operation==='stage'?{state:'staged',provisional:'sealed'}:{state:'current'}}))
const {nativeAnalysisInit}=await import('../../src/tool/native-analysis')
test('fixed incoming-entry command, empty, limits and malformed refusal',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'ma3-fake-provider-')),old={...process.env}
  try {
    const exe=path.join(dir,'fake-rizin')
    async function configure(json:string){await fs.writeFile(exe,`#!/bin/sh\n[ "$4" = 'aa;axtj @ 0x3' ] || exit 9\nprintf '%s' '${json}'\n`,{mode:0o700});process.env.OPEN_CLANK_RIZIN_EXECUTABLE=exe;process.env.OPEN_CLANK_RIZIN_VERSION='0.8.1';process.env.OPEN_CLANK_RIZIN_SHA256=createHash('sha256').update(await fs.readFile(exe)).digest('hex')}
    const def=await Effect.runPromise(Effect.provide(nativeAnalysisInit,Spawner.defaultLayer))
    const ctx={sessionID:'session',callID:'call',messageID:'message',agent:'build',abort:new AbortController().signal,messages:[],metadata:()=>Effect.void,ask:()=>Effect.void,extra:{actorExecution:{rootOperationID:'root',actorID:'main',innerTurnID:'turn',executionRevision:1}}} as any
    const args={source_ref:'opaque-ref',revision,operation:'xrefs_to_entry' as const,max_functions:128,max_strings:128,xref_target:target}
    await configure('[]')
    let output=JSON.parse((await Effect.runPromise(def.execute(args,ctx))).output)
    expect(output.xrefs_to_entry).toEqual([]);expect(output.omitted).toBe(0)
    await configure('[{"from":7,"to":3,"type":"CALL"},{"from":7,"to":3,"type":"CALL"},{"from":9,"to":3,"type":"CODE"}]')
    output=JSON.parse((await Effect.runPromise(def.execute(args,ctx))).output)
    expect(output.xrefs_to_entry).toEqual([{from:7,to:3,type:'CALL',addressSpace:'provider_virtual'},{from:7,to:3,type:'CALL',addressSpace:'provider_virtual'}]);expect(output.omitted).toBe(1)
    for(const invalid of ['[{"from":3.1,"to":3,"type":"CALL"}]','[{"from":3,"to":4,"type":"CALL"}]','[{"from":3,"to":3,"type":""}]']) {await configure(invalid);await expect(Effect.runPromise(def.execute(args,ctx))).rejects.toThrow()}

  } finally {process.env=old;await fs.rm(dir,{recursive:true,force:true})}
})
