/** Pure candidate selection; callers must independently prove both producer and History sources. */
import { createHash } from 'node:crypto'
import { parseJsonInputWithSpans, type Node } from '../util/toon-subset'

export type BrowserSnapshotFrame = Readonly<{callID:string; frame:string; argsHash:string}>
type ObjectNode = Extract<Node, {k:'o'}>
const bytes=(s:string)=>Buffer.byteLength(s,'utf8')
const field=(node:ObjectNode,key:string)=>node.v.find(([name])=>name===key)?.[1]
const atom=(node:Node|undefined)=>node?.k==='a' ? node.v : undefined
const keys=(value:unknown, names:string[])=>!!value && typeof value==='object' && !Array.isArray(value) && Object.keys(value).sort().join(',')===names.slice().sort().join(',')
const sha=(s:string)=>createHash('sha256').update(s,'utf8').digest('hex')
type Snapshot={target:string; connection:string; navigation:number; observation:number; stdout:string}
function snapshot(text:string):Snapshot|undefined {
  if(bytes(text)>51200) return
  parseJsonInputWithSpans(text)
  const v=JSON.parse(text)
  if(!keys(v,['targetId','connectionGeneration','navigationGeneration','observationGeneration','origin','title','result','textCoverage']) ||
     !keys(v.result,['stdout','stderr','exit_code']) || !keys(v.textCoverage,['version','clipped','clippedStrings','omittedCharacters','upstreamCompleteness'])) return
  if(typeof v.targetId!=='string' || !v.targetId || typeof v.connectionGeneration!=='string' || !v.connectionGeneration ||
     !Number.isSafeInteger(v.navigationGeneration) || v.navigationGeneration<0 || !Number.isSafeInteger(v.observationGeneration) || v.observationGeneration<0 ||
     typeof v.origin!=='string' || typeof v.title!=='string' || typeof v.result.stdout!=='string' || v.result.stderr!=='' || v.result.exit_code!==0 ||
     v.textCoverage.version!==1 || v.textCoverage.clipped!==false || v.textCoverage.clippedStrings!==0 || v.textCoverage.omittedCharacters!==0 ||
     v.textCoverage.upstreamCompleteness!=='unknown') return
  return {target:v.targetId,connection:v.connectionGeneration,navigation:v.navigationGeneration,observation:v.observationGeneration,stdout:v.result.stdout}
}
/** Returns at most one smaller request and the two identities that must be qualified before sending. */
export function selectBrowserSnapshotRepeat(body:string, frames:readonly BrowserSnapshotFrame[], modelID:string):{body:string;baseCallID:string;targetCallID:string}|undefined {
  try {
    if(typeof body!=='string' || bytes(body)>51200 || !Array.isArray(frames) || frames.length<2 || frames.length>32 || !modelID) return
    const ids=new Set<string>()
    for(const f of frames) if(!f || typeof f.callID!=='string' || !f.callID || bytes(f.callID)>256 || ids.has(f.callID) || typeof f.frame!=='string' || !/^[0-9a-f]{64}$/.test(f.argsHash)) return; else ids.add(f.callID)
    const parsed=parseJsonInputWithSpans(body), root=parsed.root
    const allowed=new Set(['model','messages','stream','stream_options','max_tokens','max_completion_tokens','temperature','top_p','seed','store','tools','tool_choice','prompt_cache_key'])
    if(root.k!=='o' || root.v.some(([k])=>!allowed.has(k)) || atom(field(root,'model'))!==modelID || atom(field(root,'stream'))!==true) return
    const messages=field(root,'messages');if(messages?.k!=='l')return
    const seen=new Set<string>(), items:{id:string;content:string;node:Node;position:number;frame:BrowserSnapshotFrame;s:Snapshot}[]=[]
    const byID=new Map(frames.map(f=>[f.callID,f]))
    for(let i=0;i<messages.v.length;i++) {
      const m=messages.v[i]!;if(m.k!=='o' || atom(field(m,'role'))!=='tool')continue
      if(m.v.length!==3 || m.v.some(([k])=>!['role','tool_call_id','content'].includes(k)))return
      const id=atom(field(m,'tool_call_id')), node=field(m,'content'), content=atom(node)
      if(typeof id!=='string' || !id || bytes(id)>256 || seen.has(id) || typeof content!=='string' || !node)return
      seen.add(id);const frame=byID.get(id);if(!frame || frame.frame!==content)continue
      const s=snapshot(content);if(s)items.push({id,content,node,position:i,frame,s})
    }
    let best:{body:string;baseCallID:string;targetCallID:string}|undefined
    for(let t=1;t<items.length;t++) for(let b=0;b<t;b++) {
      const base=items[b]!,target=items[t]!
      if(base.frame.argsHash!==target.frame.argsHash || base.s.target!==target.s.target || base.s.connection!==target.s.connection || base.s.navigation!==target.s.navigation || base.s.observation>=target.s.observation || base.s.stdout!==target.s.stdout)continue
      const p=parseJsonInputWithSpans(target.content), n=p.root
      if(n.k!=='o')continue
      const result=field(n,'result');if(result?.k!=='o')continue
      const out=field(result,'stdout');if(!out)continue
      const span=p.spanOf(out),wire=parsed.spanOf(target.node);if(!span || !wire)continue
      const marker=`[Repeated sanitized Browser snapshot text; same bytes as tool call ${JSON.stringify(base.id)}, SHA-256 ${sha(base.s.stdout)}. This observation retains its own target and generations; original available in History.]`
      const replacement=target.content.slice(0,span.start)+JSON.stringify(marker)+target.content.slice(span.end)
      if(bytes(replacement)>=bytes(target.content))continue
      const verify=replacement.slice(0,span.start)+target.content.slice(span.start,span.end)+replacement.slice(span.start+JSON.stringify(marker).length)
      if(verify!==target.content)continue
      const next=body.slice(0,wire.start)+JSON.stringify(replacement)+body.slice(wire.end)
      if(bytes(next)>=bytes(body))continue
      parseJsonInputWithSpans(next)
      if(!best || bytes(next)<bytes(best.body))best={body:next,baseCallID:base.id,targetCallID:target.id}
    }
    return best
  } catch { return }
}
