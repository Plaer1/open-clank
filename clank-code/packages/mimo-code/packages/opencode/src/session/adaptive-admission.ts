/** Original optional adaptive preference, never an execution grant. */
const keys=['version','requested','sessionID','rootOperationID','hostAccountID','holderID','generation','settingsRevision','providerID','modelID','connectionID','modelRouteID','routeMethodKey','manualMode','manualRevision']
export function encodeAdaptiveAdmission(value:unknown,sessionID:string,route:Record<string,unknown>|undefined,model:{providerID:string;modelID:string}):string|undefined {
  try {
    if (!value || typeof value!=='object' || Array.isArray(value) || !route) return
    const v=value as Record<string,unknown>
    if (Object.keys(v).length!==keys.length || keys.some(k=>!(k in v)) || v.version!==1 || v.requested!==true || v.manualMode!=='normal' ||
        !Number.isSafeInteger(v.manualRevision) || (v.manualRevision as number)<0 || !Number.isSafeInteger(v.generation) || (v.generation as number)<0 ||
        typeof v.settingsRevision!=='string' || !/^[a-f0-9]{64}$/.test(v.settingsRevision) || v.sessionID!==sessionID ||
        v.rootOperationID!==route.rootOperationID || v.providerID!==route.providerID || v.modelID!==route.modelID ||
        v.connectionID!==route.connectionID || v.modelRouteID!==route.modelRouteID ||
        (model.providerID!==v.providerID && model.providerID!==v.connectionID) || model.modelID!==v.modelID ||
        ['sessionID','rootOperationID','hostAccountID','holderID','providerID','modelID','connectionID','modelRouteID','routeMethodKey'].some(k=>typeof v[k]!=='string' || !(v[k] as string) || Buffer.byteLength(v[k] as string,'utf8')>256 || Buffer.from(v[k] as string,'utf8').toString('utf8')!==v[k])) return
    return JSON.stringify(Object.fromEntries([...keys].sort().map(k=>[k,v[k]])))
  } catch { return }
}
