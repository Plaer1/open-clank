import { expect, test } from "bun:test"
import { createOpenAI } from "@ai-sdk/openai"
import { JSONParseError, TypeValidationError } from "@ai-sdk/provider"
import { streamText, wrapLanguageModel } from "ai"
import { attachCaptureAttempt, captureFetch, emitMalformedDiagnostic, installCaptureScope, markMalformedResponse, observeCodexInnerSend, withSdkDispatches } from "../../src/provider/logging-transport"
import { drainAnthropicCallbacks, type AnthropicFailureProbe } from "../../src/provider/fallback-terminal-witness"
import { notePinnedResponsesSDK, notePinnedResponsesModel } from "../../src/provider/malformed-origin"

test("capture fence owns best effort and blocks a deferred write after closure", async () => {
  const old = process.env.OPEN_CLANK_MANAGED; process.env.OPEN_CLANK_MANAGED = "1"
  const binding:any={bindingID:"b",bindingRevision:1,rootOperationID:"r",connectionID:"c",providerID:"openai",billingLane:"subscription",modelID:"m",credentialRequired:true,accountID:"a",credentialRevision:1,source:"round_robin",attempt:1,committed:false}
  const admission:any={admissionID:"a",policy:{revision:1,advanced_enabled:true,request_body_enabled:true,response_body_enabled:false,binary_body_enabled:false},context:{instanceID:"h",providerID:"openai",operationID:"o",rootOperationID:"r",bindingID:"b",bindingRevision:1,identityCoverage:"complete",transportMode:"advanced_proxy",persistence:{numeric:true,content:true,reason:"normal"}}}
  let releaseDiagnostic!:()=>void, diagnosticStarted=false, events=0
  const connection:any={extMethod(_method:string,payload:any){events++; if(payload.diagnosticOnly){diagnosticStarted=true;return new Promise(resolve=>{releaseDiagnostic=()=>resolve({accepted:true})})} return Promise.resolve({accepted:true})}}
  const scope=installCaptureScope({} as any,binding,connection,admission), probe:AnthropicFailureProbe={sends:0,state:"pending"}
  expect(attachCaptureAttempt(scope,probe)).toBe(true)
  const endpoint=new URL("https://chatgpt.com/backend-api/codex/responses")
  const upstream=async()=>{const r=new Response('data: {broken}\n\n',{status:200,headers:{"content-type":"text/event-stream"}});Object.defineProperty(r,"url",{value:endpoint.href});return r}
  const fake:any=async (_input:any,init:any)=>observeCodexInnerSend(endpoint,{method:"POST",body:typeof init?.body==="string"?init.body:"{}"},upstream)
  const sdk=createOpenAI({apiKey:"fixture",baseURL:"https://unused.invalid/v1",fetch:captureFetch(fake,scope)})
  const language=sdk.responses("fixture");notePinnedResponsesSDK(sdk);notePinnedResponsesModel(sdk,language)
  const ids:string[]=[];let error:any;const signal=new AbortController().signal
  const model=wrapLanguageModel({model:language,middleware:{specificationVersion:"v3",async wrapStream({doStream}){const result=await withSdkDispatches(scope,ids,doStream);return {...result,stream:result.stream.pipeThrough(new TransformStream({transform(chunk,controller){if(chunk.type==="error"&&(JSONParseError.isInstance(chunk.error)||TypeValidationError.isInstance(chunk.error))){error=chunk.error;markMalformedResponse(error,scope,ids,signal,language)}controller.enqueue(chunk)}}))}}}})
  try {
    const prior=installCaptureScope({} as any,binding,connection,admission)
    await captureFetch(fake,prior)("https://unused.invalid/v1/responses",{method:"POST",body:"{}"})
    expect(attachCaptureAttempt(prior,{sends:0,state:"pending"})).toBe(false)
    for await (const _ of streamText({model,prompt:"fixture",maxRetries:0}).fullStream) {}
    emitMalformedDiagnostic(error,scope); expect(diagnosticStarted).toBe(true)
    let releaseBody!:()=>void
    const body=new ReadableStream({start(controller){releaseBody=()=>{controller.enqueue(new TextEncoder().encode("{}"));controller.close()}}})
    const request=new Request("https://unused.invalid/v1/responses",{method:"POST",body,duplex:"half"} as any)
    await captureFetch(fake,scope)(request)
    const before=events
    expect(await drainAnthropicCallbacks(probe,5)).toBe(false)
    releaseDiagnostic();releaseBody()
    await new Promise(resolve=>setTimeout(resolve,30))
    expect(events).toBe(before)
    expect(probe.state).toBe("unavailable")
  } finally {if(old===undefined) delete process.env.OPEN_CLANK_MANAGED;else process.env.OPEN_CLANK_MANAGED=old}
})
