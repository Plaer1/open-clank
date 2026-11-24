import {expect,test} from 'bun:test'
import {mkdtempSync} from 'node:fs'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
const root=mkdtempSync(join(tmpdir(),'schema-annotations-'))
for(const [key,value] of Object.entries({OPEN_CLANK_DATA_DIR:'data',ODYSSEUS_DATA_DIR:'odysseus',FM_DB_PATH:'fm.db',XDG_CONFIG_HOME:'config',XDG_DATA_HOME:'xdg-data',XDG_CACHE_HOME:'cache',XDG_STATE_HOME:'state',MIMOCODE_DATA_DIR:'mimo',PYTHONPYCACHEPREFIX:'pycache'}))process.env[key]=join(root,value)
process.env.DATABASE_URL='sqlite:///'+join(root,'app.db')
delete process.env.OPEN_CLANK_AGENT_HOME;delete process.env.THESIUS_AGENT_HOME

test('resolved schema, final tool selection and raw fallback share the canonical owner',async()=>{
 const {tool,jsonSchema}=await import('ai')
 const {asSchema}=await import('@ai-sdk/provider-utils')
 const {SessionPrefixSnapshot}=await import('../../src/session/prefix-snapshot')
 const PrefixProvenance=await import('../../src/session/prefix-provenance')
 const {LLM}=await import('../../src/session/llm')
 const {selectAnnotationView}=await import('../../src/session/tool-schema-annotations')
 const validate=async(value:any)=>Number.isInteger(value?.x) ? {success:true as const,value} : {success:false as const,error:new Error('invalid')}
 const execute=async()=>({output:'ok'})
 const schema={type:'object',description:'private long documentation',properties:{description:{type:'string',description:'field help'},x:{type:'integer',description:'number help',examples:[1]}},required:['x'],additionalProperties:false,extension:{description:'unknown data'}} as any
 const questionSchema={type:'object',description:'question witness',properties:{x:{type:'integer'}},required:['x']} as any
 const definitions={native:tool({description:'native description',inputSchema:jsonSchema(schema,{validate}),execute}),question:tool({description:'question description',inputSchema:jsonSchema(questionSchema),execute}),hidden:tool({description:'hidden',inputSchema:jsonSchema(schema),execute})}
 const agent={permission:[{permission:'hidden',pattern:'*',action:'deny'}]} as any
 const selected=LLM.selectFinalTools({tools:definitions as any,activeTools:['native','question','hidden'],agent,permission:[],user:{tools:{}} as any})
 expect(selected.activeTools).toEqual(['native','question'])
 const resolved=await SessionPrefixSnapshot.resolveTools(selected.tools,selected.activeTools,{eligible:name=>name!=='question'})
 expect(resolved.canonicalSnapshots).toBeDefined();const snapshot=resolved.snapshots.find(x=>x.name==='native')!
 const advertised=await Promise.resolve(asSchema(resolved.tools.native!.inputSchema).jsonSchema)
 expect(advertised).toEqual(snapshot.input_schema)
 expect(SessionPrefixSnapshot.toolsHash(resolved.snapshots)).toBe(SessionPrefixSnapshot.toolsHash(resolved.snapshots.map(x=>x.name==='native'?{...x,input_schema:advertised}:x)))
 expect((advertised as any).properties.description.type).toBe('string')
 expect((advertised as any).properties.description.description).toBeUndefined()
 expect((advertised as any).extension.description).toBe('unknown data')
 expect((advertised as any).required).toEqual(['x'])
 expect(resolved.tools.question).toBe(resolved.canonicalTools!.question)
 expect(resolved.tools.native!.execute).toBe(execute)
 const validated=asSchema(resolved.tools.native!.inputSchema).validate!
 expect((await validated({x:3})).success).toBe(true);expect((await validated({x:'bad'})).success).toBe(false)
 const changed={...schema,description:'different source documentation'}
 const other=await SessionPrefixSnapshot.resolveTools({native:tool({description:'native description',inputSchema:jsonSchema(changed,{validate}),execute})},['native'],{eligible:()=>true})
 expect(other.snapshots[0]!.input_schema).toEqual(snapshot.input_schema)
 const digest=(raw:unknown)=>PrefixProvenance.digest(JSON.stringify({version:1,pre:raw,post:raw}))
 expect(digest(schema)).not.toBe(digest(changed))
 const profileValue=(raw:unknown)=>PrefixProvenance.build({system:[],toolsHash:SessionPrefixSnapshot.toolsHash(resolved.snapshots),instructions:[],policy:'policy',transformProfile:`provider-schema-transform-v1+schema-annotations-v1:${digest(raw)}`})
 expect(PrefixProvenance.valid(profileValue(schema))).toBe(true);expect(PrefixProvenance.sameReceipt(profileValue(schema),profileValue(changed))).toBe(false)
 let reanchors=0,current=true
 const guarded={...resolved.tools.native!,execute:async()=>({output:'guarded'})}
 const final={tools:{native:guarded,question:resolved.tools.question!},activeTools:['native','question']}
 const profile={names:['native','question'],canonicalTools:resolved.canonicalTools!,projectedSchemas:{native:guarded.inputSchema,question:resolved.tools.question!.inputSchema},current:async()=>current,reanchor:()=>{reanchors++}}
 expect((await selectAnnotationView(final,profile)).reanchored).toBe(false)
 current=false;const fallback=await selectAnnotationView(final,profile)
 expect(fallback.reanchored).toBe(true);expect(reanchors).toBe(1)
 expect(fallback.tools.native!.inputSchema).toBe(resolved.canonicalTools!.native!.inputSchema)
 expect(fallback.tools.native!.execute).toBe(guarded.execute)
 current=true;expect((await selectAnnotationView({...final,activeTools:['native']},profile)).reanchored).toBe(true)
 const off=await SessionPrefixSnapshot.resolveTools(selected.tools,selected.activeTools)
 expect(off.canonicalSnapshots).toBeUndefined();expect(off.snapshots[0]!.input_schema).toEqual(schema)
 const key={providerID:'p',modelID:'m',agent:'a',agentID:'main',harness:'default',systemMode:'append',system:'',permission:[]}
 expect(SessionPrefixSnapshot.profileKey(key)).not.toBe(SessionPrefixSnapshot.profileKey({...key,schemaViewIdentity:'schema-annotations-v1:'+ 'a'.repeat(64)}))
})
