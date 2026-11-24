import { createHash } from 'node:crypto'

/** Copy only bounded plain JSON before serialization, cloning or hashing. */
export function boundedOutlineSidecar(value:unknown, callID:string, output:string):string | undefined {
  try {
    if(typeof callID!=='string' || !callID || callID.length>256 || typeof output!=='string' || Buffer.byteLength(output,'utf8')>51200) return
    let count=0, size=0
    const active=new Set<object>()
    const copy=(v:unknown,depth:number):unknown=>{
      if (++count>16384 || depth>32) throw 0
      if (v===null || typeof v==='boolean') return v
      if (typeof v==='number') {if(!Number.isFinite(v)) throw 0;return v}
      if (typeof v==='string') {size+=Buffer.byteLength(v,'utf8');if(size>65536)throw 0;return v}
      if (!v || typeof v!=='object' || active.has(v)) throw 0
      const arr=Array.isArray(v), proto=Object.getPrototypeOf(v)
      if (!(arr ? proto===Array.prototype : proto===Object.prototype || proto===null)) throw 0
      active.add(v)
      const out:any=arr?[]:Object.create(null)
      if(arr && (v as unknown[]).length>16384) throw 0
      let arrayMembers=0
      for(const k in v as Record<string,unknown>){
        if(!Object.hasOwn(v,k)) continue
        size+=Buffer.byteLength(k,'utf8');if(size>65536)throw 0
        const descriptor=Object.getOwnPropertyDescriptor(v,k)
        if(!descriptor || !('value' in descriptor) || (arr && !/^(0|[1-9]\d*)$/.test(k)))throw 0
        if(arr) arrayMembers++
        out[k]=copy(descriptor.value,depth+1)
      }
      if(arr && (out.length!==(v as unknown[]).length || arrayMembers!==(v as unknown[]).length))throw 0
      active.delete(v);return out
    }
    const obj=copy(value,0) as Record<string,unknown>
    if(!obj || Array.isArray(obj) || Object.keys(obj).sort().join(',')!=='extraCost,outline,outputSHA256,toolCallID,version' ||
       obj.version!==1 || obj.toolCallID!==callID || obj.outputSHA256!==createHash('sha256').update(output,'utf8').digest('hex'))return
    const text=JSON.stringify(obj);if(Buffer.byteLength(text,'utf8')>65536)return
    return text
  } catch {return}
}
