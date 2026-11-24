import { expect, test } from "bun:test"
import { parseMimoEndpoint, resolveMimoEndpoint } from "../../src/provider/mimo-endpoint-policy"

test("MiMo endpoint policy accepts only documented lane endpoints", () => {
  expect(parseMimoEndpoint("https://api.xiaomimimo.com/v1", "metered_api")).toMatchObject({
    url: "https://api.xiaomimimo.com/v1",
    policy: "mimo-metered-api-v1",
  })
  for (const cluster of ["cn", "sgp", "ams"]) {
    expect(parseMimoEndpoint(`https://token-plan-${cluster}.xiaomimimo.com/v1`, "subscription")?.policy)
      .toBe("mimo-token-plan-v1")
  }
  for (const url of [
    "http://token-plan-cn.xiaomimimo.com/v1",
    "https://token-plan-cn.xiaomimimo.com:8443/v1",
    "https://token-plan-unknown.xiaomimimo.com/v1",
    "https://token-plan-cn.xiaomimimo.com/anthropic",
    "https://api.xiaomimimo.com/v1",
  ]) {
    expect(parseMimoEndpoint(url, "subscription")).toBeUndefined()
  }
})

test("endpoint resolution falls back only when metered metadata is omitted", () => {
  expect(resolveMimoEndpoint({ lane: "metered_api" })).toEqual({
    url: "https://api.xiaomimimo.com/v1",
    policy: "mimo-metered-api-v1",
  })
  expect(resolveMimoEndpoint({ lane: "metered_api", credentialURL: "https://bad.example/v1" })).toBeUndefined()
  expect(resolveMimoEndpoint({ lane: "subscription" })).toBeUndefined()
  expect(resolveMimoEndpoint({
    lane: "subscription",
    connectionURL: "https://token-plan-ams.xiaomimimo.com/v1",
  })).toMatchObject({ policy: "mimo-token-plan-v1" })
  expect(resolveMimoEndpoint({
    lane: "subscription",
    credentialURL: "https://token-plan-cn.xiaomimimo.com/v1",
    connectionURL: "https://token-plan-sgp.xiaomimimo.com/v1",
  })).toBeUndefined()
})
