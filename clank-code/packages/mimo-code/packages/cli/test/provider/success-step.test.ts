import { expect, test } from 'bun:test'
import { streamText, wrapLanguageModel } from 'ai'
import { MockLanguageModelV3 } from 'ai/test'
import { captureFetch, installCaptureScope, markSdkSuccessStep, observeCodexInnerSend, takeSdkSuccessStep, withSdkDispatches } from '../../src/provider/logging-transport'

test('pinned SDK carries only the exact one-use qualified success step', async () => {
  process.env.OPEN_CLANK_MANAGED='1'
  const binding={bindingID:'b',bindingRevision:1,rootOperationID:'r',connectionID:'c',providerID:'openai',billingLane:'subscription',modelID:'m',credentialRequired:true,accountID:'a',credentialRevision:1,source:'round_robin',attempt:1,committed:false} as any
  const scope=installCaptureScope({} as any,binding,{extMethod:async()=>({})} as any,null)
  const endpoint=new URL('https://chatgpt.com/backend-api/codex/responses'), request={method:'POST',body:'{}'}
  const response=()=>{const value=new Response(null,{status:200});Object.defineProperty(value,'url',{value:endpoint.href});return value}
  const ids:string[]=[]
  const language=new MockLanguageModelV3({doStream:async()=>{
    await captureFetch((async()=>observeCodexInnerSend(endpoint,request,async()=>response())) as any,scope)('https://example.invalid/v1/responses',request)
    return {stream:new ReadableStream({start(controller){
      controller.enqueue({type:'stream-start',warnings:[]})
      controller.enqueue({type:'finish',finishReason:{unified:'stop',raw:'stop'},usage:{inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:0,text:0,reasoning:0}}})
      controller.close()
    }})}
  }})
  const model=wrapLanguageModel({model:language,middleware:{specificationVersion:'v3',async wrapStream({doStream}){
    const result=await withSdkDispatches(scope,ids,doStream)
    return {...result,stream:result.stream.pipeThrough(new TransformStream({transform(chunk,controller){
      if(chunk.type==='finish') controller.enqueue({...chunk,providerMetadata:markSdkSuccessStep(scope,ids,chunk.providerMetadata)})
      else controller.enqueue(chunk)
    }}))}
  }}})
  try {
    const result=streamText({model,prompt:'synthetic'})
    let metadata:any
    for await (const event of result.fullStream) if(event.type==='finish-step') metadata=event.providerMetadata
    const taken=takeSdkSuccessStep(scope,metadata)
    expect(taken.observation).toMatchObject({profile:'openai.codex.responses.v1',captureDispatchID:ids[0],httpStatus:200})
    expect(taken.observation?.upstreamDispatchID).not.toBe(ids[0])
    expect(taken.metadata?._openclankSuccessStep).toBeUndefined()
    expect(takeSdkSuccessStep(scope,metadata).observation).toBeUndefined()
    expect(markSdkSuccessStep(scope,[...ids,'another'],{})).toEqual({})
    expect(takeSdkSuccessStep(scope,{_openclankSuccessStep:{marker:'changed'}}).observation).toBeUndefined()
  } finally { delete process.env.OPEN_CLANK_MANAGED }
})
