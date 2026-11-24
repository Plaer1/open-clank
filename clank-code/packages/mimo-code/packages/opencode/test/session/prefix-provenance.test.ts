import { expect, test } from "bun:test"
import { writeFile } from "node:fs/promises"
import path from "node:path"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { Instruction } from "../../src/session/instruction"
import { SessionPrefixSnapshot } from "../../src/session/prefix-snapshot"
import * as Provenance from "../../src/session/prefix-provenance"
import { MessageID } from "../../src/session/schema"
import { AppRuntime } from "../../src/effect/app-runtime"
import { tmpdir } from "../fixture/fixture"
import { Database } from "../../src/storage"

test("actual instruction receipt and stored prefix do not reuse incomplete authority", async () => {
  await using tmp = await tmpdir()
  await writeFile(path.join(tmp.path, "AGENTS.md"), "same instruction\n")
  await Instance.provide({directory:tmp.path, fn:async () => {
    const session = await AppRuntime.runPromise(Session.Service.use((service) => service.create({})))
    const source = await AppRuntime.runPromise(Instruction.Service.use((service) => service.system()))
    expect(source.manifest?.some((item) => item.kind === "file" && item.state === "present" && item.digest === Provenance.digest("same instruction\n"))).toBe(true)
    const first = Provenance.build({system:["same"],toolsHash:Provenance.digest("tools"),instructions:source.manifest ?? [],policy:"policy-a"})
    const changed = Provenance.build({system:["same"],toolsHash:Provenance.digest("tools"),instructions:source.manifest ?? [],policy:"policy-b"})
    const key = "fixture-profile"
    const saved = await AppRuntime.runPromise(SessionPrefixSnapshot.pin({sessionID:session.id,profileKey:key,system:["same"],toolsHash:Provenance.digest("tools"),tools:[],provenance:first,watermarkMessageID:MessageID.ascending()}))
    expect(saved.provenance).toEqual(first)
    expect(first.system).toEqual(changed.system)
    expect(first.policy).not.toBe(changed.policy)
    expect(Provenance.reusable(saved.provenance, changed)).toBe(false)
    expect(Provenance.reusable(saved.provenance, undefined)).toBe(false) // fork/compaction without current proof
    expect(Provenance.reusable(null, first)).toBe(false)
    const nextHash = Provenance.digest("changed schema")
    const next = Provenance.build({system:["second instruction"],toolsHash:nextHash,instructions:source.manifest ?? [],policy:"policy-b"})
    const second = await AppRuntime.runPromise(SessionPrefixSnapshot.rotate({sessionID:session.id,profileKey:key,system:["second instruction"],toolsHash:nextHash,tools:[],provenance:next,watermarkMessageID:MessageID.ascending()}))
    const captured = await AppRuntime.runPromise(SessionPrefixSnapshot.get(session.id,key))
    expect(second.revision).toBe(2)
    expect(captured?.system).toEqual(["second instruction"])
    expect(captured?.tools_hash).toBe(nextHash)
    expect(captured?.provenance).toEqual(next)
    const route = {providerID:"provider",modelID:"model",connectionID:"connection",billingLane:"api",modelRouteID:"route",
      accountID:"account",credentialRevision:1,grantID:"grant",grantRevision:1,methodID:"anthropic.claude_subscription",methodPolicyRevision:1,
      catalogRevision:1,mappingRevision:1,workspaceRevision:1,bindingID:"binding",bindingRevision:1,committed:false,methodApplicability:"required" as const,grantApplicability:"shared" as const}
    const complete = Provenance.build({system:["second instruction"],toolsHash:nextHash,instructions:source.manifest ?? [],policy:"policy-b",
      agent:{name:"build",id:"main"},route,catalog:"catalog",registryGeneration:"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      pluginGeneration:{plugins:"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",fileHooks:"cccccccc-cccc-4ccc-8ccc-cccccccccccc"},
      mcpPresent:false,transformProfile:"provider-schema-transform-v1",systemHook:"fresh_built",sourceComplete:true})
    expect(complete.state).toBe("complete")
    expect(Provenance.reusable(complete, complete)).toBe(true)
    expect(Provenance.reusable(complete, {...complete,policy:Provenance.digest("changed policy")})).toBe(false)
    const unknown = Provenance.build({system:["second instruction"],toolsHash:nextHash,instructions:source.manifest ?? [],policy:"policy-b",
      agent:{name:"build",id:"main"},route:{...route,grantID:null,grantRevision:null,methodID:null,methodPolicyRevision:null,methodApplicability:"unknown",grantApplicability:"unknown"},catalog:"catalog",
      registryGeneration:"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",pluginGeneration:{plugins:"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",fileHooks:"cccccccc-cccc-4ccc-8ccc-cccccccccccc"},
      mcpPresent:false,transformProfile:"provider-schema-transform-v1",systemHook:"fresh_built",sourceComplete:true})
    expect(unknown.state).toBe("incomplete")
    const personal = {...complete, route:{...route,providerID:"openai",grantID:null,grantRevision:null,methodID:null,methodPolicyRevision:null,
      methodApplicability:"not_applicable" as const,grantApplicability:"none" as const}}
    expect(Provenance.reusable(personal,personal)).toBe(true)
    const emptyReceipt = await AppRuntime.runPromise(SessionPrefixSnapshot.pin({sessionID:session.id,profileKey:"nullable",system:["same"],toolsHash:Provenance.digest("tools"),tools:[],watermarkMessageID:MessageID.ascending()}))
    expect(emptyReceipt.provenance).toBeNull()
    const admitted = await AppRuntime.runPromise(SessionPrefixSnapshot.rotate({sessionID:session.id,profileKey:"nullable",system:["same"],toolsHash:Provenance.digest("tools"),tools:[],provenance:first,watermarkMessageID:MessageID.ascending()}))
    expect(admitted.revision).toBe(2)
    expect(admitted.provenance).toEqual(first)
    // The isolated test DB also exercises the recognized legacy shape. This
    // fixture-only DDL is not performed by the application on an existing DB.
    Database.use((db) => db.run("ALTER TABLE session_prefix_snapshot DROP COLUMN provenance"))
    const old = await AppRuntime.runPromise(SessionPrefixSnapshot.pin({sessionID:session.id,profileKey:"legacy",system:["same"],toolsHash:Provenance.digest("tools"),tools:[],provenance:first,watermarkMessageID:MessageID.ascending()}))
    expect(old.provenance).toBeNull()
    expect(old.system).toEqual(["same"])
    const oldAgain = await AppRuntime.runPromise(SessionPrefixSnapshot.rotate({sessionID:session.id,profileKey:"legacy",system:["same"],toolsHash:Provenance.digest("tools"),tools:[],provenance:first,watermarkMessageID:MessageID.ascending()}))
    expect(oldAgain.revision).toBe(old.revision)
  }})
}, 20000)
