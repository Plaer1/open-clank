import { expect, test } from "bun:test"
import { createOpenAI } from "@ai-sdk/openai"
import { JSONParseError, TypeValidationError } from "@ai-sdk/provider"
import { streamText, wrapLanguageModel } from "ai"
import { captureFetch, installCaptureScope, withSdkDispatches, observeCodexInnerSend, markMalformedResponse, emitMalformedDiagnostic } from "../../src/provider/logging-transport"
import { notePinnedResponsesSDK, notePinnedResponsesModel, isPinnedResponsesModel } from "../../src/provider/malformed-origin"
test("pinned Responses parser error is diagnostic-only for one observed 2xx", async () => {
  const old=process.env.OPEN_CLANK_MANAGED;process.env.OPEN_CLANK_MANAGED="1";const sent:any[]=[]
  const admission={admissionID:"admit",policy:{advanced_enabled:true,request_body_enabled:false,response_body_enabled:false,binary_body_enabled:false,revision:1},context:{providerID:"openai",operationID:"op",rootOperationID:"root",instanceID:"holder",bindingID:"b",bindingRevision:1,identityCoverage:"complete",persistence:{numeric:true,content:true,reason:"normal"},transportMode:"direct"}} as any
  const binding={bindingID:"b",bindingRevision:1,rootOperationID:"root",connectionID:"c",providerID:"openai",billingLane:"subscription",modelID:"m",credentialRequired:true,accountID:"a",credentialRevision:1,source:"round_robin",attempt:1,committed:false} as any
  const connection={async extMethod(_method:string,payload:any){sent.push(payload);return {accepted:true,replayed:false,captureState:"ready"}}} as any
  const scope=installCaptureScope({} as any,binding,connection,admission), endpoint=new URL("https://chatgpt.com/backend-api/codex/responses")
  let body='data: {broken}\n\n'
  const upstream=async()=>{const r=new Response(body,{status:200,headers:{"content-type":"text/event-stream"}});Object.defineProperty(r,"url",{value:endpoint.href});return r}
  const fake=async (_input:any,init:any)=>observeCodexInnerSend(endpoint,{method:"POST",body:typeof init?.body==="string"?init.body:"{}"},upstream)
  const sdk=createOpenAI({apiKey:"test",baseURL:"https://unused.invalid/v1",fetch:captureFetch(fake as any,scope)})
  const language=sdk.responses("test");notePinnedResponsesSDK(sdk as object);notePinnedResponsesModel(sdk as object,language as object)
  const ids:string[]=[]; let observed:any;const signal=new AbortController().signal
  const model=wrapLanguageModel({model:language,middleware:{specificationVersion:"v3",async wrapStream({doStream}) {
    const result=await withSdkDispatches(scope,ids,doStream)
    return {...result,stream:result.stream.pipeThrough(new TransformStream({transform(chunk,controller){
      if(chunk.type==="error" && isPinnedResponsesModel(language as object) && (JSONParseError.isInstance(chunk.error)||TypeValidationError.isInstance(chunk.error))) {observed=chunk.error;markMalformedResponse(chunk.error,scope,ids,signal,language as object)}
      controller.enqueue(chunk)
    }}))}
  }}})
  try {
    const result=streamText({model,prompt:"fixture",maxRetries:0});let received:any
    for await (const part of result.fullStream) if(part.type==="error") received=part.error
    expect(received).toBe(observed);expect(ids).toHaveLength(1)
    emitMalformedDiagnostic(received,scope);await new Promise(resolve=>setTimeout(resolve,20))
    const diagnostics=sent.filter(x=>x.diagnosticOnly)
    expect(diagnostics).toHaveLength(1);expect(diagnostics[0].diagnosticOnly).toMatchObject({kind:"malformed_success",httpStatus:200,profile:"openai.codex.responses.v1"})
    emitMalformedDiagnostic(received,scope);await new Promise(resolve=>setTimeout(resolve,10));expect(sent.filter(x=>x.diagnosticOnly)).toHaveLength(1)
    const other={};markMalformedResponse(other,scope,ids,signal,language as object);emitMalformedDiagnostic(other,scope)
    markMalformedResponse(received,scope,[...ids,"extra"],signal,language as object);emitMalformedDiagnostic(received,scope)
    markMalformedResponse(received,scope,ids,signal,{});emitMalformedDiagnostic(received,scope)
    const cancelled=new AbortController();cancelled.abort();markMalformedResponse(received,scope,ids,cancelled.signal,language as object);emitMalformedDiagnostic(received,scope)
    markMalformedResponse(received,scope,ids,signal,language as object);emitMalformedDiagnostic(received,{})
    expect(sent.filter(x=>x.diagnosticOnly)).toHaveLength(1)
    markMalformedResponse(received,scope,ids,signal,language as object)
    binding.credentialRevision=2;emitMalformedDiagnostic(received,scope);binding.credentialRevision=1
    expect(sent.filter(x=>x.diagnosticOnly)).toHaveLength(1)
    markMalformedResponse(received,scope,ids,signal,language as object)
    admission.context.instanceID="other-holder";emitMalformedDiagnostic(received,scope);admission.context.instanceID="holder"
    expect(sent.filter(x=>x.diagnosticOnly)).toHaveLength(1)
    markMalformedResponse(received,scope,ids,signal,language as object)
    installCaptureScope(scope,binding,connection,{...admission,admissionID:"replacement"})
    emitMalformedDiagnostic(received,scope)
    expect(sent.filter(x=>x.diagnosticOnly)).toHaveLength(1)
    ids.length=0;observed=undefined
    body='data: {"type":"error","sequence_number":1,"error":{"type":"server_error","code":"bad","message":"fixture"}}\n\n'
    for await (const _part of streamText({model,prompt:"fixture",maxRetries:0}).fullStream) {}
    expect(observed).toBeUndefined()
    ids.length=0;observed=undefined;body=''
    for await (const _part of streamText({model,prompt:"fixture",maxRetries:0}).fullStream) {}
    expect(observed).toBeUndefined()
    expect(sent.filter(x=>x.diagnosticOnly)).toHaveLength(1)
  } finally {if(old===undefined)delete process.env.OPEN_CLANK_MANAGED;else process.env.OPEN_CLANK_MANAGED=old}
})
