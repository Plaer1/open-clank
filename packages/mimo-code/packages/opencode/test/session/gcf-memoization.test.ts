import {expect,test} from 'bun:test'
import {mkdtempSync} from 'node:fs'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
const root=mkdtempSync(join(tmpdir(),'gcf-memo-'))
process.env.OPEN_CLANK_DATA_DIR=root;process.env.ODYSSEUS_DATA_DIR=root
process.env.XDG_DATA_HOME=join(root,'data');process.env.XDG_CONFIG_HOME=join(root,'config')
process.env.XDG_CACHE_HOME=join(root,'cache');process.env.XDG_STATE_HOME=join(root,'state')
delete process.env.OPEN_CLANK_AGENT_HOME;delete process.env.THESIUS_AGENT_HOME

test('same admitted scope reuses encoder bytes, never source authority', async()=>{
 const {createGcfEncoder}=await import('../../src/session/gcf-native')
 const {encodeGcfAdmission,gcfViewIdentity,gcfMemoScope}=await import('../../src/session/gcf-policy')
 const {MessageV2}=await import('../../src/session/message-v2')
 const {issueGcfReceipt}=await import('../../src/session/gcf-source-proof')
 let calls=0;const body=Buffer.from('GCF profile=generic\n=0\n')
 const artifact=createGcfEncoder((input:Buffer)=>{calls++;return {state:'encoded',inputBytes:input.length,outputBytes:body.length,output:body}},'test','a'.repeat(64))
 const model={id:'model',providerID:'provider',api:{id:'model',url:'https://example.invalid',npm:'@ai-sdk/openai'},name:'test',capabilities:{temperature:true,reasoning:false,attachment:false,toolcall:true,input:{text:true,audio:false,image:false,video:false,pdf:false},output:{text:true,audio:false,image:false,video:false,pdf:false},interleaved:false},cost:{input:0,output:0,cache:{read:0,write:0}},limit:{context:1000,input:1000,output:100},status:'active',options:{},headers:{},release_date:'2026-01-01'} as any
 const output=JSON.stringify({records:Array.from({length:200},(_,i)=>({item:i}))})
 const messages=[{info:{id:'msg_a',sessionID:'session',role:'assistant',time:{created:0},parentID:'msg_u',modelID:'model',providerID:'provider',mode:'build',agent:'build',path:{cwd:'/',root:'/'},cost:0,tokens:{input:0,output:0,reasoning:0,cache:{read:0,write:0}}},parts:[{id:'part_a',sessionID:'session',messageID:'msg_a',type:'tool',callID:'call',tool:'read',state:{status:'completed',input:{},output,title:'',metadata:{truncated:false,mcp:{isError:false}},time:{start:1,end:2}}}]}] as any
 messages[0].parts[0]=issueGcfReceipt(messages[0].parts[0],(part:any)=>part)
 const input={version:1,sessionID:'session',rootOperationID:'root',generation:7,settingsRevision:'b'.repeat(64),requested:true,memoize:true,providerID:'provider',modelID:'model'}
 const admission=encodeGcfAdmission(input,'session','root',{providerID:'provider',modelID:'model'})!
 const policy=gcfViewIdentity(admission,'session','root',{providerID:'provider',modelID:'model'},artifact.identity())!
 const actor={sessionID:'session',actorID:'main',executionRevision:9}
 const scope=gcfMemoScope(admission,'account','session','root',actor)!
 const invoke=(memo?:string)=>MessageV2.toModelMessages(messages,model,{gcfViewIdentity:policy,gcfArtifact:artifact,gcfMemoScope:memo})
 expect(JSON.stringify(await invoke(scope))).toContain('Experimental lossless GCF')
 await invoke(scope);expect(calls).toBe(1)
 await invoke(gcfMemoScope(admission,'account2','session','root',actor));expect(calls).toBe(2)
 await invoke();await invoke();expect(calls).toBe(4)
 const missing=structuredClone(messages);missing[0].parts[0].state.output+=' '
 await MessageV2.toModelMessages(missing,model,{gcfViewIdentity:policy,gcfArtifact:artifact,gcfMemoScope:scope});expect(calls).toBe(4)
 const t04=await MessageV2.toModelMessages(messages,model,{gcfViewIdentity:policy,gcfArtifact:artifact,gcfMemoScope:scope,toolViews:new Map([['part_a','T04']])});expect(JSON.stringify(t04)).toContain('T04');expect(calls).toBe(4)
 expect(gcfMemoScope(admission,undefined,'session','root',actor)).toBeUndefined()
 const result=artifact.encode(Buffer.from(output),'gcf-generic-v3-safe-integers-v1',scope);if(result.state==='encoded')result.output.fill(0)
 await invoke(scope);expect(calls).toBe(4)
 for(let i=0;i<33;i++) artifact.encode(Buffer.from(output+i),'gcf-generic-v3-safe-integers-v1',scope)
 expect(calls).toBe(37)
 await invoke(scope);expect(calls).toBe(38) // The oldest entry was evicted.
 artifact.revoke();await invoke(scope);expect(calls).toBe(38);expect(artifact.availability()).toBe('unavailable')
})
