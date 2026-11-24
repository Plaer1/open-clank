import { expect, test } from "bun:test"
import { passiveQuota } from "../../src/provider/logging-transport"

test("passive Anthropic resets require an exact calendar timestamp", () => {
  const read=(reset:string) => passiveQuota({response:{headers:new Headers({
    "anthropic-ratelimit-requests-reset":reset,
    "anthropic-ratelimit-requests-remaining":"7",
    "anthropic-ratelimit-requests-limit":"10",
  })}},"anthropic")?.requests
  expect(read("2024-02-29T10:15:00Z")).toEqual({limit:10,remaining:7,resetAt:"2024-02-29T10:15:00.000Z"})
  expect(read("2024-02-29T10:15:00+02:30")).toEqual({limit:10,remaining:7,resetAt:"2024-02-29T07:45:00.000Z"})
  expect(read("2024-02-30T10:15:00Z")).toEqual({limit:10,remaining:7})
  expect(read("2024-02-29")).toEqual({limit:10,remaining:7})
  expect(read("2024-02-29T10:15:00-00:00")).toEqual({limit:10,remaining:7})
  expect(read("2024-02-29T10:15:00+24:00")).toEqual({limit:10,remaining:7})
  const before=Date.now()
  const openai=passiveQuota({response:{headers:new Headers({"x-ratelimit-reset-requests":"5s", "x-ratelimit-remaining-requests":"2"})}},"openai")?.requests
  expect(openai?.remaining).toBe(2)
  expect(Date.parse(openai!.resetAt!)-before).toBeGreaterThanOrEqual(5000)
})
