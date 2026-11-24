/** Optional, bounded SDK schema annotation projection; canonical schema stays owned by the tool. */
const MAX_DEPTH=32,MAX_NODES=4096,MAX_BYTES=65536
export function projectSchemaAnnotations(input:unknown):unknown | undefined {
  try {
    const maps=new Set(['properties','patternProperties','$defs','definitions','dependentSchemas'])
    const singles=new Set(['items','additionalProperties','additionalItems','contains','propertyNames','not','if','then','else','unevaluatedItems','unevaluatedProperties','contentSchema'])
    const arrays=new Set(['allOf','anyOf','oneOf','prefixItems'])
    const annotations=new Set(['description','examples','$comment'])
    let nodes=0
    const bounded=(v:unknown,depth:number):void=>{
      if(++nodes>MAX_NODES || depth>MAX_DEPTH)throw new Error('schema limit')
      if(v===null || typeof v==='string' || typeof v==='boolean')return
      if(typeof v==='number' && Number.isFinite(v))return
      if(Array.isArray(v)){for(const child of v)bounded(child,depth+1);return}
      if(typeof v!=='object' || Object.getPrototypeOf(v)!==Object.prototype)throw new Error('schema shape')
      for(const value of Object.values(v))bounded(value,depth+1)
    }
    bounded(input,0)
    const original=JSON.stringify(input)
    if(!original || Buffer.byteLength(original,'utf8')>MAX_BYTES)return
    const node=(v:unknown):unknown=>{
      if(typeof v==='boolean')return v
      if(!v || typeof v!=='object' || Array.isArray(v))throw new Error('unsupported schema')
      const result:Record<string,unknown>={}
      for(const [key,value] of Object.entries(v)) {
        if(annotations.has(key))continue
        if(maps.has(key) && value && typeof value==='object' && !Array.isArray(value)) {
          result[key]=Object.fromEntries(Object.entries(value).map(([name,child])=>[name,node(child)]))
        } else if(singles.has(key) && (typeof value==='boolean' || (!!value && typeof value==='object' && !Array.isArray(value)))) result[key]=node(value)
        else if(arrays.has(key) && Array.isArray(value)) result[key]=value.map(node)
        else result[key]=value
      }
      return result
    }
    const projected=node(input), rendered=JSON.stringify(projected)
    return Buffer.byteLength(rendered,'utf8')<Buffer.byteLength(original,'utf8') ? projected : undefined
  } catch {return}
}

/** Resolve optional view at the actual SDK boundary; guarded execute closures survive fallback. */
export async function selectAnnotationView<T extends {inputSchema:unknown;description?:string}>(
  selected:{tools:Record<string,T>;activeTools:string[]},
  profile:{names:string[];canonicalTools:Record<string,T>;projectedSchemas:Record<string,unknown>;current:()=>Promise<boolean>;reanchor:()=>void},
):Promise<{tools:Record<string,T>;activeTools:string[];reanchored:boolean}> {
  let current=false
  try {current=await profile.current()} catch { /* optional projection falls back */ }
  if (current && JSON.stringify(selected.activeTools)===JSON.stringify(profile.names) &&
      profile.names.every(name=>selected.tools[name]?.inputSchema===profile.projectedSchemas[name] &&
        selected.tools[name]?.description===profile.canonicalTools[name]?.description)) return {...selected,reanchored:false}
  profile.reanchor()
  const tools=Object.fromEntries(Object.entries(selected.tools).map(([name,value])=>[name,profile.canonicalTools[name]
    ? {...value,inputSchema:profile.canonicalTools[name]!.inputSchema,description:profile.canonicalTools[name]!.description} : value])) as Record<string,T>
  return {tools,activeTools:selected.activeTools,reanchored:true}
}
