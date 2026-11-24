/** Private app-captured default fetch and pinned compatible serializer identity. */
type Witness = Readonly<{ endpoint:string; fetch:typeof fetch }>
const sdks = new WeakMap<object,Witness>()
const models = new WeakMap<object,Witness & {readonly modelID:string}>()
export function noteSameRequestSDK(sdk:object, endpoint:string, fetch:typeof globalThis.fetch) {
  sdks.set(sdk, Object.freeze({endpoint, fetch}))
}
export function noteSameRequestModel(sdk:object, model:object, modelID:string) {
  const witness=sdks.get(sdk)
  if (witness && typeof modelID==='string' && !!modelID && (model as {modelId?:unknown}).modelId===modelID) models.set(model,Object.freeze({...witness,modelID}))
}
export function sameRequestRoute(model:object, fetch:typeof globalThis.fetch, endpoint:string):string | undefined {
  const witness=models.get(model)
  return witness && witness.fetch===fetch && witness.endpoint===endpoint && (model as {modelId?:unknown}).modelId===witness.modelID ? witness.modelID : undefined
}
