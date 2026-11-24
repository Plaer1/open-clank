import { describe, expect, test } from "bun:test"
import {
  CLAUDE_SUBSCRIPTION_REQUEST_PROFILE,
  profileClaudeSubscriptionRequest,
} from "../../src/plugin/claude-subscription-profile"

const context = {
  accountUUID: "acct-fixture-123",
  sessionID: "session-fixture-123",
  deviceID: "device-fixture-123",
  platform: "Linux",
  architecture: "x64",
  runtime: "bun",
  runtimeVersion: "1.3.14",
}

describe("Claude subscription request profile", () => {
  test("versions an SDK request copy, preserving text, tools, images, and cache controls", () => {
    const original = {
      messages: [{ role: "user", content: [
        { type: "text", text: "keep cch=00000 and x-anthropic-billing-header: user content" },
        { type: "image", source: { type: "base64", media_type: "image/png", data: "cG5n" } },
      ] }],
      model: "claude-fixture",
      max_tokens: 100,
      system: [
        { type: "text", text: "system instruction", cache_control: { type: "ephemeral" } },
        { type: "text", text: "x-anthropic-billing-header: cch=00000 user-authored text" },
      ],
      tools: [{ name: "lookup", input_schema: { type: "object" }, cache_control: { type: "ephemeral" } }],
      metadata: { caller_marker: "preserve" },
    }
    const before = structuredClone(original)
    const result = profileClaudeSubscriptionRequest(JSON.stringify(original), {
      "anthropic-beta": "custom-beta, fallback-credit-2026-06-01, interleaved-thinking-2025-05-14",
      "cache-control": "no-store",
      "content-length": "999",
    }, context)
    const body = JSON.parse(result.body)

    expect(result.profileId).toBe(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.id)
    expect(original).toEqual(before)
    expect(body.messages).toEqual(before.messages)
    expect(body.tools).toEqual(before.tools)
    expect(body.system.at(-2)).toEqual(before.system[0])
    expect(body.system.at(-1)).toEqual(before.system[1])
    expect(body.system.filter((item: any) => item.text === CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.identity)).toHaveLength(1)
    expect(body.system[0].text).toMatch(/^x-anthropic-billing-header: cch=[0-9a-f]{5};/)
    const checksumInput = new TextEncoder().encode(result.body.replace(/cch=[0-9a-f]{5}/, "cch=00000"))
    const expectedChecksum = (Bun.hash.xxHash64(checksumInput, CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.checksumSeed)
      & CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.checksumMask).toString(16).padStart(5, "0")
    expect(body.system[0].text).toContain(`cch=${expectedChecksum};`)
    expect(body.messages[0].content[0].text).toBe("keep cch=00000 and x-anthropic-billing-header: user content")
    expect(body.metadata.caller_marker).toBe("preserve")
    const userID = JSON.parse(body.metadata.user_id)
    expect(Object.keys(userID)).toEqual(["device_id", "session_id", "account_uuid"])
    expect(userID).toEqual({ device_id: context.deviceID, session_id: context.sessionID, account_uuid: context.accountUUID })
    expect(result.headers.get("cache-control")).toBe("no-store")
    expect(result.headers.get("anthropic-beta")).toContain("custom-beta")
    expect(result.headers.get("anthropic-beta")).toContain(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.toolsBeta)
    expect(result.headers.get("anthropic-beta")).not.toContain(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.thinkingBeta)
    expect(result.headers.get("anthropic-beta")).not.toContain(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.paidFallbackBeta)
    expect(result.headers.get("x-api-key")).toBeNull()
    expect(result.headers.get("content-length")).toBeNull()

    const reprofileInput = JSON.parse(result.body)
    reprofileInput.tools = []
    reprofileInput.thinking = { type: "enabled" }
    const changedContext = { ...context, accountUUID: "acct-fixture-456", sessionID: "session-fixture-456" }
    const reprofiled = profileClaudeSubscriptionRequest(
      JSON.stringify(reprofileInput), result.headers, changedContext,
    )
    const reprofiledBody = JSON.parse(reprofiled.body)
    expect(JSON.parse(reprofiledBody.metadata.user_id)).toEqual({
      device_id: context.deviceID,
      session_id: changedContext.sessionID,
      account_uuid: changedContext.accountUUID,
    })
    expect(reprofiled.headers.get("anthropic-beta")).not.toContain(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.toolsBeta)
    expect(reprofiled.headers.get("anthropic-beta")).toContain(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.thinkingBeta)
    const reprofileChecksumInput = new TextEncoder().encode(reprofiled.body.replace(/cch=[0-9a-f]{5}/, "cch=00000"))
    const reprofileChecksum = (Bun.hash.xxHash64(reprofileChecksumInput, CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.checksumSeed)
      & CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.checksumMask).toString(16).padStart(5, "0")
    expect(reprofiledBody.system[0].text).toContain(`cch=${reprofileChecksum};`)
    expect(reprofiledBody.messages[0].content[0].text).toBe("keep cch=00000 and x-anthropic-billing-header: user content")
  })

  test("conditional beta set, stable identity, and identity/billing prefix are idempotent", () => {
    const body = JSON.stringify({
      system: [{ type: "text", text: "keep me" }],
      messages: [{ role: "user", content: "hello" }],
      thinking: { type: "enabled", budget_tokens: 1000 },
    })
    const first = profileClaudeSubscriptionRequest(body, {}, context)
    const second = profileClaudeSubscriptionRequest(first.body, first.headers, context)
    const parsed = JSON.parse(first.body)
    const identity = JSON.parse(parsed.metadata.user_id)
    const secondParsed = JSON.parse(second.body)

    expect(first.headers.get("anthropic-beta")).toContain(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.thinkingBeta)
    expect(first.headers.get("anthropic-beta")).not.toContain(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.toolsBeta)
    expect(first.headers.get("anthropic-beta")).not.toContain(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.paidFallbackBeta)
    expect(parsed.system.filter((item: any) => item.text === CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.identity)).toHaveLength(1)
    expect(parsed.system.filter((item: any) => item.text.startsWith(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.billingPrefix))).toHaveLength(1)
    expect(JSON.parse(secondParsed.metadata.user_id)).toEqual(identity)
    expect(secondParsed.system.filter((item: any) => item.text === CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.identity)).toHaveLength(1)
    expect(secondParsed.system.filter((item: any) => item.text.startsWith(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.billingPrefix))).toHaveLength(1)
    expect(second.body).toBe(first.body)
  })

  test("retains a plain SDK system prompt string", () => {
    const result = profileClaudeSubscriptionRequest(JSON.stringify({ system: "keep original" }), {}, context)
    expect(JSON.parse(result.body).system.at(-1)).toEqual({ type: "text", text: "keep original" })
  })
})
