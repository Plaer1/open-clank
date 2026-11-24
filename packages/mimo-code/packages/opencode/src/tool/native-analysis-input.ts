import { createHash } from 'node:crypto'
import path from 'node:path'
import { ANALYSIS_PROFILES, type AnalysisProfile } from './native-analysis-profile'

export function decompileConfiguredProfile(env:NodeJS.ProcessEnv=process.env,profile:AnalysisProfile=ANALYSIS_PROFILES[0]!) {
  const e=profile.env,provider=env[e.sha256] || '',plugin=env[e.ghidraSha256] || '',library=env[e.ghidraLibrary] || '',root=env[e.sleighHome] || ''
  const manifest=env[e.sleighManifest] || '',manifestSHA=env[e.sleighManifestSha256] || ''
  if(!/^[a-f0-9]{64}$/.test(provider)||!/^[a-f0-9]{64}$/.test(plugin)||!path.isAbsolute(library)||!path.isAbsolute(root)||[library,root,manifest].some(v=>Buffer.byteLength(v,'utf8')>4096||v.includes('\0')||/[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/u.test(v)) || (!!manifest!==!!manifestSHA) || (manifest && (!path.isAbsolute(manifest)||!/^[a-f0-9]{64}$/.test(manifestSHA))))return
  const rootSelectorSHA256=createHash('sha256').update(profile.rootSelectorDomain+'\0').update(path.normalize(root),'utf8').digest('hex')
  return {providerSHA256:provider,pluginSHA256:plugin,rootSelectorSHA256,manifestSHA256:manifestSHA || null}
}

/** Canonical identity only; a selector never grants authority to read a dossier. */
export function nativeAnalysisInputSHA(value: unknown): string | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return
  const args=value as Record<string,unknown>
  if (Object.keys(args).some(key=>!['source_ref','revision','operation','max_functions','max_strings','function_target','xref_target','decompile_target','instruction_target'].includes(key))) return
  const revision=args.revision as Record<string,unknown> | undefined
  if (!revision || typeof revision!=='object' || Array.isArray(revision) || Object.keys(revision).sort().join(',')!=='kind,value') return
  const source=args.source_ref,kind=revision.kind,revisionValue=revision.value,operation=args.operation ?? 'functions',maxFunctions=args.max_functions ?? 128,maxStrings=args.max_strings ?? 128
  if (typeof source!=='string' || source.length<8 || source.length>16384 || !/^[\x21-\x7e]+$/.test(source)
    || typeof kind!=='string' || kind.length<1 || kind.length>64 || !/^[\x21-\x7e]+$/.test(kind)
    || typeof revisionValue!=='string' || revisionValue.length<1 || revisionValue.length>256 || !/^[\x21-\x7e]+$/.test(revisionValue)
    || typeof operation!=='string' || !['functions','info','strings','disassembly','xrefs_to_entry','decompile','xrefs_from_instruction'].includes(operation)
    || typeof maxFunctions!=='number' || !Number.isSafeInteger(maxFunctions) || maxFunctions<1 || maxFunctions>256
    || typeof maxStrings!=='number' || !Number.isSafeInteger(maxStrings) || maxStrings<1 || maxStrings>256) return
  let fields:unknown[]=['openclank.native-analysis.v1',source,kind,revisionValue,operation,maxFunctions,maxStrings]
  if(operation==='xrefs_from_instruction'){
    if(args.function_target!==undefined||args.xref_target!==undefined||args.decompile_target!==undefined)return
    const t=args.instruction_target as Record<string,unknown> | undefined
    if(!t||typeof t!=='object'||Array.isArray(t)||!['dossier_id,expected_dossier_revision,max_references,selector','dossier_id,expected_dossier_revision,selector'].includes(Object.keys(t).sort().join(',')))return
    const rev=t.expected_dossier_revision as Record<string,unknown> | undefined,s=t.selector as Record<string,unknown> | undefined,count=t.max_references??64
    if(!rev||typeof rev!=='object'||Array.isArray(rev)||Object.keys(rev).sort().join(',')!=='kind,value'||rev.kind!=='hostFingerprint'||typeof rev.value!=='string'||!/^sha256:[a-f0-9]{64}:[0-9]+$/.test(rev.value)||rev.value.length>256
      ||typeof t.dossier_id!=='string'||!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(t.dossier_id)||!s||typeof s!=='object'||Array.isArray(s)||Object.keys(s).sort().join(',')!=='address,addressSpace,opcode,size'
      ||typeof s.opcode!=='string'||/[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/u.test(s.opcode)||Buffer.byteLength(s.opcode,'utf8')>1024||s.addressSpace!=='provider_virtual'
      ||typeof s.address!=='number'||!Number.isSafeInteger(s.address)||s.address<0||typeof s.size!=='number'||!Number.isSafeInteger(s.size)||s.size<=0||!Number.isSafeInteger(s.address+s.size)
      ||typeof count!=='number'||!Number.isSafeInteger(count)||count<1||count>128)return
    fields=['openclank.native-analysis.xrefs-from-instruction.v1',source,kind,revisionValue,t.dossier_id,rev.kind,rev.value,s.address,s.size,s.opcode,s.addressSpace,count]
  } else if (operation==='disassembly' || operation==='xrefs_to_entry' || operation==='decompile') {
    if(args.instruction_target!==undefined)return
    const xref=operation==='xrefs_to_entry',decompile=operation==='decompile',countKey=xref?'max_references':'max_instructions'
    if ((xref && (args.function_target!==undefined || args.decompile_target!==undefined)) || (decompile && (args.function_target!==undefined || args.xref_target!==undefined)) || (!xref && !decompile && (args.xref_target!==undefined || args.decompile_target!==undefined)))return
    const t=(decompile?args.decompile_target:xref?args.xref_target:args.function_target) as Record<string,unknown> | undefined
    if(!t || typeof t!=='object' || Array.isArray(t) || !(decompile?['dossier_id,expected_dossier_revision,selector']:[`dossier_id,expected_dossier_revision,${countKey},selector`,'dossier_id,expected_dossier_revision,selector']).includes(Object.keys(t).sort().join(',')))return
    const rev=t.expected_dossier_revision as Record<string,unknown> | undefined,s=t.selector as Record<string,unknown> | undefined
    const count=t[countKey] ?? (xref?64:32)
    if(!rev || typeof rev!=='object' || Array.isArray(rev) || Object.keys(rev).sort().join(',')!=='kind,value' || rev.kind!=='hostFingerprint' || typeof rev.value!=='string' || !/^sha256:[a-f0-9]{64}:[0-9]+$/.test(rev.value) || rev.value.length>256
      || typeof t.dossier_id!=='string' || !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(t.dossier_id)
      || !s || typeof s!=='object' || Array.isArray(s) || Object.keys(s).sort().join(',')!=='address,addressSpace,name,size'
      || typeof s.name!=='string' || /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/u.test(s.name) || Buffer.byteLength(s.name,'utf8')>512 || s.addressSpace!=='provider_virtual'
      || typeof s.address!=='number' || !Number.isSafeInteger(s.address) || s.address<0 || typeof s.size!=='number' || !Number.isSafeInteger(s.size) || s.size<=0 || !Number.isSafeInteger(s.address+s.size)
      || typeof count!=='number' || !Number.isSafeInteger(count) || count<1 || count>128)return
    if(decompile){const profile=decompileConfiguredProfile();if(!profile)return;fields=['openclank.native-analysis.decompile.v1',source,kind,revisionValue,t.dossier_id,rev.kind,rev.value,s.name,s.address,s.size,s.addressSpace,profile.providerSHA256,profile.pluginSHA256,profile.rootSelectorSHA256,profile.manifestSHA256]}
    else fields=[xref?'openclank.native-analysis.xrefs-to-entry.v1':'openclank.native-analysis.disassembly.v1',source,kind,revisionValue,t.dossier_id,rev.kind,rev.value,s.name,s.address,s.size,s.addressSpace,count]
  } else if(args.function_target!==undefined || args.xref_target!==undefined || args.decompile_target!==undefined || args.instruction_target!==undefined)return
  return createHash('sha256').update(JSON.stringify(fields),'utf8').digest('hex')
}
