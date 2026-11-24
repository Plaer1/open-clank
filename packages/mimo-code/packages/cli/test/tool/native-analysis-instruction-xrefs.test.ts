import { test,expect,mock } from 'bun:test'
import { Effect } from 'effect'
import { createHash } from 'node:crypto'
import { promises as fs } from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import * as Spawner from '../../src/effect/cross-spawn-spawner'
const source=Buffer.from('benign-source\n'),sha=createHash('sha256').update(source).digest('hex')
const revision={kind:'hostFingerprint',value:`sha256:${sha}:${source.length}`},selector={address:3,size:2,opcode:'mov eax, ebx',addressSpace:'provider_virtual' as const}
const target={dossier_id:'sample',expected_dossier_revision:revision,selector,max_references:2},verified={dossierID:'sample',dossierRevision:revision,instruction:selector,maxReferences:2,resultSHA256:'c'.repeat(64)}
const actual=await import('../../src/acp/managed-provider')
mock.module('../../src/acp/managed-provider',()=>({...actual,managedHostCall:async (_:string,r:any)=>r.operation==='eligibility'?{state:'eligible'}:r.operation==='capture'?{state:'captured',captureToken:'a'.repeat(64),sourceSHA256:sha,revision,size:source.length,bytesBase64:source.toString('base64'),instructionTarget:verified}:r.operation==='stage'?{state:'staged',provisional:'sealed'}:{state:'current'}}))
const {nativeAnalysisInit}=await import('../../src/tool/native-analysis')
test('one verified instruction, fixed command and outgoing partial references',async()=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'ma3-insn-xrefs-')),old={...process.env}
 try{
  const exe=path.join(dir,'fake-rizin');process.env.OPEN_CLANK_RIZIN_EXECUTABLE=exe;process.env.OPEN_CLANK_RIZIN_VERSION='0.8.1'
  async function configure(output:string){await fs.writeFile(exe,`#!/bin/sh\n[ "$4" = 'aa;pdj 1 @ 0x3;axfj @ 0x3;pdj 1 @ 0x3' ] || exit 9\ncat <<'RESULT'\n${output}\nRESULT\n`,{mode:0o700});process.env.OPEN_CLANK_RIZIN_SHA256=createHash('sha256').update(await fs.readFile(exe)).digest('hex')}
  const def=await Effect.runPromise(Effect.provide(nativeAnalysisInit,Spawner.defaultLayer))
  const ctx={sessionID:'session',callID:'call',messageID:'message',agent:'build',abort:new AbortController().signal,messages:[],metadata:()=>Effect.void,ask:()=>Effect.void,extra:{actorExecution:{rootOperationID:'root',actorID:'main',innerTurnID:'turn',executionRevision:1}}} as any
  const args={source_ref:'opaque-ref',revision,operation:'xrefs_from_instruction' as const,max_functions:128,max_strings:128,instruction_target:target}
  const row='[{"offset":3,"size":2,"opcode":"mov eax, ebx"}]',body='[{"from":3,"to":7,"type":"CALL"},{"from":3,"to":7,"type":"CALL"},{"from":3,"to":9,"type":"CODE"}]'
  await configure(`${row}\n${body}\n${row}`);let value=JSON.parse((await Effect.runPromise(def.execute(args,ctx))).output)
  expect(value.direction).toBe('outgoing_from_instruction');expect(value.xrefs_from_instruction).toHaveLength(2);expect(value.omitted).toBe(1);expect(value.xrefs_from_instruction[0].from).toBe(3)
  await configure(`${row}\n[]\n${row}`);value=JSON.parse((await Effect.runPromise(def.execute(args,ctx))).output);expect(value.xrefs_from_instruction).toEqual([])
  for(const output of [`${row}\n[{"from":4,"to":7,"type":"CALL"}]\n${row}`,`${row}\n[]\n[{"offset":3,"size":2,"opcode":"changed"}]`,`${row}\n[{"from":3,"to":7.5,"type":"CALL"}]\n${row}`,`${row}\n[]\n${row}\nnoise`]){await configure(output);await expect(Effect.runPromise(def.execute(args,ctx))).rejects.toThrow()}
 }finally{process.env=old;await fs.rm(dir,{recursive:true,force:true})}
})
