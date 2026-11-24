import { isDeepStrictEqual } from "node:util"
import { asSchema } from "@ai-sdk/provider-utils"
import { Effect } from "effect"
import type { Tool as AITool } from "ai"
import type { MemoryDefinitionWitness } from "../tool/registry"

type RecordValue = {
  witness: MemoryDefinitionWitness
  owner: () => boolean
  schemaJSON: string
  inputSchema: AITool["inputSchema"]
  description: AITool["description"]
  execute: AITool["execute"]
}
const records = new WeakMap<object, RecordValue>()

// Only the actual registry-to-SDK constructor invokes this with its owner witness.
export function bindConstructedTool(value: AITool, schema: unknown, witness: MemoryDefinitionWitness, owner: () => boolean) {
  try {
    const schemaJSON = JSON.stringify(schema)
    if (!owner() || !schemaJSON || Buffer.byteLength(schemaJSON, "utf8") > 65536 || value.description !== witness.description) return
    records.set(value, { witness, owner, schemaJSON, inputSchema: value.inputSchema, description: value.description, execute: value.execute })
  } catch { /* Unavailable provenance must not change tool behavior. */ }
}

export function transferDetachedTool(source: AITool, target: AITool, schema: unknown) {
  try {
    const record = records.get(source)
    if (!record || !record.owner() || source.inputSchema !== record.inputSchema || source.description !== record.description ||
        source.execute !== record.execute || target.description !== record.description || target.execute !== record.execute ||
        JSON.stringify(schema) !== record.schemaJSON) return
    records.set(target, { ...record, inputSchema: target.inputSchema })
  } catch { /* A detached copy without an exact live source stays unqualified. */ }
}

/** Transfer only across the actual processor-owned execute guard, not restored JSON. */
export function transferGuardedToolDefinition(source: AITool, target: AITool) {
  try {
    const record=records.get(source)
    if (!record || !record.owner() || source.inputSchema!==record.inputSchema || source.description!==record.description ||
        source.execute!==record.execute || target.inputSchema!==record.inputSchema || target.description!==record.description ||
        typeof target.execute!=="function") return
    records.set(target,{...record,execute:target.execute})
  } catch { /* Optional evidence cannot change execution. */ }
}

export async function readToolDefinition(value: AITool) {
  try {
    const record = records.get(value)
    if (!record || !record.owner() || value.inputSchema !== record.inputSchema || value.description !== record.description ||
        value.execute !== record.execute || !(await Effect.runPromise(record.witness.current()))) return undefined
    return { schemaJSON: record.schemaJSON, description: record.description }
  } catch { return undefined }
}

// The final provider transform owns this record; it is not a tool-execution receipt.
const finalTools = new WeakMap<object, {tools: Record<string, AITool>; entries: Array<{name:string;value:AITool}>}>()
const wireTools = new WeakMap<object, Readonly<{dispatchID:string; bodySHA256:string}>>()
type OrdinaryEntry={name:string;value:AITool;input:AITool["inputSchema"];description:AITool["description"];execute:AITool["execute"]}
const ordinaryTools=new WeakMap<object,{tools:Record<string,AITool>;entries:OrdinaryEntry[];snapshot:Promise<unknown | undefined>;current:()=>Promise<boolean>}>()
async function ordinarySnapshot(entries:OrdinaryEntry[]):Promise<unknown | undefined> {
  try {
    const expected=await Promise.all(entries.map(async entry=>({type:'function',function:{name:entry.name,description:entry.description,parameters:await asSchema(entry.input).jsonSchema}})))
    const bytes=JSON.stringify(expected)
    if(!bytes || Buffer.byteLength(bytes,'utf8')>524288)return
    return JSON.parse(bytes)
  } catch {return}
}
export function captureFinalTools<T extends Record<string, AITool>>(tools: T, active: string[], attempt: object | undefined, layoutV2=false, definitionCurrent?:()=>Promise<boolean>): T {
  if (!attempt) return tools
  try {
    finalTools.delete(attempt); wireTools.delete(attempt); ordinaryTools.delete(attempt)
    if(layoutV2) {
      if(!definitionCurrent || active.length<1 || active.length>128 || new Set(active).size!==active.length || active.some(name=>!name || Buffer.byteLength(name,'utf8')>256 || !tools[name]))return tools
      const entries=active.map(name=>({name,value:tools[name],input:tools[name].inputSchema,description:tools[name].description,execute:tools[name].execute}))
      if(entries.some(entry=>!entry.input || (entry.description!==undefined && typeof entry.description!=='string')))return tools
      ordinaryTools.set(attempt,{tools,entries,snapshot:ordinarySnapshot(entries),current:definitionCurrent})
      return tools
    }
    if (active.length < 1 || active.length > 2 || new Set(active).size !== active.length ||
        active.some(name => name !== "memory" && name !== "question")) return tools
    const entries=active.map(name=>({name,value:tools[name]}))
    if (entries.some(entry=>!entry.value || records.get(entry.value)?.witness.id!==entry.name)) return tools
    finalTools.set(attempt, {tools, entries})
  } catch {}
  return tools
}
export async function observeFinalToolWire(attempt: object, dispatchID: string, bodySHA256: string, body: unknown, current: () => boolean) {
  try {
    const ordinary=ordinaryTools.get(attempt)
    if(ordinary) {
      const same=()=>current() && ordinaryTools.get(attempt)===ordinary && ordinary.entries.every(entry=>ordinary.tools[entry.name]===entry.value && entry.value.inputSchema===entry.input && entry.value.description===entry.description && entry.value.execute===entry.execute)
      if(!same() || !(await ordinary.current()) || !same())return
      const before=await ordinary.snapshot
      if(!before || !same())return
      const after=await ordinarySnapshot(ordinary.entries)
      if(!after || !same() || !(await ordinary.current()) || !same() || !isDeepStrictEqual(before,after) || !isDeepStrictEqual((body as {tools?:unknown})?.tools,before))return false
      wireTools.set(attempt,Object.freeze({dispatchID,bodySHA256}))
      return true
    }
    const owner=finalTools.get(attempt)
    if (!owner || owner.entries.some(entry=>owner.tools[entry.name]!==entry.value) || !current()) return
    const sources=await Promise.all(owner.entries.map(entry=>readToolDefinition(entry.value)))
    if (sources.some(source=>!source) || !current() || finalTools.get(attempt)!==owner ||
        owner.entries.some(entry=>owner.tools[entry.name]!==entry.value)) return
    const raw=body as {tools?:unknown}
    const expected=owner.entries.map((entry,index)=>({type:"function",function:{name:entry.name,description:sources[index]!.description,parameters:JSON.parse(sources[index]!.schemaJSON)}}))
    if (!isDeepStrictEqual(raw?.tools,expected)) return false
    wireTools.set(attempt,Object.freeze({dispatchID,bodySHA256}))
    return true
  } catch {}
}
// No consumer can turn this provisional observation into authority yet.
