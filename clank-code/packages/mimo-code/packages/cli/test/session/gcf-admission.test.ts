import { expect, test } from 'bun:test'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
const root=mkdtempSync(join(tmpdir(),'gcf-admission-'))
process.env.OPEN_CLANK_DATA_DIR=root
process.env.XDG_DATA_HOME=join(root,'data');process.env.XDG_CONFIG_HOME=join(root,'config')
process.env.XDG_CACHE_HOME=join(root,'cache');process.env.XDG_STATE_HOME=join(root,'state')
delete process.env.OPEN_CLANK_AGENT_HOME;delete process.env.THESIUS_AGENT_HOME

test('admitted preference changes only prefix identity; originals and inherited messages stay exact', async () => {
  const {encodeGcfAdmission,gcfViewIdentity}=await import('../../src/session/gcf-policy')
  const {SessionPrefixSnapshot}=await import('../../src/session/prefix-snapshot')
  const {MessageV2}=await import('../../src/session/message-v2')
  const base={version:1,sessionID:'session',rootOperationID:'root',generation:7,settingsRevision:'a'.repeat(64),requested:true,providerID:'provider',modelID:'model'}
  const modelRef={providerID:'provider',modelID:'model'}
  const admitted=encodeGcfAdmission(base,'session','root',modelRef)!
  const policy=gcfViewIdentity(admitted,'session','root',modelRef)!
  expect(policy).toBeDefined()
  const next=encodeGcfAdmission({...base,rootOperationID:'next'},'session','next',modelRef)
  expect(gcfViewIdentity(next,'session','next',modelRef)).toBe(policy)
  const changed=encodeGcfAdmission({...base,settingsRevision:'b'.repeat(64)},'session','root',modelRef)
  expect(gcfViewIdentity(changed,'session','root',modelRef)).not.toBe(policy)
  expect(gcfViewIdentity(admitted,'session','other',modelRef)).toBeUndefined()
  expect(gcfViewIdentity(admitted,'session','root',{providerID:'provider',modelID:'other'})).toBeUndefined()
  expect(encodeGcfAdmission({...base,extra:true},'session','root',modelRef)).toBeUndefined()
  expect(gcfViewIdentity(encodeGcfAdmission({...base,requested:false},'session','root',modelRef),'session','root',modelRef)).toBeUndefined()
  const profile={providerID:'provider',modelID:'model',agent:'main',agentID:'main',harness:'default',systemMode:'append',system:'',permission:[]}
  expect(SessionPrefixSnapshot.profileKey(profile)).not.toBe(SessionPrefixSnapshot.profileKey({...profile,gcfViewIdentity:policy}))
  expect(SessionPrefixSnapshot.profileKey({...profile,gcfViewIdentity:policy})).toBe(SessionPrefixSnapshot.profileKey({...profile,gcfViewIdentity:policy}))
  const model={id:'model',providerID:'provider',api:{id:'model',url:'https://example.invalid',npm:'@ai-sdk/openai'},name:'test',
    capabilities:{temperature:true,reasoning:false,attachment:false,toolcall:true,input:{text:true,audio:false,image:false,video:false,pdf:false},output:{text:true,audio:false,image:false,video:false,pdf:false},interleaved:false},
    cost:{input:0,output:0,cache:{read:0,write:0}},limit:{context:1000,input:1000,output:100},status:'active',options:{},headers:{},release_date:'2026-01-01'} as any
  const output=JSON.stringify({records:Array.from({length:200},(_,i)=>({item:i}))})
  const input=[{info:{id:'msg_a',sessionID:'session',role:'assistant',time:{created:0},parentID:'msg_u',modelID:'model',providerID:'provider',mode:'build',agent:'build',path:{cwd:'/',root:'/'},cost:0,tokens:{input:0,output:0,reasoning:0,cache:{read:0,write:0}}},parts:[{id:'part_a',sessionID:'session',messageID:'msg_a',type:'tool',callID:'call',tool:'read',state:{status:'completed',input:{},output,title:'',metadata:{truncated:false,mcp:{_meta:{opaque:'preserved'}}},time:{start:1,end:2}}}]}] as any
  const {issueGcfReceipt}=await import('../../src/session/gcf-source-proof')
  const qualified=structuredClone(input)
  qualified[0].parts[0].state.metadata.mcp.isError=false
  qualified[0].parts[0]=issueGcfReceipt(qualified[0].parts[0],(part:any)=>part)
  const original=JSON.stringify(input)
  const baseline=await MessageV2.toModelMessages(input,model)
  const requested=await MessageV2.toModelMessages(input,model,{gcfViewIdentity:policy})
  expect(JSON.stringify(requested)).toBe(JSON.stringify(baseline))
  expect(JSON.stringify(input)).toBe(original)
  const artifact={availability:()=>"available",identity:()=>"fixture-codec",revoke:()=>{},encode:(bytes:Uint8Array)=>({state:"encoded",output:Buffer.from("GCF profile=generic\n=0\n"),inputBytes:bytes.byteLength,outputBytes:23})} as any
  const transformed=await MessageV2.toModelMessages(qualified,model,{gcfViewIdentity:policy,gcfArtifact:artifact})
  expect(JSON.stringify(transformed)).toContain("Experimental lossless GCF")
  const t04=await MessageV2.toModelMessages(qualified,model,{gcfViewIdentity:policy,gcfArtifact:artifact,toolViews:new Map([["part_a","selected T04 view"]])})
  expect(JSON.stringify(t04)).toContain("selected T04 view")
  expect(JSON.stringify(t04)).not.toContain("Experimental lossless GCF")
  expect(qualified[0].parts[0].state.output).toBe(output)
  const revoked={...artifact,availability:()=>"unavailable",identity:()=>undefined}
  const fallback=await MessageV2.toModelMessages(qualified,model,{gcfViewIdentity:policy,gcfArtifact:revoked})
  expect(JSON.stringify(fallback)).not.toContain("Experimental lossless GCF")
  expect(JSON.stringify(fallback)).toContain(output.replaceAll('"','\\"'))
  const inherited=structuredClone(baseline)
  const fork=[...inherited,...requested]
  expect(JSON.stringify(fork.slice(0,inherited.length))).toBe(JSON.stringify(inherited))
})
