import {expect,test} from 'bun:test'
import {Effect} from 'effect'
import {createOpenAICompatible} from '@ai-sdk/openai-compatible'
import {streamText,tool} from 'ai'
import {z} from 'zod'
import {captureFinalTools,observeFinalToolWire} from '../../src/session/tool-definition-provenance'
import {createHash} from 'node:crypto'
import {MessageV2} from '../../src/session/message-v2'
import {snapshotSourceParts,layoutHash,sourceLayoutMessages} from '../../src/session/source-layout'
test('actual compatible SDK preserves attested layout and rejects changed wire',async()=>{
 const texts=['context header','context body','the exact original body']
 const parts=texts.map((text,i)=>({id:`part_${i}`,messageID:'msg',sessionID:'session',type:'text',text,synthetic:i<2})) as any
 const ignored={id:'route',messageID:'msg',sessionID:'session',type:'text',text:'',synthetic:true,ignored:true} as any
 const original=[...parts,ignored], source=snapshotSourceParts(original,'part_2')!
 expect(source.sourcePosition).toBe(2);expect(source.layout.length).toBe(3)
 const reminder={id:'reminder',messageID:'msg',sessionID:'session',type:'text',text:'owned reminder',synthetic:true} as any
 const sha=(v:string)=>createHash('sha256').update(v).digest('hex')
 const full=[...source.layout,{position:3,sha256:sha(reminder.text),synthetic:true}]
 expect(layoutHash(full)).not.toBe(source.layoutSHA256)
 const user={info:{id:'msg',sessionID:'session',role:'user',agent:'build',model:{providerID:'p',modelID:'m'},time:{created:1}},parts:[...original,reminder]} as any
 const model={id:'m',providerID:'p',api:{npm:'@ai-sdk/openai-compatible'},capabilities:{input:{image:false}}} as any
 const messages=await Effect.runPromise(MessageV2.toModelMessagesEffect([user],model))
 expect(sourceLayoutMessages(full,messages,false)).toBe(true)
 let body:any
 const tools=Object.fromEntries(Array.from({length:32},(_,i)=>[`tool_${i}`,tool({description:`Tool ${i}`,inputSchema:z.object({query:z.string()})})]))
 const attempt={}, names=Object.keys(tools);captureFinalTools(tools,names,attempt,true,async()=>true)
 const sdk=createOpenAICompatible({baseURL:'http://mock/v1',name:'fixture',fetch:(async (_url:any,init:any)=>{
   body=JSON.parse(init.body)
   return new Response('data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}\n\ndata: {"id":"x","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',{headers:{'content-type':'text/event-stream'}})
 }) as any})
 await streamText({model:sdk.languageModel('m'),messages:messages as any,tools}).text
 expect(sourceLayoutMessages(full,body.messages,true)).toBe(true)
 expect(await observeFinalToolWire(attempt,'dispatch','a'.repeat(64),body,()=>true)).toBe(true)
 const alteredTools=structuredClone(body);alteredTools.tools[0].function.description='changed'
 expect(await observeFinalToolWire(attempt,'dispatch','a'.repeat(64),alteredTools,()=>true)).toBe(false)
 const changed=structuredClone(body.messages);changed.at(-1).content[1].text='changed'
 expect(sourceLayoutMessages(full,changed,true)).toBe(false)
 const extra=structuredClone(body.messages);extra.at(-1).content.push({type:'text',text:'unknown'})
 expect(sourceLayoutMessages(full,extra,true)).toBe(false)
 const reordered=structuredClone(body.messages);[reordered.at(-1).content[0],reordered.at(-1).content[1]]=[reordered.at(-1).content[1],reordered.at(-1).content[0]]
 expect(sourceLayoutMessages(full,reordered,true)).toBe(false)
 expect(snapshotSourceParts([...original,parts[0]],'part_2')).toBeUndefined()
})
