import { expect, spyOn, test } from "bun:test"
import { createHash } from "node:crypto"
import { contextFrame, mediaSourceEvidence, projectContextInspection, sourceEvidence } from "../../src/session/context-inspection"
import { Effect } from "effect"
import { Server } from "../../src/server/server"
import { Instance } from "../../src/project/instance"
import { ManagedProvider } from "../../src/acp/managed-provider"
import { mintOriginalPrompt, currentOriginalPromptMessage } from "../../src/acp/original-prompt"
import { MessageV2 } from "../../src/session/message-v2"
import { MessageID } from "../../src/session/schema"
import { tmpdir } from "../fixture/fixture"
import { TestLLMServer } from "../lib/llm-server"
import { createOpencodeClient } from "@mimo-ai/sdk/v2"

function projection(info:any, parts:any[]) {
  const sort=(value:any):any => Array.isArray(value) ? value.map(sort) : value && typeof value==="object"
    ? Object.fromEntries(Object.entries(value).filter(([,v])=>v!==undefined).sort(([a],[b])=>a<b?-1:a>b?1:0).map(([k,v])=>[k,sort(v)])) : value
  return JSON.stringify(sort({id:info.id,sessionID:info.sessionID,actorID:info.agentID ?? "main",role:info.role,
    parts:parts.map(({metadata,...part})=>part)}))
}

test("busy /message binds the winning original input and rejects a replay",()=>Effect.runPromise(
  Effect.gen(function* () {
    const llm=yield* TestLLMServer
    yield* Effect.promise(async()=>{
      await using tmp=await tmpdir({config:{enabled_providers:["alibaba"],provider:{alibaba:{options:{apiKey:"test",baseURL:llm.url}}},
        model:"alibaba/qwen-plus",small_model:"alibaba/qwen-plus",agent:{build:{model:"alibaba/qwen-plus"}}}})
      let live=true
      const spy=spyOn(ManagedProvider,"captureOriginalPromptDelivery").mockImplementation(()=>()=>live)
      const app=Server.Default().app, query=`?directory=${encodeURIComponent(tmp.path)}`
      const post=(path:string,body:unknown,token?:string)=>app.request(path+query,{method:"POST",headers:{"Content-Type":"application/json",...(token?{"x-openclank-original-prompt":token}:{})},body:JSON.stringify(body)})
      const gate=Promise.withResolvers<void>()
      try {
        const created=await post("/session",{title:"origin fixture"});expect(created.status).toBe(200)
        const session=await created.json() as {id:string}
        await Effect.runPromise(llm.hold("first",gate.promise))
        const firstID=MessageID.ascending()
        const firstBody={messageID:firstID,model:{providerID:"alibaba",modelID:"qwen-plus"},parts:[{type:"text",text:"first"}]}
        const firstToken=mintOriginalPrompt(session.id,"root-first",{body:firstBody,directory:tmp.path})!
        const first=await post(`/session/${session.id}/message`,firstBody,firstToken.token)
        expect(first.status).toBe(200)
        const deadline=Date.now()+10000
        while((await Effect.runPromise(llm.calls))<1){if(Date.now()>deadline)throw Error("first turn did not start");await Bun.sleep(10)}
        const firstActual=MessageV2.get({sessionID:session.id as any,messageID:firstID})
        expect(currentOriginalPromptMessage(session.id,"root-first",firstID,projection(firstActual.info,firstActual.parts.filter((part)=>part.type!=="text" || !part.synthetic)))).toBe(true)
        const body={model:{providerID:"alibaba",modelID:"qwen-plus"},parts:[{type:"text",text:"original"}]}
        const issued=mintOriginalPrompt(session.id,"root",{body,directory:tmp.path});expect(issued).toBeDefined()
        const sdk=createOpencodeClient({baseUrl:"http://local.invalid",fetch:(request)=>app.fetch(request)})
        const response=await sdk.session.prompt({sessionID:session.id,directory:tmp.path,...body} as any,
          {headers:{"x-openclank-original-prompt":issued!.token}})
        expect(response.response.status).toBe(202)
        const accepted=response.data as {info:{id:string};parts:any[];receiptId:string}
        expect(accepted.receiptId).toBeTruthy()
        const actual=MessageV2.get({sessionID:session.id as any,messageID:accepted.info.id as any})
        const snapshot=projection(actual.info,actual.parts)
        expect(currentOriginalPromptMessage(session.id,"root",accepted.info.id,snapshot)).toBe(true)
        expect(JSON.stringify(accepted)).not.toContain(issued!.token)
        expect(currentOriginalPromptMessage(session.id,"root",accepted.info.id,snapshot+"changed")).toBe(false)
        const replay=await post(`/session/${session.id}/message`,{...body,parts:[{type:"text",text:"replay"}]},issued!.token)
        expect(replay.status).toBe(202)
        const other=await replay.json() as {info:{id:string};parts:any[]}
        expect(currentOriginalPromptMessage(session.id,"root",other.info.id,projection(other.info,other.parts))).toBe(false)
        const imageBody={...body,parts:[{type:"text",text:"with image"},
          {type:"file",mime:"image/png",filename:"tiny.png",url:"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+nm2QAAAAASUVORK5CYII="}]}
        const imageToken=mintOriginalPrompt(session.id,"root-image",{body:imageBody,directory:tmp.path})!
        const imageResult=await sdk.session.prompt({sessionID:session.id,directory:tmp.path,...imageBody} as any,
          {headers:{"x-openclank-original-prompt":imageToken.token}})
        expect(imageResult.response.status).toBe(202)
        const imageAccepted=imageResult.data as {info:{id:string};parts:any[]}
        const imageActual=MessageV2.get({sessionID:session.id as any,messageID:imageAccepted.info.id as any})
        expect(imageActual.parts.some((part)=>part.type==="file" && part.mime==="image/png")).toBe(true)
        const byID=new Map(imageActual.parts.map((part)=>[part.id,part]))
        expect(byID.size).toBe(imageActual.parts.length)
        const originalOrder=imageAccepted.parts.map((part)=>byID.get(part.id))
        expect(originalOrder.every(Boolean)).toBe(true)
        expect(currentOriginalPromptMessage(session.id,"root-image",imageAccepted.info.id,projection(imageActual.info,originalOrder))).toBe(true)
        const file=imageActual.parts.find((part)=>part.type==='file' && part.mime==='image/png') as any
        const bytes=Buffer.from(file.url.split(',')[1],'base64'), digest=createHash('sha256').update(bytes).digest('hex')
        const offer={version:1 as const,rootOperationID:'root-image',items:[{uploadID:'upload',sourceSHA256:digest,stage:'prepared' as const,reason:null,providerDelivery:'unknown' as const}]}
        const current=()=>currentOriginalPromptMessage(session.id,'root-image',imageAccepted.info.id,projection(imageActual.info,originalOrder))
        const media=mediaSourceEvidence(offer,imageActual,current)
        expect(media).toEqual([{kind:'media_input',id:'upload',mediaStage:'native_accepted',providerDelivery:'unknown',mediaReason:null,digest,messageID:imageActual.info.id,partID:file.id}])
        const frame=contextFrame({identity:{sessionID:session.id},providerID:'p',modelID:'m',window:{hard:100,effective:90,usable:80,source:'config'},sequence:{attempt:1,transform:1},prompt:[],instructions:undefined,tools:[],...sourceEvidence([],undefined,undefined,media)})
        expect(projectContextInspection({version:1,frames:[frame],omittedFrameCount:0})?.frames[0].sources).toEqual(media)
        expect(mediaSourceEvidence({...offer,items:[...offer.items,{...offer.items[0],uploadID:'second'}]},imageActual,current).every(item=>item.mediaStage==='prepared')).toBe(true)
        expect(mediaSourceEvidence(offer,imageActual,()=>false)).toEqual([])
        let checks=0;expect(mediaSourceEvidence(offer,imageActual,()=>++checks===1)).toEqual([])
        imageToken.revoke()
        live=false;expect(currentOriginalPromptMessage(session.id,"root",accepted.info.id,snapshot)).toBe(false)
        issued!.revoke();firstToken.revoke();gate.resolve();await first.text()
      } finally {gate.resolve();spy.mockRestore();await Instance.disposeAll()}
    })
  }).pipe(Effect.provide(TestLLMServer.layer),Effect.scoped)),30000)
