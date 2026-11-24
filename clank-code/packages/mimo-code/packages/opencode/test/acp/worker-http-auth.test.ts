import { expect, test } from "bun:test"
import path from "node:path"
import { closeSync, openSync } from "node:fs"
import { consumeServerAuthEnvironment } from "../../src/cli/cmd/acp"
import { consumeInheritedServerPassword } from "../../src/flag/flag"
import { tmpdir } from "../fixture/fixture"

test("Open Clank consumes worker auth from an inherited descriptor", async () => {
  await using tmp = await tmpdir()
  const secretPath = path.join(tmp.path, "worker-auth")
  await Bun.write(secretPath, "descriptor-only-secret")
  const fd = openSync(secretPath, "r")
  process.env.OPEN_CLANK_WORKER_AUTH_FD = String(fd)

  expect(consumeInheritedServerPassword()).toBe("descriptor-only-secret")
  expect(process.env.OPEN_CLANK_WORKER_AUTH_FD).toBeUndefined()
})

test("a configured worker-auth descriptor fails closed", async () => {
  await using tmp = await tmpdir()

  delete process.env.OPEN_CLANK_WORKER_AUTH_FD
  expect(consumeInheritedServerPassword()).toBeUndefined()

  process.env.OPEN_CLANK_WORKER_AUTH_FD = "not-a-descriptor"
  expect(() => consumeInheritedServerPassword()).toThrow(
    "Invalid Open Clank worker-auth descriptor",
  )

  const emptyPath = path.join(tmp.path, "empty-worker-auth")
  await Bun.write(emptyPath, "")
  process.env.OPEN_CLANK_WORKER_AUTH_FD = String(openSync(emptyPath, "r"))
  expect(() => consumeInheritedServerPassword()).toThrow(
    "Invalid Open Clank worker-auth secret",
  )

  const closedPath = path.join(tmp.path, "closed-worker-auth")
  await Bun.write(closedPath, "unreadable")
  const closedFD = openSync(closedPath, "r")
  closeSync(closedFD)
  process.env.OPEN_CLANK_WORKER_AUTH_FD = String(closedFD)
  expect(() => consumeInheritedServerPassword()).toThrow(
    "Unable to read Open Clank worker-auth descriptor",
  )
  expect(process.env.OPEN_CLANK_WORKER_AUTH_FD).toBeUndefined()
})

test("ACP keeps server auth in memory without passing it to child processes", () => {
  const previousPasswordEnv = process.env.MIMOCODE_SERVER_PASSWORD
  const previousUsernameEnv = process.env.MIMOCODE_SERVER_USERNAME

  process.env.MIMOCODE_SERVER_PASSWORD = "worker-only-secret"
  process.env.MIMOCODE_SERVER_USERNAME = "open-clank"

  try {
    const headers = consumeServerAuthEnvironment({
      password: "worker-only-secret",
      username: "open-clank",
    })

    expect(headers).toEqual({
      Authorization: `Basic ${Buffer.from("open-clank:worker-only-secret").toString("base64")}`,
    })
    expect(process.env.MIMOCODE_SERVER_PASSWORD).toBeUndefined()
    expect(process.env.MIMOCODE_SERVER_USERNAME).toBeUndefined()
  } finally {
    if (previousPasswordEnv === undefined) delete process.env.MIMOCODE_SERVER_PASSWORD
    else process.env.MIMOCODE_SERVER_PASSWORD = previousPasswordEnv
    if (previousUsernameEnv === undefined) delete process.env.MIMOCODE_SERVER_USERNAME
    else process.env.MIMOCODE_SERVER_USERNAME = previousUsernameEnv
  }
})
