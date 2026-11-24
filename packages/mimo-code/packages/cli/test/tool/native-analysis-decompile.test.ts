import { test, expect, mock } from 'bun:test'
import { Effect } from 'effect'
import { createHash } from 'node:crypto'
import { promises as fs } from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import * as Spawner from '../../src/effect/cross-spawn-spawner'
const source=Buffer.from('benign-source\n'),sha=createHash('sha256').update(source).digest('hex')
const revision={kind:'hostFingerprint',value:`sha256:${sha}:${source.length}`}
const selector={name:'sample',address:3,size:8,addressSpace:'provider_virtual' as const}
const target={dossier_id:'first',expected_dossier_revision:revision,selector}
const verified={dossierID:'first',dossierRevision:revision,selector,resultSHA256:'c'.repeat(64)}
let captured:any
const actual=await import('../../src/acp/managed-provider')
mock.module('../../src/acp/managed-provider',()=>({...actual,managedHostCall:async (_method:string,r:any)=>{if(r.operation==='eligibility')return {state:'eligible'};if(r.operation==='capture'){captured=r;return {state:'captured',captureToken:'a'.repeat(64),sourceSHA256:sha,revision,size:source.length,bytesBase64:source.toString('base64'),functionTarget:verified}};return r.operation==='stage'?{state:'staged',provisional:'sealed'}:{state:'current'}}}))
const {nativeAnalysisInit}=await import('../../src/tool/native-analysis')
test('configured fixed partial decompile, framing, target and measured profile',async()=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'ma3-fake-decompile-')),old={...process.env}
 try{
  const exe=path.join(dir,'rizin'),plugin=path.join(dir,'plugin.so'),root=path.join(dir,'sleigh');await fs.writeFile(plugin,'fake library');await fs.mkdir(root)
  process.env.OPEN_CLANK_RIZIN_EXECUTABLE=exe;process.env.OPEN_CLANK_RIZIN_VERSION='0.8.1';process.env.OPEN_CLANK_RIZIN_GHIDRA_LIBRARY=plugin;process.env.OPEN_CLANK_RIZIN_GHIDRA_SHA256=createHash('sha256').update(await fs.readFile(plugin)).digest('hex');process.env.OPEN_CLANK_RIZIN_SLEIGH_HOME=root
  delete process.env.OPEN_CLANK_RIZIN_SLEIGH_MANIFEST;delete process.env.OPEN_CLANK_RIZIN_SLEIGH_MANIFEST_SHA256
  const def=await Effect.runPromise(Effect.provide(nativeAnalysisInit,Spawner.defaultLayer))
  const ctx={sessionID:'session',callID:'call',messageID:'message',agent:'build',abort:new AbortController().signal,messages:[],metadata:()=>Effect.void,ask:()=>Effect.void,extra:{actorExecution:{rootOperationID:'root',actorID:'main',innerTurnID:'turn',executionRevision:1}}} as any
  const args={source_ref:'opaque-ref',revision,operation:'decompile' as const,max_functions:128,max_strings:128,decompile_target:target}
  async function configure(out:string,extra=''){await fs.writeFile(exe,`#!/bin/sh\n[ "$1" = '-NN' ] && [ "$2" = '-l' ] && [ "$3" = '${plugin}' ] && [ "$SLEIGHHOME" = '${root}' ] && [ "$6" = 'aa;afij @ 0x3;pdgj @ 0x3;afij @ 0x3' ] || exit 9\n${extra}\ncat <<'RESULT'\n${out}\nRESULT\n`,{mode:0o700});process.env.OPEN_CLANK_RIZIN_SHA256=createHash('sha256').update(await fs.readFile(exe)).digest('hex')}
  const row='[{"offset":3,"name":"sample","size":8}]',code='é'+('x'.repeat(4200))
  await configure(`${row}\n${JSON.stringify({code,annotations:[]})}\n${row}`)
  const value=JSON.parse((await Effect.runPromise(def.execute(args,ctx))).output)
  expect(value.decompile).toBe(code);expect(value.completion).toBe('unknown');expect(value.profileEvidence.dependencies).toBe('unverified');expect(captured.decompileProfile.pluginSHA256).toBe(process.env.OPEN_CLANK_RIZIN_GHIDRA_SHA256)
  for(const out of [`${row}\n{"errors":["failed"]}\n${row}`,`${row}\n{"code":"x","annotations":[]}\n[]`,`${row}\n{"code":"x","annotations":[]}\n${row}\nnoise`]){await configure(out);await expect(Effect.runPromise(def.execute(args,ctx))).rejects.toThrow()}
  await configure(`${row}\n{"code":"x","annotations":[]}\n${row}`,`printf x >> '${plugin}'`);await expect(Effect.runPromise(def.execute(args,ctx))).rejects.toThrow()
 }finally{process.env=old;await fs.rm(dir,{recursive:true,force:true})}
})
