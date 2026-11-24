import { createHash } from "node:crypto"
import { readFileSync, lstatSync, realpathSync, openSync, closeSync, fstatSync, readSync } from "node:fs"
import path from "node:path"

function fileDigest(filename: string, expectedSize: number): string {
  const before = lstatSync(filename)
  if (!before.isFile() || before.isSymbolicLink() || before.size !== expectedSize) throw new Error("Invalid GCF file")
  const fd = openSync(filename, "r")
  try {
    const opened = fstatSync(fd)
    if (!opened.isFile() || opened.size !== expectedSize || opened.dev !== before.dev || opened.ino !== before.ino) throw new Error("Invalid GCF file")
    const hash = createHash("sha256"), buffer = Buffer.alloc(65536)
    let total = 0
    for (;;) {
      const n = readSync(fd, buffer, 0, buffer.length, null)
      if (!n) break
      total += n
      if (total > expectedSize) throw new Error("Invalid GCF file")
      hash.update(buffer.subarray(0, n))
    }
    const after = fstatSync(fd), current = lstatSync(filename)
    if (total !== expectedSize || after.size !== expectedSize || after.mtimeMs !== opened.mtimeMs || after.ctimeMs !== opened.ctimeMs || current.isSymbolicLink() || current.dev !== opened.dev || current.ino !== opened.ino) throw new Error("Invalid GCF file")
    return hash.digest("hex")
  } finally { closeSync(fd) }
}

const STREAM_POLICY = "gcf-generic-v3-stream-safe-integers-v1"
type NativeStream = { push(input:Buffer):any; close():any; abort():any }
const streamOwners = new WeakMap<object, () => NativeStream | undefined>()
const GRAPH_STREAM_POLICY = "gcf-graph-stream-v1"
type NativeGraphStream = { push(input:Buffer):any; end():any; abort():any }
const graphStreamOwners = new WeakMap<object, () => NativeGraphStream | undefined>()
const graphDecodeOwners = new WeakMap<object, (input:Buffer) => Buffer | undefined>()

const decodeReasons = new Set(["invalid", "too_large", "too_deep", "too_many_values", "codec_failure"])
const bypassReasons = new Set(["too_small", "too_large", "invalid", "unsupported_number", "too_deep", "too_many_values", "duplicate_key", "not_smaller"])

// Each bootstrap receives its own revocable capability; no model-visible path.
export function createGcfArtifact(value: unknown) {
  let active = true
  if (value === undefined) return Object.freeze({ availability: () => "unavailable" as const, identity: () => undefined,
    encode: (input: Uint8Array, _policyID: string, _scope?: string) => ({ state:"bypass" as const, reason:"unavailable" as const, inputBytes:input.byteLength, outputBytes:input.byteLength }),
    previewEncodeAvailability: () => "unavailable" as const,
    previewEncode: (input: Uint8Array, _policyID: string) => ({ state:"bypass" as const, reason:"unavailable" as const, inputBytes:input.byteLength, outputBytes:input.byteLength }),
    decodeAvailability: () => "unavailable" as const,
    decode: (input: Uint8Array, _policyID: string) => ({ state:"refused" as const, reason:"unavailable" as const, inputBytes:input.byteLength }),
    revoke: () => { active = false } })
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid GCF artifact")
  const v = value as Record<string, unknown>
  if (Object.keys(v).sort().join(",") !== "abi,absolutePath,codecVersion,napi,sha256,size,target,version" || v.version !== 1 || v.abi !== 1 || v.napi !== 6 || typeof v.absolutePath !== "string" || typeof v.target !== "string" || typeof v.codecVersion !== "string" || !/^[0-9A-Za-z][0-9A-Za-z._+-]{0,127}$/.test(v.codecVersion) || typeof v.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(v.sha256) || !Number.isSafeInteger(v.size) || (v.size as number) < 1 || (v.size as number) > 268435456) throw new Error("Invalid GCF artifact")
  const executable = realpathSync(process.execPath)
  const root = path.dirname(executable)
  const leaf = path.join(root, "native", "openclank-gcf.node")
  if (lstatSync(path.join(root, "native")).isSymbolicLink() || lstatSync(leaf).isSymbolicLink() || realpathSync(leaf) !== leaf || v.absolutePath !== leaf) throw new Error("Invalid GCF artifact path")
  const provenance = JSON.parse(readFileSync(path.join(root, "provenance.json"), "utf8"))
  const engine = lstatSync(executable)
  if (!engine.isFile() || provenance.binary?.size !== engine.size || fileDigest(executable, engine.size) !== provenance.binary?.sha256) throw new Error("Invalid GCF engine identity")
  const expectedTarget = `${process.platform === "win32" ? "windows" : process.platform}-${process.arch === "x64" ? "x64" : process.arch === "arm64" ? "arm64" : "unsupported"}`
  const entries = provenance.native_artifacts
  const item = Array.isArray(entries) && entries.length === 1 ? entries[0] : undefined
  if (!["linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64", "windows-x64"].includes(expectedTarget) || provenance.schema_version !== 2 || provenance.target !== v.target || v.target !== expectedTarget || provenance.binary?.name !== path.basename(executable) || !item || Object.keys(item).sort().join(",") !== "abi,codecVersion,napi,path,sha256,size,target" || item.path !== "native/openclank-gcf.node" || item.target !== v.target || item.napi !== 6 || item.abi !== 1 || item.codecVersion !== v.codecVersion || item.sha256 !== v.sha256 || item.size !== v.size) throw new Error("Invalid GCF provenance")
  const stat = lstatSync(leaf)
  if (!stat.isFile() || stat.size !== v.size || fileDigest(leaf, v.size as number) !== v.sha256) throw new Error("Invalid GCF artifact integrity")
  const nativeModule = { exports: {} } as NodeModule
  process.dlopen(nativeModule, leaf)
  const native = nativeModule.exports as { codecInfoV1?: () => any; encodeV1?: (input: Buffer, policyID: string) => any; decodeV1?: (input: Buffer, policyID: string) => any; encodePreviewV1?: (input: Buffer, policyID: string) => any; streamInfoV1?: () => any; createGenericStreamV1?: (policyID:string) => NativeStream; graphStreamInfoV1?: () => any; createGraphStreamV1?: (policyID:string) => NativeGraphStream; decodeGraphStreamV1?: (input:Buffer, policyID:string) => any }
  if (!native || typeof native.codecInfoV1 !== "function" || typeof native.encodeV1 !== "function") throw new Error("Invalid GCF ABI")
  const info = native.codecInfoV1()
  if (!info || info.abi !== 1 || info.codecVersion !== v.codecVersion || !Array.isArray(info.policyIDs) || !info.policyIDs.includes("gcf-generic-v3-safe-integers-v1")) throw new Error("Invalid GCF ABI")
  const decode = Array.isArray(info.decodePolicyIDs) && info.decodePolicyIDs.includes("gcf-generic-v3-safe-integers-v1") && typeof native.decodeV1 === "function" ? native.decodeV1 : undefined
  const preview = Array.isArray(info.previewPolicyIDs) && info.previewPolicyIDs.includes("gcf-generic-v3-safe-integers-v1") && typeof native.encodePreviewV1 === "function" ? native.encodePreviewV1 : undefined
  let stream: ((policyID:string) => NativeStream) | undefined
  try {
    const details = typeof native.streamInfoV1 === "function" ? native.streamInfoV1() : undefined
    if (details && Object.keys(details).sort().join(",") === "policyIDs,version" && details.version === 1
      && Array.isArray(details.policyIDs) && details.policyIDs.length === 1 && details.policyIDs[0] === STREAM_POLICY
      && typeof native.createGenericStreamV1 === "function") stream = native.createGenericStreamV1
  } catch { /* Old or incompatible add-ons remain usable for their old operations. */ }
  let graphStream: ((policyID:string) => NativeGraphStream) | undefined
  let graphDecode: ((input:Buffer,policyID:string) => any) | undefined
  try {
    const details = typeof native.graphStreamInfoV1 === "function" ? native.graphStreamInfoV1() : undefined
    if (details && Object.keys(details).sort().join(",") === "policyIDs,version" && details.version === 1
      && Array.isArray(details.policyIDs) && details.policyIDs.length === 1 && details.policyIDs[0] === GRAPH_STREAM_POLICY
      && typeof native.createGraphStreamV1 === "function") {
      graphStream = native.createGraphStreamV1
      if (typeof native.decodeGraphStreamV1 === "function") graphDecode = native.decodeGraphStreamV1
    }
  } catch { /* An add-on without the graph stream only reports that direction unavailable. */ }
  return createGcfEncoder(native.encodeV1, v.codecVersion as string, v.sha256 as string, decode, preview, stream, graphStream, graphDecode)
}

/** Scoped encoder owner; production calls only after artifact verification. */
export function createGcfEncoder(encodeV1: (input:Buffer, policyID:string)=>any, codecVersion:string, sha256:string, decodeV1?: (input:Buffer, policyID:string)=>any, encodePreviewV1?: (input:Buffer, policyID:string)=>any, createStream?: (policyID:string)=>NativeStream, createGraphStream?: (policyID:string)=>NativeGraphStream, decodeGraphStream?: (input:Buffer,policyID:string)=>any) {
  let active = true
  let memo: Map<string, {input:Buffer;output:Buffer}> | undefined
  let memoBytes = 0
  const owner = Object.freeze({
    availability: () => active ? "available" as const : "unavailable" as const,
    identity: () => active ? JSON.stringify({codecVersion,sha256}) : undefined,
    encode: (input: Uint8Array, policyID: string, scope?: string) => {
      const inputBytes = input.byteLength
      if (!active || policyID !== "gcf-generic-v3-safe-integers-v1") return { state: "bypass" as const, reason: "unavailable" as const, inputBytes, outputBytes: inputBytes }
      if (typeof SharedArrayBuffer !== "undefined" && input.buffer instanceof SharedArrayBuffer) return { state: "bypass" as const, reason: "shared_input" as const, inputBytes, outputBytes: inputBytes }
      if (inputBytes > 51200) return { state: "bypass" as const, reason: "too_large" as const, inputBytes, outputBytes: inputBytes }
      let key: string | undefined
      if (scope && typeof scope === 'string' && Buffer.byteLength(scope,'utf8') <= 2048) {
        key = JSON.stringify([scope,policyID,codecVersion,sha256,createHash('sha256').update(input).digest('hex')])
        const found=memo?.get(key)
        if (found && found.input.equals(input) && found.output.length < inputBytes && found.output.length <= 51200) {
          memo!.delete(key); memo!.set(key,found)
          return {state:'encoded' as const,output:Buffer.from(found.output),inputBytes,outputBytes:found.output.length}
        }
      }
      try {
        const result = encodeV1(Buffer.from(input), policyID)
        if (result?.state === "encoded" && Buffer.isBuffer(result.output) && result.inputBytes === inputBytes && result.outputBytes === result.output.length && result.output.length <= 51200 && result.output.length < inputBytes) {
          if (key) {
            memo ??= new Map()
            const old=memo.get(key); if (old) {memoBytes-=old.input.length+old.output.length;memo.delete(key)}
            const entry={input:Buffer.from(input),output:Buffer.from(result.output)}
            memo.set(key,entry);memoBytes+=entry.input.length+entry.output.length
            while (memo.size > 32 || memoBytes > 524288) {const first=memo.keys().next().value!;const removed=memo.get(first)!;memo.delete(first);memoBytes-=removed.input.length+removed.output.length}
          }
          return { state: "encoded" as const, output: Buffer.from(result.output), inputBytes, outputBytes: result.output.length }
        }
        if (result?.state === "bypass" && typeof result.reason === "string" && bypassReasons.has(result.reason) && result.inputBytes === inputBytes && result.outputBytes === inputBytes)
          return { state: "bypass" as const, reason: result.reason, inputBytes, outputBytes: inputBytes }
      } catch { /* Preserve the original input on codec failure. */ }
      return { state: "bypass" as const, reason: "codec_failure" as const, inputBytes, outputBytes: inputBytes }
    },
    previewEncodeAvailability: () => active && encodePreviewV1 ? "available" as const : "unavailable" as const,
    previewEncode: (input: Uint8Array, policyID: string) => {
      const inputBytes = input.byteLength
      const bypass = (reason: string) => ({ state:"bypass" as const, reason, inputBytes, outputBytes:inputBytes })
      if (!active || !encodePreviewV1 || policyID !== "gcf-generic-v3-safe-integers-v1") return bypass("unavailable")
      if (typeof SharedArrayBuffer !== "undefined" && input.buffer instanceof SharedArrayBuffer) return bypass("shared_input")
      if (inputBytes > 51200) return bypass("too_large")
      try {
        const result = encodePreviewV1(Buffer.from(input), policyID)
        if (!active) return bypass("unavailable")
        if (result) {
          const keys = Object.keys(result).sort().join(","), state = result.state, reportedInput = result.inputBytes
          if (keys === "inputBytes,output,outputBytes,state" && state === "encoded" && reportedInput === inputBytes) {
            const candidate = result.output, reportedOutput = result.outputBytes
            if (Buffer.isBuffer(candidate) && reportedOutput === candidate.length && candidate.length <= 51200) {
              const output = Buffer.from(candidate)
              if (!active) return bypass("unavailable")
              return {state:"encoded" as const,output,inputBytes,outputBytes:output.length}
            }
          }
          if (keys === "inputBytes,outputBytes,reason,state" && state === "bypass" && reportedInput === inputBytes) {
            const reason = result.reason, reportedOutput = result.outputBytes
            if (typeof reason === "string" && bypassReasons.has(reason) && reportedOutput === inputBytes)
              return bypass(active ? reason : "unavailable")
          }
        }
      } catch { /* Preserve original text on explicit codec failure. */ }
      return bypass(active ? "codec_failure" : "unavailable")
    },
    decodeAvailability: () => active && decodeV1 ? "available" as const : "unavailable" as const,
    decode: (input: Uint8Array, policyID: string) => {
      const inputBytes = input.byteLength
      const refused = (reason: "unavailable" | "shared_input" | "too_large" | "invalid" | "too_deep" | "too_many_values" | "codec_failure") => ({state:"refused" as const,reason,inputBytes})
      if (!active || !decodeV1 || policyID !== "gcf-generic-v3-safe-integers-v1") return refused("unavailable")
      if (typeof SharedArrayBuffer !== "undefined" && input.buffer instanceof SharedArrayBuffer) return refused("shared_input")
      if (inputBytes > 51200) return refused("too_large")
      try {
        const result = decodeV1(Buffer.from(input), policyID)
        if (!active) return refused("unavailable")
        if (result) {
          const keys = Object.keys(result).sort().join(",")
          const state = result.state, reportedInput = result.inputBytes
          if (keys === "inputBytes,output,outputBytes,state" && state === "decoded" && reportedInput === inputBytes) {
            const candidate = result.output, reportedOutput = result.outputBytes
            if (Buffer.isBuffer(candidate) && reportedOutput === candidate.length && candidate.length <= 51200) {
              const output = Buffer.from(candidate)
              if (!active) return refused("unavailable")
              return {state:"decoded" as const,output,inputBytes,outputBytes:output.length}
            }
          }
          if (keys === "inputBytes,reason,state" && state === "refused" && reportedInput === inputBytes) {
            const reason = result.reason
            if (typeof reason === "string" && decodeReasons.has(reason))
              return refused(active ? reason as "invalid" | "too_large" | "too_deep" | "too_many_values" | "codec_failure" : "unavailable")
          }
        }
      } catch { /* Refuse; never dispatch partially decoded bytes. */ }
      return refused(active ? "codec_failure" : "unavailable")
    },
    revoke: () => { active = false; memo?.clear(); memo = undefined; memoBytes = 0 },
  })
  if (createStream) streamOwners.set(owner, () => active ? createStream(STREAM_POLICY) : undefined)
  if (createGraphStream) graphStreamOwners.set(owner, () => active ? createGraphStream(GRAPH_STREAM_POLICY) : undefined)
  if (decodeGraphStream) graphDecodeOwners.set(owner, (input) => {
    if (!active || input.length > 51200) return
    try {
      const result=decodeGraphStream(Buffer.from(input),GRAPH_STREAM_POLICY)
      if (!active || result?.state !== 'decoded' || !Buffer.isBuffer(result.output) || result.output.length > 51200) return
      return Buffer.from(result.output)
    } catch { return }
  })
  return owner
}

type GcfArtifactWithPreview = ReturnType<typeof createGcfArtifact>
export type GcfArtifact = Omit<GcfArtifactWithPreview, "previewEncode" | "previewEncodeAvailability"> &
  Partial<Pick<GcfArtifactWithPreview, "previewEncode" | "previewEncodeAvailability">>


function streamRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.getPrototypeOf(value) !== Object.prototype) throw new Error("Invalid stream event")
  const descriptors = Object.getOwnPropertyDescriptors(value)
  if (Reflect.ownKeys(descriptors).some((key) => typeof key !== "string" || !descriptors[key].enumerable || !("value" in descriptors[key]))) throw new Error("Invalid stream event")
  return Object.fromEntries(Object.entries(descriptors).map(([key, desc]) => [key, desc.value]))
}
function streamArray(value: unknown): unknown[] {
  if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype) throw new Error("Invalid stream event")
  const descriptors = Object.getOwnPropertyDescriptors(value)
  if (Reflect.ownKeys(descriptors).some((key) => typeof key !== "string" || (key !== "length" && (!/^(0|[1-9][0-9]*)$/.test(key) || !descriptors[key].enumerable || !("value" in descriptors[key]))))) throw new Error("Invalid stream event")
  if (value.length > 16384 || Object.keys(descriptors).length !== value.length + 1) throw new Error("Invalid stream event")
  return Array.from({length:value.length}, (_, i) => descriptors[String(i)].value)
}
function streamString(value: unknown): value is string { return typeof value === "string" && value.isWellFormed() }
function streamScalar(value: unknown): boolean {
  return value === null || typeof value === "boolean" || streamString(value) || (typeof value === "number" && Number.isSafeInteger(value) && !Object.is(value, -0))
}
function streamEvent(value: unknown): Buffer {
  const event = streamRecord(value), keys = Object.keys(event).sort().join(",")
  let clean: object
  if (event.op === "begin" && keys === "fields,name,op" && streamString(event.name)) {
    const fields = streamArray(event.fields)
    if (!fields.every(streamString)) throw new Error("Invalid stream event")
    clean = {op:"begin",name:event.name,fields}
  } else if (event.op === "row" && keys === "op,values") {
    const values = streamArray(event.values)
    if (!values.every(streamScalar)) throw new Error("Invalid stream event")
    clean = {op:"row",values}
  } else if (event.op === "end" && keys === "op") clean = {op:"end"}
  else if (event.op === "kv" && keys === "name,op,value" && streamString(event.name) && streamScalar(event.value)) clean = {op:"kv",name:event.name,value:event.value}
  else throw new Error("Invalid stream event")
  const bytes = Buffer.from(JSON.stringify(clean), "utf8")
  if (bytes.length > 51200) throw new Error("Stream event exceeds bounds")
  return bytes
}

/** Provisional chunks are not a complete frame until the final complete event. */
export async function* encodeGenericStream(events: AsyncIterable<unknown>, artifact: GcfArtifact, options: {signal?: AbortSignal} = {}) {
  const signal = options.signal
  const check = () => { if (signal?.aborted || artifact.availability() !== "available") throw new Error("Stream unavailable") }
  check()
  const stream = streamOwners.get(artifact)?.()
  if (!stream || typeof stream.push !== "function" || typeof stream.close !== "function" || typeof stream.abort !== "function") throw new Error("Stream unavailable")
  let iterator: AsyncIterator<unknown> | undefined, complete = false, returned = false, total = 0
  const copy = (result: any, state: "provisional" | "complete") => {
    if (!result || Object.keys(result).sort().join(",") !== "chunk,state" || result.state !== state || !Buffer.isBuffer(result.chunk)
      || (typeof SharedArrayBuffer !== "undefined" && result.chunk.buffer instanceof SharedArrayBuffer)) throw new Error("Invalid native stream result")
    total += result.chunk.length
    if (total > 51200) throw new Error("Stream exceeds bounds")
    return Buffer.from(result.chunk)
  }
  try {
    if (!events || typeof events[Symbol.asyncIterator] !== "function") throw new Error("Invalid stream source")
    iterator = events[Symbol.asyncIterator]()
    for (;;) {
      check()
      let listener: (() => void) | undefined
      const pending = Promise.resolve().then(() => iterator!.next())
      const next = signal ? Promise.race([pending, new Promise<never>((_, reject) => {
        listener = () => reject(new Error("Stream aborted")); signal.addEventListener("abort", listener, {once:true})
        if (signal.aborted) listener()
      })]) : pending
      let item: IteratorResult<unknown>
      try { item = await next } finally { if (signal && listener) signal.removeEventListener("abort", listener) }
      check()
      if (item.done) break
      const chunk = copy(stream.push(streamEvent(item.value)), "provisional")
      check()
      yield {state:"provisional" as const, chunk}
    }
    check()
    const chunk = copy(stream.close(), "complete")
    check()
    complete = true
    yield {state:"complete" as const, chunk}
  } finally {
    if (!complete) {
      try { stream.abort() } catch {}
      if (iterator && !returned && typeof iterator.return === "function") {
        returned = true
        try { Promise.resolve(iterator.return()).catch(() => {}) } catch {}
      }
    }
  }
}

/** Explicit independent graph decoder, bound to the same revocable verified artifact. */
export function decodeGraphStream(input: Uint8Array, artifact: GcfArtifact): {state:'decoded';output:Buffer} | {state:'refused'|'unavailable'} {
  if (artifact.availability()!=='available') return {state:'unavailable'}
  const decode=graphDecodeOwners.get(artifact)
  if (!decode) return {state:'unavailable'}
  if (input.byteLength>51200 || (typeof SharedArrayBuffer!=='undefined' && input.buffer instanceof SharedArrayBuffer)) return {state:'refused'}
  const output=decode(Buffer.from(input))
  if (artifact.availability()!=='available') return {state:'unavailable'}
  return output ? {state:'decoded',output} : {state:'refused'}
}

export function graphStreamAvailable(artifact: GcfArtifact): boolean { return artifact.availability() === "available" && graphStreamOwners.has(artifact) }

/** One typed Symbol/Edge record, serialized as a single JSON line; the native writer owns all other validation. */
function graphEvent(value: unknown): Buffer {
  const event = streamRecord(value), keys = Object.keys(event)
  if ((event.op !== "symbol" && event.op !== "edge") || !keys.every((key) => key === "op" || streamScalar(event[key]))) throw new Error("Invalid graph event")
  const bytes = Buffer.from(JSON.stringify(event) + "\n", "utf8")
  if (bytes.length > 51200) throw new Error("Graph event exceeds bounds")
  return bytes
}

/** Stateless graph stream: symbols, then edges between declared symbols. Chunks are provisional until the complete one. */
export async function* encodeGraphStream(events: AsyncIterable<unknown>, artifact: GcfArtifact, options: {signal?: AbortSignal} = {}) {
  const signal = options.signal
  const check = () => { if (signal?.aborted || artifact.availability() !== "available") throw new Error("Stream unavailable") }
  check()
  const stream = graphStreamOwners.get(artifact)?.()
  if (!stream || typeof stream.push !== "function" || typeof stream.end !== "function" || typeof stream.abort !== "function") throw new Error("Stream unavailable")
  let complete = false, total = 0
  const copy = (result: any, state: "provisional" | "complete") => {
    if (!result || Object.keys(result).sort().join(",") !== "chunk,state" || result.state !== state || !Buffer.isBuffer(result.chunk)
      || (typeof SharedArrayBuffer !== "undefined" && result.chunk.buffer instanceof SharedArrayBuffer)) throw new Error("Invalid native stream result")
    total += result.chunk.length
    if (total > 51200) throw new Error("Stream exceeds bounds")
    return Buffer.from(result.chunk)
  }
  try {
    if (!events || typeof events[Symbol.asyncIterator] !== "function") throw new Error("Invalid stream source")
    for await (const item of events) {
      check()
      const chunk = copy(stream.push(graphEvent(item)), "provisional")
      check()
      yield {state:"provisional" as const, chunk}
    }
    check()
    const chunk = copy(stream.end(), "complete")
    check()
    complete = true
    yield {state:"complete" as const, chunk}
  } finally {
    if (!complete) { try { stream.abort() } catch {} }
  }
}
