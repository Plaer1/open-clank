/** Private original-turn identity for an explicitly declared argument wrapper. */
const fields=['version','requested','sessionID','rootOperationID','hostAccountID','holderID','generation','settingsRevision','providerID','modelID','routeMethodKey','connectionID','modelRouteID','profile']
export const GCF_ARGUMENT_TOOL='gcf_arguments'
export const GCF_ARGUMENT_POLICY='gcf-generic-v3-safe-integers-v1'
export function encodeArgumentAdmission(value:unknown,sessionID:string,route:Record<string,unknown>|undefined,model:{providerID:string;modelID:string}):string|undefined {
 if (!value || typeof value!=='object' || Array.isArray(value) || !route) return
 const v=value as Record<string,unknown>
 if (Object.keys(v).length!==fields.length || fields.some(k=>!Object.hasOwn(v,k)) || v.version!==1 || v.requested!==true || v.profile!=='gcf-argument-v1' || v.sessionID!==sessionID || v.rootOperationID!==route.rootOperationID || v.providerID!==route.providerID || v.modelID!==route.modelID || v.connectionID!==route.connectionID || v.modelRouteID!==route.modelRouteID || (model.providerID!==v.providerID && model.providerID!==v.connectionID) || model.modelID!==v.modelID || !Number.isSafeInteger(v.generation) || (v.generation as number)<0 || typeof v.settingsRevision!=='string' || !/^[a-f0-9]{64}$/.test(v.settingsRevision) || ['sessionID','rootOperationID','hostAccountID','holderID','providerID','modelID','routeMethodKey','connectionID','modelRouteID'].some(k=>typeof v[k]!=='string' || !(v[k] as string) || (v[k] as string).length>256)) return
 return JSON.stringify(v)
}
