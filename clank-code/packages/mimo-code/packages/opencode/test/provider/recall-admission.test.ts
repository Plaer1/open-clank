import { expect, test } from "bun:test"
import { createHash } from "node:crypto"
import { createOpenAI } from "@ai-sdk/openai"
import { generateText } from "ai"
import { convertMcpTool, takeRecallAttestation } from "../../src/mcp"
import { MessageV2 } from "../../src/session/message-v2"
import { ProviderTransform } from "../../src/provider"
import { recallToolConverted, recallToolFrame } from "../../src/session/context-inspection"
import { captureFetch, flushCaptures, installCaptureScope, withSdkDispatches } from "../../src/provider/logging-transport"

test("private recall result reaches the OpenAI chat wire unchanged before ACK", async () => {
  const text = '{"results":[{"content":"synthetic"}]}'
  const hash = createHash("sha256").update(text).digest("hex")
  const client = { callTool: async () => ({ content: [{type:"text",text}], _meta: {
    "com.xiaomi.mimo/recall-admission": {attestationID:"abcdefghijklmnop",resultSHA256:hash}, ordinary:"retained",
  } }) }
  const tool = convertMcpTool({name:"search",inputSchema:{type:"object",properties:{}}} as any, client as any, undefined, undefined, async () => "invocation")
  const result: any = await tool.execute!({}, {toolCallId:"call-1",messages:[],abortSignal:new AbortController().signal} as any)
  expect(result._meta).toEqual({ordinary:"retained"})
  const attestation = takeRecallAttestation(result)!
  expect(attestation.text).toBe(text)
  const source: any = [{info:{id:"message-1",role:"assistant",providerID:"p",modelID:"m",time:{created:0},tokens:{input:0,output:0,reasoning:0,cache:{read:0,write:0}}},parts:[{type:"tool",tool:"search",callID:"call-1",state:{status:"completed",input:{},output:text,time:{start:0,end:1},attachments:[]}}]}]
  const model: any = {id:"m",providerID:"p",api:{id:"m",npm:"@ai-sdk/openai"},capabilities:{input:{text:true},output:{text:true},attachment:false},options:{}}
  const converted = await MessageV2.toModelMessages(source,model)
  const candidate = recallToolConverted(source,converted,new Map([["call-1",{...attestation,messageID:"message-1"}]]))
  expect(candidate).toBeDefined()
  const frame = ProviderTransform.message(converted,model,{})
  expect(recallToolFrame(candidate,frame)).toBeDefined()
  expect(recallToolFrame(candidate,[{role:"tool",content:[{type:"tool-result",toolCallId:"call-1",output:{type:"text",value:"rewritten"}}]}])).toBeUndefined()
  const old = process.env.OPEN_CLANK_MANAGED
  process.env.OPEN_CLANK_MANAGED = "1"
  const binding: any = {bindingID:"binding",bindingRevision:1,rootOperationID:"root",connectionID:"connection",providerID:"openai",billingLane:"metered_api",modelID:"m",credentialRequired:false,source:"keyless",attempt:1,committed:true}
  const admission: any = {admissionID:"a",policy:{revision:1,advanced_enabled:false,request_body_enabled:false,response_body_enabled:false,binary_body_enabled:false},context:{instanceID:"worker",providerID:"openai",operationID:"operation",rootOperationID:"root",bindingID:"binding",bindingRevision:1,identityCoverage:"complete",transportMode:"advanced_proxy",persistence:{numeric:true,content:false,reason:"normal"}}}
  const scope = installCaptureScope({} as any,binding,{async extMethod(){return {accepted:true,replayed:false}}} as any,admission)
  const receipts: unknown[] = []
  const fake = (async () => new Response(JSON.stringify({id:"x",object:"chat.completion",created:1,model:"m",choices:[{index:0,message:{role:"assistant",content:"ok"},finish_reason:"stop"}],usage:{prompt_tokens:1,completion_tokens:1,total_tokens:2}}),{status:200,headers:{"content-type":"application/json"}})) as unknown as typeof fetch
  const sdk = createOpenAI({apiKey:"synthetic",baseURL:"https://example.invalid/v1",fetch:captureFetch(fake,scope)})
  try {
    await withSdkDispatches(scope,[],() => generateText({model:sdk.chat("m"),messages:frame,maxRetries:0}),undefined,undefined,{sessionID:"session",rootOperationID:"root",actorID:"main",executionRevision:1,innerTurnID:"turn"},candidate,async receipt => {receipts.push(receipt)})
    await flushCaptures()
    expect(receipts).toHaveLength(1)
    expect(JSON.stringify(receipts)).not.toContain(text)
  } finally { if (old === undefined) delete process.env.OPEN_CLANK_MANAGED; else process.env.OPEN_CLANK_MANAGED = old }
})
