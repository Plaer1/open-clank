import { expect, test } from "bun:test"
import type { AccountSelection } from "../../src/provider/account-selection"
import {
  captureFetch,
  flushCaptures,
  installCaptureScope,
  captureScopeEvidence,
  withSdkDispatches,
  recordSdkFailure,
  takeSdkFailureReset,
  takeSdkCodexWindows,
  takeSdkFailureDispatch,
  observeCodexInnerSend,
} from "../../src/provider/logging-transport"
import type { ExtensionConnection, LoggingEventRequest, LoggingAdmissionResult } from "../../src/acp/openclank-protocol"
import { ManagedProvider } from "../../src/acp/managed-provider"

const binding = {
  bindingID: "binding",
  bindingRevision: 1,
  rootOperationID: "root",
  connectionID: "connection",
  providerID: "openai",
  billingLane: "metered_api",
  modelID: "selected",
  credentialRequired: false,
  source: "keyless",
  attempt: 1,
  committed: true,
} as AccountSelection.Binding

test('failure dispatch association binds exact response and Codex final inner send', async () => {
  const old=process.env.OPEN_CLANK_MANAGED; process.env.OPEN_CLANK_MANAGED='1'
  const connection={async extMethod(){throw new Error('capture disabled')}} as ExtensionConnection
  const anth=installCaptureScope({} as AccountSelection.Scope,{...binding,providerID:'anthropic',billingLane:'metered_api'} as AccountSelection.Binding,connection,null)
  const codex=installCaptureScope({} as AccountSelection.Scope,{...binding,providerID:'openai',billingLane:'subscription'} as AccountSelection.Binding,connection,null)
  const ids:string[]=[]; const other:string[]=[]; const error=Object.assign(new Error('429'),{statusCode:429})
  const response=(status:number,url:string)=>{const r=new Response(null,{status});Object.defineProperty(r,'url',{value:url});return r}
  try {
    await withSdkDispatches(anth,ids,()=>captureFetch((async()=>response(429,'https://api.anthropic.com/v1/messages')) as unknown as typeof fetch,anth)('https://api.anthropic.com/v1/messages',{method:'POST'}))
    recordSdkFailure(error,anth,[...ids,'extra']); expect(takeSdkFailureDispatch(error,anth)).toBeUndefined()
    recordSdkFailure(error,anth,ids); expect(takeSdkFailureDispatch(error,codex)).toBeUndefined()
    recordSdkFailure(error,anth,ids); expect(takeSdkFailureDispatch(error,anth)).toEqual({version:1,profile:'anthropic.messages.v1',captureDispatchID:ids[0],upstreamDispatchID:ids[0],httpStatus:429})
    const endpoint=new URL('https://chatgpt.com/backend-api/codex/responses'), request={method:'POST',body:'{}'}
    await withSdkDispatches(codex,other,()=>captureFetch((async()=>{
      await observeCodexInnerSend(endpoint,request,async()=>response(401,endpoint.href))
      return observeCodexInnerSend(endpoint,request,async()=>response(429,endpoint.href))
    }) as unknown as typeof fetch,codex)('https://example.invalid/v1/responses',request))
    recordSdkFailure(error,codex,other); const observed=takeSdkFailureDispatch(error,codex)
    expect(observed).toMatchObject({version:1,profile:'openai.codex.responses.v1',captureDispatchID:other[0],httpStatus:429})
    expect(observed?.upstreamDispatchID).not.toBe(other[0])
    expect(takeSdkFailureDispatch({},codex)).toBeUndefined()
  } finally { if(old===undefined) delete process.env.OPEN_CLANK_MANAGED; else process.env.OPEN_CLANK_MANAGED=old }
})

test("Anthropic request-rate reset requires one exact 429 dispatch even with capture off", async () => {
  const old = process.env.OPEN_CLANK_MANAGED
  process.env.OPEN_CLANK_MANAGED = "1"
  let attempt: Record<string, unknown> | undefined
  let revision = 1
  const selected = () => ({ ...binding, bindingRevision: revision, providerID: "anthropic", accountID: "account", credentialRequired: true,
    credentialRevision: 1, source: "round_robin", committed: false, scopeKind: "auxiliary_operation", operationID: "operation", modelRouteID: "route" })
  const connection: ExtensionConnection = { async extMethod(method, params) {
    if (method.endsWith('/account/bind')) return selected()
    if (method.endsWith('/credential/lease')) return { leaseID:'lease',connectionID:'connection',accountID:'account',credentialRevision:1,expiresAt:Date.now()+30_000,credential:{type:'api',key:'synthetic'} }
    if (method.endsWith('/account/attempt')) { attempt=params as Record<string, unknown>; revision++; return selected() }
    throw new Error("capture disabled")
  } }
  ManagedProvider.installHostConnection(connection as any)
  const at = new Date(Date.now() + 60_000).toISOString()
  const response = (date: string) => {
    const value = new Response(null, {status:429,headers:{"anthropic-ratelimit-requests-reset":date}})
    Object.defineProperty(value, 'url', {value:'https://api.anthropic.com/v1/messages'})
    return value
  }
  const fake = (async () => response(at)) as unknown as typeof fetch
  try {
    const route = { rootOperationID:'root', operationID:'operation', connectionID:'connection', providerID:'anthropic',billingLane:'metered_api',modelID:'selected',modelRouteID:'route' }
    await ManagedProvider.withOperation('session-rate', route, {providerID:'connection',modelID:'selected'}, async () => {
      const scope = ManagedProvider.requireScope('session-rate')
      const ids: string[] = []
      await withSdkDispatches(scope, ids, () => captureFetch(fake, scope)("https://api.anthropic.com/v1/messages"))
      const error = Object.assign(new Error("429"), {statusCode:429})
      recordSdkFailure(error, scope, [])
      expect(takeSdkFailureReset(error, scope)).toBeUndefined()
      recordSdkFailure(error, scope, [...ids, "another"])
      expect(takeSdkFailureReset(error, scope)).toBeUndefined()
      recordSdkFailure(error, scope, ids)
      await ManagedProvider.recordSessionAttempt('session-rate',error)
      expect(attempt?.evidence).toMatchObject({kind:'throttle',httpStatus:429,providerReset:{at,source:'anthropic.requests.rate-limit.v1',dispatchID:ids[0]}})
      expect(takeSdkFailureReset(error, scope)).toBeUndefined()
      const wrongHost: string[] = []
      await withSdkDispatches(scope, wrongHost, () => captureFetch(fake, scope)("https://proxy.invalid/v1/messages"))
      recordSdkFailure(error, scope, wrongHost)
      expect(takeSdkFailureReset(error, scope)).toBeUndefined()
      const badDate: string[] = []
      const malformed = (async () => response("2030-02-31T00:00:00Z")) as unknown as typeof fetch
      await withSdkDispatches(scope, badDate, () => captureFetch(malformed, scope)("https://api.anthropic.com/v1/messages"))
      recordSdkFailure(error, scope, badDate)
      expect(takeSdkFailureReset(error, scope)).toBeUndefined()
    })
  } finally {
    ManagedProvider.resetForTest()
    if (old === undefined) delete process.env.OPEN_CLANK_MANAGED
    else process.env.OPEN_CLANK_MANAGED = old
  }
})

test("Codex window evidence binds the final observed inner 429 to one SDK dispatch", async () => {
  const old=process.env.OPEN_CLANK_MANAGED; process.env.OPEN_CLANK_MANAGED="1"
  const scope=installCaptureScope({} as AccountSelection.Scope, {...binding,providerID:"openai",billingLane:"subscription"} as AccountSelection.Binding,
    {async extMethod(){throw new Error("capture disabled")}} as ExtensionConnection,null)
  const endpoint=new URL("https://chatgpt.com/backend-api/codex/responses"), request={method:"POST",body:'{}'}
  const run=async (headers:Record<string,string>) => {
    const ids:string[]=[]
    await withSdkDispatches(scope,ids,()=>captureFetch((async()=>observeCodexInnerSend(endpoint,request,async()=>{const r=new Response(null,{status:429,headers});Object.defineProperty(r,"url",{value:endpoint.href});return r})) as unknown as typeof fetch,scope)("https://example.invalid/v1/responses",request))
    return ids
  }
  try {
    const ids=await run({"x-codex-primary-used-percent":"100","x-codex-primary-window-minutes":"300","x-codex-primary-reset-at":String(Math.floor(Date.now()/1000)+60)})
    const error=Object.assign(new Error("429"),{statusCode:429})
    recordSdkFailure(error,scope,[...ids,"another"]);expect(takeSdkCodexWindows(error,scope)).toBeUndefined()
    recordSdkFailure(error,scope,ids);const observed=takeSdkCodexWindows(error,scope)
    expect(observed?.windows).toMatchObject([{window:"primary",usedPercent:100,windowMinutes:300}])
    expect(observed?.dispatchID).toBeString();expect(observed?.dispatchID).not.toBe(ids[0])
    const bad=await run({"x-codex-primary-used-percent":"NaN"});recordSdkFailure(error,scope,bad);expect(takeSdkCodexWindows(error,scope)).toBeUndefined()
    const absent=await run({});recordSdkFailure(error,scope,absent);expect(takeSdkCodexWindows(error,scope)).toBeUndefined()
  } finally { if(old===undefined) delete process.env.OPEN_CLANK_MANAGED; else process.env.OPEN_CLANK_MANAGED=old }
})

function admission(responseBody = true): LoggingAdmissionResult {
  return {
    admissionID: "admitted",
    policy: {
      revision: 1,
      advanced_enabled: true,
      request_body_enabled: true,
      response_body_enabled: responseBody,
      binary_body_enabled: false,
    },
    context: {
      instanceID: "worker",
      providerID: "openai",
      operationID: "operation",
      rootOperationID: "root",
      bindingID: "binding",
      bindingRevision: 1,
      identityCoverage: "complete",
      transportMode: "advanced_proxy",
      persistence: { numeric: true, content: true, reason: "normal" },
    },
  }
}

test("managed transport preserves split CRLF bytes and protocol terminal outcome; body-off and unsupported stay bounded", async () => {
  const old = process.env.OPEN_CLANK_MANAGED
  process.env.OPEN_CLANK_MANAGED = "1"
  const posted: LoggingEventRequest[] = []
  const connection: ExtensionConnection = {
    async extMethod(method, params) {
      if (method.endsWith("/admit")) throw new Error("fetch must use pinned admission")
      posted.push(params as unknown as LoggingEventRequest)
      return { accepted: true, replayed: false }
    },
  }
  let scope = installCaptureScope({} as AccountSelection.Scope, binding, connection, admission())
  try {
    const pieces = [
      'data: {"model":"actual","choices":[{"delta":{"content":"hi"}}]}\r',
      "\n\r",
      '\ndata: {"usage":{"prompt_tokens":12,"completion_tokens":3,"prompt_tokens_details":{"cached_tokens":2}},"choices":[{"finish_reason":"stop"}]}\r\n\r\ndata: [DONE]\r\n\r\n',
    ]
    let index = 0
    const fake = Object.assign(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            pull(controller) {
              if (index < pieces.length) controller.enqueue(new TextEncoder().encode(pieces[index++]))
            },
          }),
          { status: 200, headers: { "content-type": "text/event-stream" } },
        ),
      { preconnect: fetch.preconnect },
    ) as typeof fetch
    const response = await captureFetch(fake, scope)("https://example.invalid/chat/completions", {
      body: JSON.stringify({ password: "exclude", input_audio: { data: "YWJj", format: "wav" } }),
    })
    const reader = response.body!.getReader()
    let delivered = ""
    for (let i = 0; i < pieces.length; i++) delivered += new TextDecoder().decode((await reader.read()).value)
    await reader.cancel("SDK consumed DONE")
    await flushCaptures()
    expect(delivered).toBe(pieces.join(""))
    const terminal = posted.find((value) => value.terminal)!
    expect(terminal.outcome).toBe("completed")
    expect(terminal.metrics).toMatchObject({ input_tokens: 12, output_tokens: 3, cache_read_tokens: 2 })
    expect(terminal.actualModel).toBe("actual")
    expect(JSON.stringify(terminal.requestBody)).not.toContain("exclude")
    expect(JSON.stringify(terminal.requestBody)).toContain("embedded_media")
    posted.length = 0
    scope = installCaptureScope({} as AccountSelection.Scope, binding, connection, admission(false))
    const jsonFetch = Object.assign(
      async () =>
        new Response(
          JSON.stringify({
            choices: [{ message: { content: "normal JSON" } }],
            usage: { prompt_tokens: 2, completion_tokens: 1 },
          }),
          { headers: { "content-type": "application/json" } },
        ),
      { preconnect: fetch.preconnect },
    ) as typeof fetch
    const json = await captureFetch(jsonFetch, scope)("https://example.invalid/chat/completions")
    await json.text()
    await flushCaptures()
    const bodyOff = posted.find((value) => value.terminal)!
    expect(bodyOff.responseBody).toBeUndefined()
    expect(bodyOff.events).toBeUndefined()
    expect(bodyOff.metrics?.output_tokens).toBe(1)
    posted.length = 0
    scope = installCaptureScope({} as AccountSelection.Scope, binding, connection, admission())
    const unsupported = await captureFetch(jsonFetch, scope)("https://example.invalid/vendor/custom")
    await unsupported.text()
    await flushCaptures()
    const unmatched = posted.find((value) => value.terminal)!
    expect(unmatched.responseBody).toBeDefined()
    expect(unmatched.metrics).toMatchObject({ input_tokens: 2, output_tokens: 1 })
    expect(unmatched.normalizationProfile).toBeNull()
    expect(unmatched.lossReasons).toContain("parse_degraded")
    expect(captureFetch(jsonFetch, undefined)).toBe(jsonFetch)
  } finally {
    if (old === undefined) delete process.env.OPEN_CLANK_MANAGED
    else process.env.OPEN_CLANK_MANAGED = old
  }
})

test("timed out real logging RPCs keep capacity while provider responses continue", async () => {
  const old = process.env.OPEN_CLANK_MANAGED
  process.env.OPEN_CLANK_MANAGED = "1"
  let calls = 0
  const release: Array<(value: unknown) => void> = []
  const connection: ExtensionConnection = {
    extMethod() {
      calls++
      return new Promise((resolve) => release.push(resolve))
    },
  }
  const scope = installCaptureScope({} as AccountSelection.Scope, binding, connection, admission())
  const fake = Object.assign(async () => new Response("provider response"), {
    preconnect: fetch.preconnect,
  }) as typeof fetch
  try {
    const responses = await Promise.all(
      Array.from({ length: 48 }, async () =>
        (await captureFetch(fake, scope)("https://example.invalid/responses")).text(),
      ),
    )
    expect(responses.every((value) => value === "provider response")).toBe(true)
    expect(calls).toBeLessThanOrEqual(32)
    expect(captureScopeEvidence(scope).lossReasons).toContain("queue_full")
    await captureFetch(fake, scope)("https://example.invalid/responses")
    expect(calls).toBeLessThanOrEqual(32)
  } finally {
    for (const resolve of release) resolve({ accepted: true, replayed: false, captureState: "stored" })
    await Promise.resolve()
    if (old === undefined) delete process.env.OPEN_CLANK_MANAGED
    else process.env.OPEN_CLANK_MANAGED = old
  }
})

test("native text evidence needs unchanged positional mapping and successful transport", async () => {
  const {snapshotNativeText, reconcileNativeText, nativeSingleUserWire} = await import("../../src/session/context-inspection")
  const old = process.env.OPEN_CLANK_MANAGED; process.env.OPEN_CLANK_MANAGED = "1"
  const messages = [{info:{id:"m", role:"user"}, parts:[{id:"p",type:"text",text:"synthetic hello"}]}]
  const snapshot = snapshotNativeText(messages)
  expect(reconcileNativeText(snapshot, [{...messages[0],parts:[{...messages[0].parts[0],text:"rewritten"}]}]).sources).toHaveLength(0)
  const candidate = nativeSingleUserWire(reconcileNativeText(snapshot,messages), [{role:"user",content:[{type:"text",text:"synthetic hello"}]}])
  expect(candidate).toBeDefined()
  const connection: ExtensionConnection = {async extMethod() { return {accepted:true,replayed:false} }}
  const scope = installCaptureScope({} as AccountSelection.Scope,binding,connection,admission())
  const identity = {sessionID:"s",rootOperationID:"root",actorID:"main",executionRevision:1,innerTurnID:"turn"}
  const receipts: unknown[] = [], ids: string[] = []
  const fake = (async () => new Response(null,{status:200})) as unknown as typeof fetch
  try {
    await withSdkDispatches(scope,ids,() => captureFetch(fake,scope)("https://example.invalid/chat/completions",{method:"POST",body:JSON.stringify({messages:[{role:"user",content:"synthetic hello"}]})}),candidate,async receipt => {receipts.push(receipt)},identity)
    await flushCaptures()
    expect(receipts).toHaveLength(1)
    expect(JSON.stringify(receipts)).not.toContain("synthetic hello")
    const rejected: unknown[] = []
    await withSdkDispatches(scope,[],() => captureFetch(fake,scope)("https://example.invalid/chat/completions",{method:"POST",body:JSON.stringify({messages:[{role:"user",content:"changed"}]})}),candidate,async receipt => {rejected.push(receipt)},identity)
    await flushCaptures(); expect(rejected).toHaveLength(0)
    const denied: unknown[] = []
    const rejectFetch = (async () => new Response(null,{status:429})) as unknown as typeof fetch
    await withSdkDispatches(scope,[],() => captureFetch(rejectFetch,scope)("https://example.invalid/chat/completions",{method:"POST",body:JSON.stringify({messages:[{role:"user",content:"synthetic hello"}]})}),candidate,async receipt => {denied.push(receipt)},identity)
    await flushCaptures(); expect(denied).toHaveLength(0)
  } finally { if (old === undefined) delete process.env.OPEN_CLANK_MANAGED; else process.env.OPEN_CLANK_MANAGED = old }
})
