/** Private preference identity only. The producer/route/artifact gates are not joined. */
export type GcfAdmission = Readonly<{
  version: 1
  sessionID: string
  rootOperationID: string
  generation: number
  settingsRevision: string
  requested: boolean
  memoize?: boolean
  sameRequestRows?: true
  graphReferences?: true
  sessionView?: true
  providerID: string
  modelID: string
}>

export function encodeGcfAdmission(value: unknown, sessionID: string, rootOperationID: unknown, model: {providerID: string; modelID: string}): string | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return
  const v = value as Record<string, unknown>
  const keys = ['version','sessionID','rootOperationID','generation','settingsRevision','requested','providerID','modelID']
  if ('memoize' in v) keys.push('memoize')
  if ('sameRequestRows' in v) keys.push('sameRequestRows')
  if ('graphReferences' in v) keys.push('graphReferences')
  if ('sessionView' in v) keys.push('sessionView')
  if (Object.keys(v).length !== keys.length || keys.some((k) => !(k in v)) || v.version !== 1 || v.sessionID !== sessionID ||
      v.rootOperationID !== rootOperationID || typeof rootOperationID !== 'string' || !rootOperationID ||
      typeof v.generation !== 'number' || !Number.isSafeInteger(v.generation) || v.generation < 0 ||
      typeof v.settingsRevision !== 'string' || !/^[a-f0-9]{64}$/.test(v.settingsRevision) ||
      typeof v.requested !== 'boolean' || ('memoize' in v && typeof v.memoize !== 'boolean') || ('sameRequestRows' in v && v.sameRequestRows !== true) || ('graphReferences' in v && v.graphReferences !== true) || ('sessionView' in v && v.sessionView !== true) || v.providerID !== model.providerID || v.modelID !== model.modelID) return
  return JSON.stringify({version:1,sessionID,rootOperationID,generation:v.generation,settingsRevision:v.settingsRevision,
    requested:v.requested,...('memoize' in v ? {memoize:v.memoize} : {}),...('sameRequestRows' in v ? {sameRequestRows:true} : {}),...('graphReferences' in v ? {graphReferences:true} : {}),...('sessionView' in v ? {sessionView:true} : {}),providerID:model.providerID,modelID:model.modelID})
}

/** Preserve old Off profile keys. Non-Off identity never authorizes encoding. */
export function gcfViewIdentity(encoded: string | undefined, sessionID: string, rootOperationID: string | undefined,
  model: {providerID: string; modelID: string}, artifactIdentity?: string): string | undefined {
  if (!encoded || !rootOperationID) return
  try {
    const value = JSON.parse(encoded) as GcfAdmission
    if (!value.requested || encodeGcfAdmission(value,sessionID,rootOperationID,model) !== encoded) return
    return JSON.stringify({version:1,policy:'gcf-generic-v3-safe-integers-v1',codec:'openclank-gcf-pure-v1',
      settingsRevision:value.settingsRevision,providerID:value.providerID,modelID:value.modelID,...(artifactIdentity ? {artifactIdentity} : {})})
  } catch { return }
}

/** A cache key is an optimization hint, never an admission or source grant. */
export function gcfMemoScope(encoded:string | undefined, account:string | undefined, session:string, root:string | undefined, actor:{sessionID:string;actorID:string;executionRevision:number} | undefined):string | undefined {
  if (!encoded?.includes('"memoize":true') || !account || !root || !actor || actor.sessionID !== session || !actor.actorID || !Number.isSafeInteger(actor.executionRevision) || actor.executionRevision < 0) return
  try {
    const admission=JSON.parse(encoded) as GcfAdmission
    if (admission.requested !== true || admission.memoize !== true || admission.rootOperationID !== root || !admission.sessionID ||
        encodeGcfAdmission(admission,admission.sessionID,root,{providerID:admission.providerID,modelID:admission.modelID}) !== encoded ||
        [account,session,root,actor.actorID].some(value => Buffer.byteLength(value,'utf8') > 256)) return
    return JSON.stringify([account,session,root,actor.actorID,actor.executionRevision,admission.generation,admission.settingsRevision,admission.providerID,admission.modelID])
  } catch { return }
}

/** A row-view selector is independent consent, not a source or codec grant. */
export function gcfSameRequestRowsAdmission(encoded: string | undefined, sessionID: string, rootOperationID: string | undefined, model: {providerID:string; modelID:string}): {version:1; settingsRevision:string} | undefined {
  if (!encoded || !rootOperationID) return
  try {
    const value = JSON.parse(encoded) as GcfAdmission
    if (value.sameRequestRows !== true || encodeGcfAdmission(value,sessionID,rootOperationID,model) !== encoded) return
    return {version:1,settingsRevision:value.settingsRevision}
  } catch { return }
}

/** Independent consent for same-request Code Graph deltas; not a source or codec grant. */
export function gcfGraphReferencesAdmission(encoded: string | undefined, sessionID: string, rootOperationID: string | undefined, model: {providerID:string; modelID:string}): {version:1; settingsRevision:string} | undefined {
  if (!encoded || !rootOperationID) return
  try {
    const value = JSON.parse(encoded) as GcfAdmission
    if (value.graphReferences !== true || encodeGcfAdmission(value,sessionID,rootOperationID,model) !== encoded) return
    return {version:1,settingsRevision:value.settingsRevision}
  } catch { return }
}

/** Separate consent for local descriptors; never a source or remote-state grant. */
export function gcfSessionViewAdmission(encoded: string | undefined, sessionID: string, rootOperationID: string | undefined, model: {providerID:string; modelID:string}): {version:1;settingsRevision:string} | undefined {
  if (!encoded || !rootOperationID) return
  try { const value=JSON.parse(encoded) as GcfAdmission
    if (value.requested !== true || value.sessionView !== true || encodeGcfAdmission(value,sessionID,rootOperationID,model)!==encoded) return
    return {version:1,settingsRevision:value.settingsRevision}
  } catch { return }
}
