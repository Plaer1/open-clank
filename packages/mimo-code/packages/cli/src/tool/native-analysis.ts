import { ChildProcess, ChildProcessSpawner } from 'effect/unstable/process'
import { Effect, Fiber, Stream } from 'effect'
import { createHash } from 'node:crypto'
import { promises as fs } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import z from 'zod'
import * as Tool from './tool'
import { managedHostCall } from '@/acp/managed-provider'
import { nativeAnalysisInputSHA, decompileConfiguredProfile } from './native-analysis-input'
import { activeAnalysisProfile, profileResultIdentity } from './native-analysis-profile'
// Rizin adapter: command construction, output parsing and 'Rizin' wording below drive the Rizin binary; result identity comes from the profile.

const MAX_OUTPUT = 2 * 1024 * 1024
const MAX_PROVIDER = 128 * 1024 * 1024
const MAX_RESULT = 50 * 1024

type JsonInteger = { integer: number | null }
function parseProviderJson(bytes: Buffer): unknown {
  const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  const parse = JSON.parse as unknown as (text: string, reviver: (key: string, value: unknown, context?: { source?: string }) => unknown) => unknown
  return parse(text, (_key, value, context) => {
    if (typeof value !== 'number') return value
    const source = context?.source
    return { integer: typeof source === 'string' && /^(?:0|[1-9][0-9]*)$/.test(source) && Number.isSafeInteger(value) && value >= 0 ? value : null } satisfies JsonInteger
  })
}
function parseThreeDocuments(bytes:Buffer):unknown[] {
  const text=new TextDecoder('utf-8',{fatal:true}).decode(bytes),docs:unknown[]=[];let i=0
  while(i<text.length){while(i<text.length&&/[\t\n\r ]/.test(text[i]!))i++;if(i===text.length)break
    const start=i,stack:string[]=[];let quoted=false,escaped=false
    for(;i<text.length;i++){const c=text[i]!;if(quoted){if(escaped)escaped=false;else if(c==='\\')escaped=true;else if(c==='"')quoted=false;continue}
      if(c==='"'){quoted=true;continue}if(c==='{'||c==='['){stack.push(c);continue}if(c==='}'||c===']'){const top=stack.pop();if(!top||(top==='{'?c!=='}':c!==']'))throw new Error('Invalid provider framing.');if(!stack.length){i++;break}continue}if(!stack.length)throw new Error('Invalid provider framing.')}
    if(quoted||stack.length||i===start)throw new Error('Invalid provider framing.')
    docs.push(parseProviderJson(Buffer.from(text.slice(start,i),'utf8')));if(docs.length>3)throw new Error('Invalid provider framing.')
  }
  if(docs.length!==3)throw new Error('Invalid provider framing.');return docs
}
function integer(value: unknown): number | null {
  if (!value || typeof value !== 'object' || !('integer' in value)) return null
  const n = (value as JsonInteger).integer
  return typeof n === 'number' && Number.isSafeInteger(n) && n >= 0 ? n : null
}
function safeText(value: unknown, maximum: number): string | null {
  if (value == null) return null
  if (typeof value !== 'string' || /[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]|[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/u.test(value) || Buffer.byteLength(value, 'utf8') > maximum) throw new Error('Rizin returned unsupported text.')
  return value
}
function safeContent(value: unknown, maximum: number): string | null {
  if (value == null) return null
  if (typeof value !== 'string' || /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/u.test(value) || Buffer.byteLength(value, 'utf8') > maximum) throw new Error('Rizin returned unsupported text.')
  return value
}
function renderJson(value: unknown): string {
  return JSON.stringify(value).replace(/[\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u206f]/g, character => `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`)
}
function boundedResult(base: Record<string, unknown>, key?: string, rows?: unknown[]): string {
  if (key && rows) {
    const kept = rows.slice()
    for (;;) {
      const output = renderJson({ ...base, [key]: kept, omitted: (base.omitted as number) + rows.length - kept.length })
      if (Buffer.byteLength(output, 'utf8') <= MAX_RESULT) return output
      if (!kept.length) throw new Error('Rizin result exceeds its output limit.')
      kept.pop()
    }
  }
  const output = renderJson(base)
  if (Buffer.byteLength(output, 'utf8') > MAX_RESULT) throw new Error('Rizin result exceeds its output limit.')
  return output
}

async function boundedFileIdentity(file:string, limit:number, signal:AbortSignal) {
  const handle=await fs.open(file,'r')
  try {
    const before=await handle.stat()
    if(!before.isFile() || before.size>limit) throw new Error('File exceeds bounds')
    const hash=createHash('sha256'), chunk=Buffer.alloc(65536); let total=0
    while(true) {
      if(signal.aborted) throw new Error('Cancelled')
      const {bytesRead}=await handle.read(chunk,0,Math.min(chunk.length,limit-total+1),null)
      if(!bytesRead) break
      total+=bytesRead; if(total>limit) throw new Error('File exceeds bounds')
      hash.update(chunk.subarray(0,bytesRead))
    }
    const after=await handle.stat()
    if(total!==before.size || before.dev!==after.dev || before.ino!==after.ino || before.size!==after.size || before.mtimeMs!==after.mtimeMs) throw new Error('File changed')
    return {sha:hash.digest('hex'),dev:before.dev,ino:before.ino,size:before.size,mtime:before.mtimeMs}
  } finally {await handle.close()}
}

/** A fixed command and bounded output; no caller-supplied shell, path or Rizin expression. */
export const nativeAnalysisInit = Effect.gen(function* () {
  const spawner = yield* ChildProcessSpawner.ChildProcessSpawner
  return {
    description:'Read-only functions, file info, strings, a bounded linear entry sample, incoming entry references, outgoing references from a verified instruction, or configured partial C-like decompilation of a verified saved function, using a configured Rizin 0.8.1 provider for one exact Files resource (maximum source 16 MiB). Results are partial provider evidence; decompilation completion is unknown.',
    parameters:z.object({source_ref:z.string().min(8).max(16384),revision:z.object({kind:z.string().min(1).max(64),value:z.string().min(1).max(256)}).strict(),operation:z.enum(['functions','info','strings','disassembly','xrefs_to_entry','decompile','xrefs_from_instruction']).default('functions'),max_functions:z.number().int().min(1).max(256).default(128),max_strings:z.number().int().min(1).max(256).default(128),function_target:z.object({dossier_id:z.string(),expected_dossier_revision:z.object({kind:z.string(),value:z.string()}).strict(),selector:z.object({name:z.string(),address:z.number(),size:z.number(),addressSpace:z.literal('provider_virtual')}).strict(),max_instructions:z.number().int().min(1).max(128).default(32)}).strict().optional(),xref_target:z.object({dossier_id:z.string(),expected_dossier_revision:z.object({kind:z.string(),value:z.string()}).strict(),selector:z.object({name:z.string(),address:z.number(),size:z.number(),addressSpace:z.literal('provider_virtual')}).strict(),max_references:z.number().int().min(1).max(128).default(64)}).strict().optional(),decompile_target:z.object({dossier_id:z.string(),expected_dossier_revision:z.object({kind:z.string(),value:z.string()}).strict(),selector:z.object({name:z.string(),address:z.number(),size:z.number(),addressSpace:z.literal('provider_virtual')}).strict()}).strict().optional(),instruction_target:z.object({dossier_id:z.string(),expected_dossier_revision:z.object({kind:z.string(),value:z.string()}).strict(),selector:z.object({address:z.number(),size:z.number(),opcode:z.string(),addressSpace:z.literal('provider_virtual')}).strict(),max_references:z.number().int().min(1).max(128).default(64)}).strict().optional()}).strict(),
    execute:(args:{source_ref:string;revision:{kind:string;value:string};operation?:'functions'|'info'|'strings'|'disassembly'|'xrefs_to_entry'|'decompile'|'xrefs_from_instruction';max_functions:number;max_strings?:number;function_target?:{dossier_id:string;expected_dossier_revision:{kind:string;value:string};selector:{name:string;address:number;size:number;addressSpace:'provider_virtual'};max_instructions:number};xref_target?:{dossier_id:string;expected_dossier_revision:{kind:string;value:string};selector:{name:string;address:number;size:number;addressSpace:'provider_virtual'};max_references:number};decompile_target?:{dossier_id:string;expected_dossier_revision:{kind:string;value:string};selector:{name:string;address:number;size:number;addressSpace:'provider_virtual'}};instruction_target?:{dossier_id:string;expected_dossier_revision:{kind:string;value:string};selector:{address:number;size:number;opcode:string;addressSpace:'provider_virtual'};max_references:number}},ctx:Tool.Context)=>Effect.gen(function* () {
      const profile=activeAnalysisProfile()
      const executable=profile ? process.env[profile.env.executable] || '' : ''
      const expectedHash=profile ? process.env[profile.env.sha256] || '' : ''
      if (!profile || !path.isAbsolute(executable)) throw new Error('Supported Rizin provider is not configured.')
      const actor=ctx.extra?.actorExecution as Record<string,unknown> | undefined
      if (!actor || !ctx.callID || typeof actor.rootOperationID !== 'string' || typeof actor.actorID !== 'string' || typeof actor.innerTurnID !== 'string' || !Number.isSafeInteger(actor.executionRevision)) throw new Error('Exact managed turn is unavailable.')
      const identity={sessionID:ctx.sessionID,rootOperationID:actor.rootOperationID,actorID:actor.actorID,executionRevision:actor.executionRevision as number,innerTurnID:actor.innerTurnID,callID:ctx.callID}
      const operation=args.operation ?? 'functions', maxStrings=args.max_strings ?? 128
      const decompileProfile=operation==='decompile'?decompileConfiguredProfile(process.env,profile):undefined
      const pluginPath=process.env[profile.env.ghidraLibrary] || '',sleighHome=process.env[profile.env.sleighHome] || '',manifestPath=process.env[profile.env.sleighManifest] || ''
      if(operation==='decompile'&&!decompileProfile)throw new Error('Configured decompiler is unavailable.')
      const inputSHA256=nativeAnalysisInputSHA(args)
      if (!inputSHA256) throw new Error('Analysis input unavailable.')
      yield* ctx.ask({permission:'bash',patterns:[profile.permission],always:[profile.permission],metadata:{operation:`read-only Rizin ${args.operation ?? 'functions'} analysis`}})
      if (ctx.abort.aborted) throw new Error('Analysis cancelled.')
      const eligibility=yield* Effect.tryPromise({try:()=>managedHostCall('_openclank/session/v1/native-analysis/source',{...identity,operation:'eligibility',messageID:ctx.messageID}),catch:()=>new Error('Analysis eligibility unavailable.')})
      if (eligibility.state !== 'eligible' && eligibility.state !== 'legacy') throw new Error('Analysis eligibility unavailable.')
      const qualified=eligibility.state === 'eligible'
      if ((operation==='disassembly'||operation==='xrefs_to_entry'||operation==='decompile'||operation==='xrefs_from_instruction') && !qualified) throw new Error('Verified main-turn analysis is unavailable.')
      const captured=yield* Effect.tryPromise({try:()=>managedHostCall('_openclank/session/v1/native-analysis/source',qualified ? {...identity,operation:'capture',sourceRef:args.source_ref,expectedRevision:args.revision,messageID:ctx.messageID,inputSHA256,analysisOperation:operation,maxFunctions:args.max_functions,maxStrings,...(args.function_target?{functionTarget:args.function_target}:{}),...(args.xref_target?{xrefTarget:args.xref_target}:{}),...(operation==='decompile'?{decompileTarget:args.decompile_target!,decompileProfile:decompileConfiguredProfile(process.env,profile)!}:{}),...(args.instruction_target?{instructionTarget:args.instruction_target}:{})} : {...identity,operation:'capture',sourceRef:args.source_ref,expectedRevision:args.revision}),catch:()=>new Error('Source capture unavailable.')})
      if (captured.state !== 'captured' || ctx.abort.aborted) throw new Error('Source capture unavailable or cancelled.')
      const bytes=Buffer.from(captured.bytesBase64,'base64')
      if (bytes.length!==captured.size || createHash('sha256').update(bytes).digest('hex')!==captured.sourceSHA256) throw new Error('Source capture did not match its receipt.')
      let mayHaveSpawned=false, settled=false
      const run=Effect.scoped(Effect.gen(function* () {
        const dir=yield* Effect.acquireRelease(
          Effect.tryPromise({try:()=>fs.mkdtemp(path.join(os.tmpdir(),'openclank-analysis-')),catch:()=>new Error('Private staging unavailable.')}),
          owned=>Effect.promise(()=>!mayHaveSpawned || settled ? fs.rm(owned,{recursive:true,force:true}) : Promise.resolve()),
        )
        const input=path.join(dir,'input.bin')
        const providerIdentity=yield* Effect.uninterruptible(Effect.tryPromise({try:async()=>{if(ctx.abort.aborted)throw new Error();await fs.chmod(dir,0o700); await fs.writeFile(input,bytes,{mode:0o600,flag:'wx'}); const identity=await boundedFileIdentity(executable,MAX_PROVIDER,ctx.abort); if(identity.sha!==expectedHash || ctx.abort.aborted) throw new Error(); return identity},catch:()=>new Error('Configured Rizin identity is unavailable or cancelled.')}))
        const rootHandle=operation==='decompile' ? yield* Effect.acquireRelease(Effect.tryPromise({try:()=>fs.open(sleighHome,'r'),catch:()=>new Error('Decompiler assets unavailable.')}),handle=>Effect.promise(()=>handle.close())) : undefined
        const decompilerIdentity=operation==='decompile' ? yield* Effect.uninterruptible(Effect.tryPromise({try:async()=>{
          if(!decompileProfile||JSON.stringify(decompileConfiguredProfile(process.env,profile))!==JSON.stringify(decompileProfile)||!rootHandle)throw new Error()
          const root=await rootHandle.stat(),pathRoot=await fs.stat(sleighHome);if(!root.isDirectory()||root.dev!==pathRoot.dev||root.ino!==pathRoot.ino)throw new Error()
          const plugin=await boundedFileIdentity(pluginPath,MAX_PROVIDER,ctx.abort);if(plugin.sha!==decompileProfile.pluginSHA256)throw new Error()
          const manifest=manifestPath?await boundedFileIdentity(manifestPath,64*1024,ctx.abort):null
          if((manifest?.sha||null)!==decompileProfile.manifestSHA256)throw new Error()
          return {root:{dev:root.dev,ino:root.ino},plugin,manifest}
        },catch:()=>new Error('Decompiler profile unavailable.')})) : undefined
        if(ctx.abort.aborted) throw new Error('Analysis cancelled.')
        const target=captured.functionTarget,instructionTarget=captured.instructionTarget
        const requested=operation==='decompile'?args.decompile_target:operation==='xrefs_to_entry'?args.xref_target:args.function_target
        if((operation==='disassembly'||operation==='xrefs_to_entry'||operation==='decompile') && (!target || !requested || (operation==='disassembly' ? target.maxInstructions!==args.function_target?.max_instructions : operation==='xrefs_to_entry' ? target.maxReferences!==args.xref_target?.max_references : false) || target.selector.name!==requested.selector.name || target.selector.address!==requested.selector.address || target.selector.size!==requested.selector.size || target.selector.addressSpace!==requested.selector.addressSpace || target.dossierID!==requested.dossier_id || target.dossierRevision.kind!==requested.expected_dossier_revision.kind || target.dossierRevision.value!==requested.expected_dossier_revision.value)) throw new Error('Verified target unavailable.')
        if(operation==='xrefs_from_instruction' && (!instructionTarget || !args.instruction_target || instructionTarget.maxReferences!==args.instruction_target.max_references || instructionTarget.dossierID!==args.instruction_target.dossier_id || instructionTarget.dossierRevision.kind!==args.instruction_target.expected_dossier_revision.kind || instructionTarget.dossierRevision.value!==args.instruction_target.expected_dossier_revision.value || (['address','size','opcode','addressSpace'] as const).some(k=>instructionTarget.instruction[k]!==args.instruction_target!.selector[k])))throw new Error('Verified instruction unavailable.')
        const command=operation==='xrefs_from_instruction' && instructionTarget ? `aa;pdj 1 @ 0x${instructionTarget.instruction.address.toString(16)};axfj @ 0x${instructionTarget.instruction.address.toString(16)};pdj 1 @ 0x${instructionTarget.instruction.address.toString(16)}` : operation==='decompile' && target ? `aa;afij @ 0x${target.selector.address.toString(16)};pdgj @ 0x${target.selector.address.toString(16)};afij @ 0x${target.selector.address.toString(16)}` : operation === 'info' ? 'ij' : operation === 'strings' ? 'izj' : operation==='disassembly' && target ? `pdj ${target.maxInstructions} @ 0x${target.selector.address.toString(16)}` : operation==='xrefs_to_entry' && target ? `aa;axtj @ 0x${target.selector.address.toString(16)}` : 'aa;aflj'
        mayHaveSpawned=true
        const handle=yield* spawner.spawn(ChildProcess.make(executable,operation==='decompile'?['-NN','-l',pluginPath,'-q','-c',command,input]:['-NN','-q','-c',command,input],{cwd:dir,env:operation==='decompile'?{SLEIGHHOME:sleighHome}:{},extendEnv:false,stdin:'ignore'}))
        let output=Buffer.alloc(0), overflow=false, stderrBytes=0
        const collector=yield* Effect.forkScoped(Stream.runForEach(handle.stdout, chunk=>Effect.gen(function*(){if(output.length+chunk.length>MAX_OUTPUT){if(!overflow){overflow=true;yield* handle.kill({forceKillAfter:'3 seconds'})}} else output=Buffer.concat([output,Buffer.from(chunk)])})))
        const errors=yield* Effect.forkScoped(Stream.runForEach(handle.stderr, chunk=>Effect.gen(function*(){stderrBytes+=chunk.length;if(stderrBytes>MAX_OUTPUT && !overflow){overflow=true;yield* handle.kill({forceKillAfter:'3 seconds'})}})))
        const aborted=Effect.callback<void>(resume=>{if(ctx.abort.aborted)return resume(Effect.void); const f=()=>resume(Effect.void);ctx.abort.addEventListener('abort',f,{once:true});return Effect.sync(()=>ctx.abort.removeEventListener('abort',f))})
        const exit=yield* Effect.raceAll([handle.exitCode.pipe(Effect.map(code=>({kind:'exit' as const,code}))),aborted.pipe(Effect.map(()=>({kind:'abort' as const,code:null}))),Effect.sleep('15 seconds').pipe(Effect.map(()=>({kind:'timeout' as const,code:null})))])
        if(exit.kind!=='exit') { yield* handle.kill({forceKillAfter:'3 seconds'}); yield* handle.exitCode }
        settled=true
        yield* Fiber.join(collector); yield* Fiber.join(errors)
        if(exit.kind!=='exit' || exit.code!==0 || overflow || ctx.abort.aborted) throw new Error('Rizin analysis failed, exceeded bounds or was cancelled.')
        yield* Effect.uninterruptible(Effect.tryPromise({try:async()=>{const after=await boundedFileIdentity(executable,MAX_PROVIDER,ctx.abort);const staged=await boundedFileIdentity(input,captured.size,ctx.abort);if(JSON.stringify(after)!==JSON.stringify(providerIdentity) || staged.sha!==captured.sourceSHA256 || ctx.abort.aborted)throw new Error()},catch:()=>new Error('Provider or staged input changed; analysis withheld.')}))
        if(operation==='decompile')yield* Effect.uninterruptible(Effect.tryPromise({try:async()=>{if(!decompileProfile||!decompilerIdentity||!rootHandle||JSON.stringify(decompileConfiguredProfile(process.env,profile))!==JSON.stringify(decompileProfile))throw new Error();const root=await rootHandle.stat(),atPath=await fs.stat(sleighHome);const plugin=await boundedFileIdentity(pluginPath,MAX_PROVIDER,ctx.abort),manifest=manifestPath?await boundedFileIdentity(manifestPath,64*1024,ctx.abort):null;if(!root.isDirectory()||root.dev!==decompilerIdentity.root.dev||root.ino!==decompilerIdentity.root.ino||atPath.dev!==root.dev||atPath.ino!==root.ino||JSON.stringify(plugin)!==JSON.stringify(decompilerIdentity.plugin)||JSON.stringify(manifest)!==JSON.stringify(decompilerIdentity.manifest))throw new Error()},catch:()=>new Error('Decompiler profile changed; analysis withheld.')}))
        const parsed=yield* Effect.try({try:()=>operation==='decompile'||operation==='xrefs_from_instruction'?parseThreeDocuments(output):parseProviderJson(output),catch:()=>new Error('Rizin returned unsupported output.')})
        const base:Record<string,unknown>={...profileResultIdentity(profile),providerSHA256:expectedHash,sourceSHA256:captured.sourceSHA256,sourceRevision:captured.revision,coverage:'partial_static_analysis'}
        let rendered:string
        if (operation === 'xrefs_from_instruction') {
          if(!instructionTarget||!Array.isArray(parsed)||parsed.length!==3)throw new Error('Instruction reference evidence unavailable.')
          const [before,body,after]=parsed,selected=instructionTarget.instruction
          const matches=(value:unknown)=>Array.isArray(value)&&value.length===1&&value[0]&&typeof value[0]==='object'&&!Array.isArray(value[0])&&integer(value[0].offset)===selected.address&&integer(value[0].size)===selected.size&&safeContent(value[0].opcode,1024)===selected.opcode
          if(!matches(before)||!matches(after)||!Array.isArray(body)||body.length>16384)throw new Error('Instruction reference evidence unavailable.')
          const all=body.map((row:unknown)=>{if(!row||typeof row!=='object'||Array.isArray(row))throw new Error('Invalid reference evidence.');const v=row as Record<string,unknown>,from=integer(v.from),to=integer(v.to),type=safeText(v.type,64);if(from!==selected.address||to===null||!type)throw new Error('Invalid reference evidence.');return {from,to,type,addressSpace:'provider_virtual'}})
          const rows=all.slice(0,instructionTarget.maxReferences)
          rendered=boundedResult({...base,architecture:'unknown',direction:'outgoing_from_instruction',instructionTarget,omitted:all.length-rows.length},'xrefs_from_instruction',rows)
        } else if (operation === 'decompile') {
          if(!target||!decompileProfile||!Array.isArray(parsed)||parsed.length!==3)throw new Error('Decompiler result unavailable.')
          const [before,body,after]=parsed
          const matches=(value:unknown)=>Array.isArray(value)&&value.length===1&&value[0]&&typeof value[0]==='object'&&!Array.isArray(value[0])&&integer(value[0].offset)===target.selector.address&&integer(value[0].size)===target.selector.size&&safeText(value[0].name,512)===target.selector.name
          if(!matches(before)||!matches(after)||!body||typeof body!=='object'||Array.isArray(body)||Object.keys(body).sort().join(',')!=='annotations,code')throw new Error('Decompiler result unavailable.')
          const record=body as Record<string,unknown>;if(!Array.isArray(record.annotations))throw new Error('Decompiler result unavailable.')
          const code=safeContent(record.code,48*1024);if(!code||!code.trim())throw new Error('Decompiler result unavailable.')
          rendered=boundedResult({...base,architecture:'unknown',functionTarget:target,profile:decompileProfile,profileEvidence:{provider:'measured_configured_file',plugin:'measured_configured_file',root:'configured_selector_and_observed_directory',manifest:decompileProfile.manifestSHA256?'measured_declaration_only':'unavailable',dependencies:'unverified'},completion:'unknown',interpretation:'partial_provider_interpretation',decompile:code})
        } else if (operation === 'xrefs_to_entry') {
          if (!target || typeof target.maxReferences!=='number' || !Array.isArray(parsed) || parsed.length>16384) throw new Error('Rizin returned invalid reference evidence.')
          const all=parsed.map((row:unknown)=>{
            if (!row || typeof row!=='object' || Array.isArray(row)) throw new Error('Rizin returned invalid reference evidence.')
            const v=row as Record<string,unknown>,from=integer(v.from),to=integer(v.to),type=safeText(v.type,64)
            if(from===null || to!==target.selector.address || !type) throw new Error('Rizin returned invalid reference evidence.')
            return {from,to,type,addressSpace:'provider_virtual'}
          })
          const rows=all.slice(0,target.maxReferences)
          rendered=boundedResult({...base,architecture:'unknown',direction:'incoming_to_entry',functionTarget:target,omitted:all.length-rows.length},'xrefs_to_entry',rows)
        } else if (operation === 'disassembly') {
          if (!target || typeof target.maxInstructions!=='number' || !Array.isArray(parsed) || parsed.length>target.maxInstructions) throw new Error('Rizin returned invalid instruction evidence.')
          let previous=-1,dropped=0
          const rows=[] as Array<{address:number;size:number;opcode:string;addressSpace:'provider_virtual'}>
          for (const row of parsed) {
            if (!row || typeof row!=='object' || Array.isArray(row)) throw new Error('Rizin returned invalid instruction evidence.')
            const v=row as Record<string,unknown>,address=integer(v.offset),size=integer(v.size),opcode=safeContent(v.opcode,1024)
            if(address===null || size===null || size===0 || opcode===null || address<=previous || !Number.isSafeInteger(address+size)) throw new Error('Rizin returned invalid instruction evidence.')
            previous=address
            if(address<target.selector.address || address+size>target.selector.address+target.selector.size){dropped++;continue}
            rows.push({address,size,opcode,addressSpace:'provider_virtual'})
          }
          rendered=boundedResult({...base,architecture:'unknown',functionTarget:target,omitted:dropped},'disassembly',rows)
        } else if (operation === 'info') {
          if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Rizin returned invalid info evidence.')
          const bin=(parsed as Record<string,unknown>).bin
          if (bin != null && (typeof bin !== 'object' || Array.isArray(bin))) throw new Error('Rizin returned invalid info evidence.')
          const v=(bin || {}) as Record<string,unknown>
          const endian=safeText(v.endian, 16)
          if (endian !== null && endian !== 'LE' && endian !== 'BE') throw new Error('Rizin returned invalid info evidence.')
          rendered=boundedResult({...base,info:{architecture:safeText(v.arch,256),cpu:safeText(v.cpu,256),binaryType:safeText(v.bintype,256),os:safeText(v.os,256),endian,bits:integer(v.bits),baseAddress:integer(v.baddr),baseAddressSpace:'provider_virtual',binarySize:integer(v.binsz)}})
        } else {
          if(!Array.isArray(parsed) || parsed.length>16384) throw new Error('Rizin output exceeds supported bounds.')
          const limit=operation === 'strings' ? (args.max_strings ?? 128) : (args.max_functions ?? 128)
          const rows=parsed.slice(0,limit).map((row:unknown)=>{
            if (!row || typeof row !== 'object' || Array.isArray(row)) throw new Error('Rizin returned invalid evidence.')
            const v=row as Record<string,unknown>
            if (operation === 'functions') {
              const address=integer(v.offset), size=integer(v.size), name=safeText(v.name,512)
              if (name === null || address === null || size === null || !Number.isSafeInteger(address+size)) throw new Error('Rizin returned invalid function evidence.')
              return {name,address,size,addressSpace:'provider_virtual'}
            }
            const text=safeContent(v.string,1024), type=safeText(v.type,256), section=safeText(v.section,256)
            const size=integer(v.size), length=integer(v.length), physical=integer(v.paddr), virtual=integer(v.vaddr)
            if (text === null || size === null || length === null || size > captured.size || length > captured.size) throw new Error('Rizin returned invalid string evidence.')
            const fileOffset=physical !== null && Number.isSafeInteger(physical+size) && physical+size <= captured.size ? physical : null
            return {text,type,section,length,size,physicalAddress:fileOffset,physicalAddressSpace:'provider_physical_file_offset',physicalRangeEvidence:fileOffset === null ? 'unavailable' : 'provider_reported_bounds_checked',virtualAddress:virtual,virtualAddressSpace:'provider_virtual'}
          })
          rendered=boundedResult({...base,...(operation === 'functions' ? {architecture:'unknown'} : {}),omitted:parsed.length-rows.length},operation === 'strings' ? 'strings' : 'functions',rows)
        }
        const check=yield* Effect.tryPromise({try:()=>managedHostCall('_openclank/session/v1/native-analysis/source',qualified ? {...identity,operation:'finalize',captureToken:captured.captureToken,sourceSHA256:captured.sourceSHA256,messageID:ctx.messageID,inputSHA256} : {...identity,operation:'finalize',captureToken:captured.captureToken,sourceSHA256:captured.sourceSHA256}),catch:()=>new Error('Source reauthorization unavailable.')})
        if(check.state!=='current' || ctx.abort.aborted) throw new Error('Source or turn changed; analysis withheld.')
        if (!qualified) {
          const metadata: {analysisProvisional?:string} = {}
          return {title:`Rizin ${operation}`,metadata,output:rendered}
        }
        const resultBytes=Buffer.from(rendered,'utf8')
        const staged=yield* Effect.tryPromise({try:()=>managedHostCall('_openclank/session/v1/native-analysis/source',{...identity,operation:'stage',captureToken:captured.captureToken,sourceSHA256:captured.sourceSHA256,messageID:ctx.messageID,inputSHA256,analysisOperation:operation,providerSHA256:expectedHash,providerVersion:profile.version,outputSHA256:createHash('sha256').update(resultBytes).digest('hex'),outputBase64:resultBytes.toString('base64')}),catch:()=>new Error('Analysis result staging unavailable.')})
        if(staged.state!=='staged' || ctx.abort.aborted) throw new Error('Analysis result staging unavailable or cancelled.')
        const metadata: {analysisProvisional?:string} = {analysisProvisional:staged.provisional}
        return {title:`Rizin ${operation}`,metadata,output:rendered}
      }))
      return yield* run
    }).pipe(Effect.orDie),
  }
})

export const NativeAnalysisTool = Tool.define('native_analysis', nativeAnalysisInit)
