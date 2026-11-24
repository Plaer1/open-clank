import {createHash} from 'node:crypto'
import type {MessageV2} from './message-v2'

export type SourcePartSnapshot = Readonly<{id:string;ignored:boolean;synthetic:boolean;digest:string}>
export type SourceLayoutItem = Readonly<{position:number;sha256:string;synthetic:boolean}>
export type SourceLayout = Readonly<{ordered:readonly SourcePartSnapshot[];layout:readonly SourceLayoutItem[];sourcePosition:number;layoutSHA256:string}>
export type SourceGenerated = Readonly<{constructor:'skill-search-first.v1'|'skill-search-refresh.v1'|'host-session-env-reminder.v1'|'host-session-personal-env.v1';position:number;sha256:string}>
const sha=(text:string)=>createHash('sha256').update(text,'utf8').digest('hex')
export function layoutHash(items:readonly SourceLayoutItem[]):string {return sha(JSON.stringify(items.map(x=>({position:x.position,sha256:x.sha256,synthetic:x.synthetic}))))}
export function snapshotSourceParts(parts:readonly MessageV2.Part[],sourceID:string):SourceLayout|undefined {
  if(!parts.length || parts.length>128 || parts.some(p=>p.type!=='text') || new Set(parts.map(p=>p.id)).size!==parts.length)return
  const ordered:SourcePartSnapshot[]=[],layout:SourceLayoutItem[]=[];let sourcePosition=-1, visible=0
  for(const part of parts){if(part.type!=='text')return; const item={id:part.id,ignored:!!part.ignored,synthetic:!!part.synthetic,digest:sha(part.text)};ordered.push(item)
    if(!item.ignored){const position=layout.length;layout.push({position,sha256:item.digest,synthetic:item.synthetic});if(!item.synthetic){visible++;if(item.id===sourceID)sourcePosition=position}}}
  if(visible!==1 || sourcePosition<0 || !layout.length)return
  return {ordered,layout,sourcePosition,layoutSHA256:layoutHash(layout)}
}

/** Compare exact SDK or serialized Chat message text parts, never infer roles. */
export function sourceLayoutMessages(items:readonly SourceLayoutItem[], messages:unknown, wire:boolean):boolean {
  if(!Array.isArray(messages) || !messages.length || messages.length>544)return false
  let user:unknown
  for(const raw of messages){
    if(!raw || typeof raw!=="object" || Array.isArray(raw))return false
    const m=raw as {role?:unknown;content?:unknown}
    if(wire && Object.keys(m).sort().join(',')!=="content,role")return false
    if(m.role==="system" && user===undefined && typeof m.content==="string")continue
    if(m.role!=="user" || user!==undefined)return false
    user=m.content
  }
  const parts=typeof user==="string" ? [{type:"text",text:user}] : user
  if(!Array.isArray(parts) || parts.length!==items.length)return false
  return parts.every((part,i)=>{
    if(!part || typeof part!=="object" || Array.isArray(part) || part.type!=="text" || typeof part.text!=="string")return false
    const keys=Object.keys(part).sort().join(',')
    if(keys!=="text,type" && !(keys==="providerOptions,text,type" && part.providerOptions===undefined && !wire))return false
    return items[i].position===i && sha(part.text)===items[i].sha256
  })
}
