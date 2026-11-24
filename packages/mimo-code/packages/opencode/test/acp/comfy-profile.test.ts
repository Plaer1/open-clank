import { expect, test } from "bun:test"
import { validateComfyJobConnection, validateConnection } from "../../src/acp/provider-control"

test("registered Comfy job profile accepts only keyless loopback origin and advertises no model routes", () => {
  const value = {familyID:"comfyui",adapterID:"comfyui-workflow-v1",kind:"local",billingLane:"local",url:"http://127.0.0.1:8188",settings:{}}
  const result = validateComfyJobConnection(value)
  expect(result.credentialRequired).toBe(false)
  expect(result.settings).toEqual({interface:"comfyui-workflow-v1"})
  expect(result.modelRoutes).toEqual([])
  expect(validateConnection(value)).toEqual(result)
  expect(() => validateComfyJobConnection({...value,url:"https://example.com"})).toThrow()
  expect(() => validateComfyJobConnection({...value,url:"http://127.0.0.1:8188/path"})).toThrow()
  expect(() => validateComfyJobConnection({...value,settings:{interface:"other"}})).toThrow()
})
