/** Process-private equality association only. History retains and verifies its original signed bytes. */
import { createHash } from 'node:crypto'
import { types } from 'node:util'
const limit=51200
function wellFormed(s:string) {
  for(let i=0;i<s.length;i++){const c=s.charCodeAt(i);if(c>=0xd800&&c<=0xdbff){const n=s.charCodeAt(++i);if(!(n>=0xdc00&&n<=0xdfff))return false}else if(c>=0xdc00&&c<=0xdfff)return false}
  return true
}
/** Canonicalizes precisely JSON-representable values; object undefined properties follow JSON publication. */
export function sourcePartStructuralDigest(value:unknown):string|undefined {
  try {
    let count=0,size=0
    const active=new Set<object>()
    const add=(s:string)=>{size+=Buffer.byteLength(s,'utf8');if(size>limit)throw 0}
    const visit=(v:unknown,depth:number):unknown=>{
      if(++count>16384 || depth>64)throw 0
      if(v===null || typeof v==='boolean')return v
      if(typeof v==='string'){if(!wellFormed(v))throw 0;add(v);return v}
      if(typeof v==='number'){if(!Number.isFinite(v) || Object.is(v,-0) || (Number.isInteger(v) && !Number.isSafeInteger(v)))throw 0;return v}
      if(!v || typeof v!=='object' || types.isProxy(v) || active.has(v))throw 0
      const array=Array.isArray(v),proto=Object.getPrototypeOf(v)
      if(array ? proto!==Array.prototype : proto!==Object.prototype && proto!==null)throw 0
      const desc=Object.getOwnPropertyDescriptors(v), keys=Reflect.ownKeys(desc)
      if(keys.some(k=>typeof k!=='string'))throw 0
      active.add(v)
      if(array){
        const items=v as unknown[], out:unknown[]=[]
        if(items.length>16384 || keys.length!==items.length+1 || keys.some(k=>k!=='length' && !/^(0|[1-9][0-9]*)$/.test(k as string)))throw 0
        for(let i=0;i<items.length;i++){const d=desc[String(i)];if(!d || !d.enumerable || !('value' in d) || d.value===undefined)throw 0;out.push(visit(d.value,depth+1))}
        active.delete(v);return out
      }
      const out:Record<string,unknown>=Object.create(null)
      for(const name of (keys as string[]).sort()) {
        const d=desc[name]!
        if(!d.enumerable || !('value' in d))throw 0
        if(!wellFormed(name))throw 0
        add(name)
        if(d.value!==undefined)out[name]=visit(d.value,depth+1)
      }
      active.delete(v);return out
    }
    const canonical=JSON.stringify(visit(value,0))
    if(typeof canonical!=='string' || Buffer.byteLength(canonical,'utf8')>limit)return
    return createHash('sha256').update(canonical,'utf8').digest('hex')
  }catch{return}
}
export function signedSourceStructuralDigest(content:string):string|undefined {
  try {if(typeof content!=='string' || Buffer.byteLength(content,'utf8')>limit)return;return sourcePartStructuralDigest(JSON.parse(content))}catch{return}
}

/** Return the original ACK only when this exact current scope associates structurally. */
export function associateSourceAck<T extends {accountID:string;actorID:string;chatID:string;sessionID:string;messageID:string;partID:string}>(
  part:{id:string;sessionID:string;messageID:string}, actorID:string,
  retained:{generation:object;structuralDigest?:string;ack:T}|undefined, generation:object|undefined, chatID:string|undefined):T|undefined {
  const digest=sourcePartStructuralDigest(part)
  if(!retained || !digest || !retained.structuralDigest || digest!==retained.structuralDigest || !generation || generation!==retained.generation ||
      retained.ack.actorID!==actorID || retained.ack.sessionID!==part.sessionID || retained.ack.messageID!==part.messageID ||
      retained.ack.partID!==part.id || retained.ack.chatID!==chatID)return
  return {...retained.ack}
}
