import { afterEach, beforeEach, describe, expect, test } from "bun:test"
import { Effect, Layer, ManagedRuntime } from "effect"
import fs from "fs/promises"
import os from "os"
import path from "path"
import crypto from "node:crypto"
import { Shell } from "../../src/shell/shell"
import { BashTool, sanitizeShellEnvironment } from "../../src/tool/bash"
import { Instance } from "../../src/project/instance"
import { Filesystem } from "../../src/util"
import { tmpdir } from "../fixture/fixture"
import type { Permission } from "../../src/permission"
import { Agent } from "../../src/agent/agent"
import { Truncate } from "../../src/tool"
import { SessionID, MessageID } from "../../src/session/schema"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { Plugin } from "../../src/plugin"
import { minimalShellEnvironment, resolveShellInvocation } from "../../src/tool/shell-containment"
import * as BashInteractive from "../../src/tool/bash-interactive"
import type { Client } from "@modelcontextprotocol/sdk/client/index.js"
import {
  bindMemorySessionClient,
  closeSharedMcpClient,
  registerManagedMcpClient,
  unregisterManagedMcpClient,
} from "../../src/memory/mcp-client"

const runtime = ManagedRuntime.make(
  Layer.mergeAll(
    CrossSpawnSpawner.defaultLayer,
    AppFileSystem.defaultLayer,
    Plugin.defaultLayer,
    Truncate.defaultLayer,
    Agent.defaultLayer,
  ),
)

function initBash() {
  return runtime.runPromise(BashTool.pipe(Effect.flatMap((info) => info.init())))
}

const ctx = {
  sessionID: SessionID.make("ses_test"),
  messageID: MessageID.make(""),
  callID: "",
  agent: "build",
  abort: AbortSignal.any([]),
  messages: [],
  metadata: () => Effect.void,
  ask: () => Effect.void,
}

Shell.acceptable.reset()
const quote = (text: string) => `"${text}"`
const squote = (text: string) => `'${text}'`
const projectRoot = path.join(__dirname, "../..")
const bin = quote(process.execPath.replaceAll("\\", "/"))
const bash = (() => {
  const shell = Shell.acceptable()
  if (Shell.name(shell) === "bash") return shell
  return Shell.gitbash()
})()
const shells = (() => {
  if (process.platform !== "win32") {
    const shell = Shell.acceptable()
    return [{ label: Shell.name(shell), shell }]
  }

  const list = [bash, Bun.which("pwsh"), Bun.which("powershell"), process.env.COMSPEC || Bun.which("cmd.exe")]
    .filter((shell): shell is string => Boolean(shell))
    .map((shell) => ({ label: Shell.name(shell), shell }))

  return list.filter(
    (item, i) => list.findIndex((other) => other.shell.toLowerCase() === item.shell.toLowerCase()) === i,
  )
})()
const PS = new Set(["pwsh", "powershell"])
const ps = shells.filter((item) => PS.has(item.label))

const sh = () => Shell.name(Shell.acceptable())
const evalarg = (text: string) => (sh() === "cmd" ? quote(text) : squote(text))

test("shell subprocess environment strips secret-shaped names", () => {
  expect(sanitizeShellEnvironment({
    PATH: "/bin",
    XIAOMI_API_KEY: "secret",
    FM_TEST_DEEPSEEK_API_KEY: "duplicate",
    ACCESS_TOKEN: "token",
    SMTP_PASSWORD: "password",
  })).toEqual({ PATH: "/bin" })
})

test("default and auto fall back to the OS boundary when bubblewrap is unavailable", async () => {
  await using tmp = await tmpdir()
  const previousPath = process.env.PATH
  const previousMode = process.env.OPEN_CLANK_SHELL_SANDBOX
  process.env.PATH = ""
  try {
    for (const mode of [undefined, "auto"] as const) {
      if (mode === undefined) delete process.env.OPEN_CLANK_SHELL_SANDBOX
      else process.env.OPEN_CLANK_SHELL_SANDBOX = mode
      const shell = process.platform === "win32" ? process.env.COMSPEC ?? "cmd.exe" : "/bin/sh"
      const invocation = resolveShellInvocation({
        shell,
        command: "printf ok",
        cwd: tmp.path,
        workspace: tmp.path,
      })
      expect(invocation.containment).toBe("off")
      expect(invocation.executable).toBe(shell)
    }
  } finally {
    if (previousPath === undefined) delete process.env.PATH
    else process.env.PATH = previousPath
    if (previousMode === undefined) delete process.env.OPEN_CLANK_SHELL_SANDBOX
    else process.env.OPEN_CLANK_SHELL_SANDBOX = previousMode
  }
})

test("required containment still fails closed when bubblewrap is unavailable", async () => {
  await using tmp = await tmpdir()
  const previousPath = process.env.PATH
  const previousMode = process.env.OPEN_CLANK_SHELL_SANDBOX
  process.env.PATH = ""
  process.env.OPEN_CLANK_SHELL_SANDBOX = "required"
  try {
    const shell = process.platform === "win32" ? process.env.COMSPEC ?? "cmd.exe" : "/bin/sh"
    expect(() =>
      resolveShellInvocation({
        shell,
        command: "printf ok",
        cwd: tmp.path,
        workspace: tmp.path,
      }),
    ).toThrow("OPEN_CLANK_SHELL_SANDBOX=required")
  } finally {
    if (previousPath === undefined) delete process.env.PATH
    else process.env.PATH = previousPath
    if (previousMode === undefined) delete process.env.OPEN_CLANK_SHELL_SANDBOX
    else process.env.OPEN_CLANK_SHELL_SANDBOX = previousMode
  }
})

test("network-disabled execution remains fail-closed without bubblewrap", async () => {
  await using tmp = await tmpdir()
  const previousPath = process.env.PATH
  const previousMode = process.env.OPEN_CLANK_SHELL_SANDBOX
  process.env.PATH = ""
  process.env.OPEN_CLANK_SHELL_SANDBOX = "auto"
  try {
    const shell = process.platform === "win32" ? process.env.COMSPEC ?? "cmd.exe" : "/bin/sh"
    expect(() =>
      resolveShellInvocation({
        shell,
        command: "printf ok",
        cwd: tmp.path,
        workspace: tmp.path,
        network: "disabled",
      }),
    ).toThrow("network-disabled")
  } finally {
    if (previousPath === undefined) delete process.env.PATH
    else process.env.PATH = previousPath
    if (previousMode === undefined) delete process.env.OPEN_CLANK_SHELL_SANDBOX
    else process.env.OPEN_CLANK_SHELL_SANDBOX = previousMode
  }
})

test("explicit off always selects OS-boundary execution", async () => {
  await using tmp = await tmpdir()
  const previousPath = process.env.PATH
  const previousMode = process.env.OPEN_CLANK_SHELL_SANDBOX
  process.env.PATH = ""
  process.env.OPEN_CLANK_SHELL_SANDBOX = "off"
  try {
    const shell = process.platform === "win32" ? process.env.COMSPEC ?? "cmd.exe" : "/bin/sh"
    const invocation = resolveShellInvocation({
      shell,
      command: "printf ok",
      cwd: tmp.path,
      workspace: tmp.path,
    })
    expect(invocation.containment).toBe("off")
    expect(invocation.executable).toBe(shell)
  } finally {
    if (previousPath === undefined) delete process.env.PATH
    else process.env.PATH = previousPath
    if (previousMode === undefined) delete process.env.OPEN_CLANK_SHELL_SANDBOX
    else process.env.OPEN_CLANK_SHELL_SANDBOX = previousMode
  }
})

test("workspace zsh startup files cannot run before the requested command", async () => {
  if (process.platform === "win32") return
  const zsh = Bun.which("zsh")
  if (!zsh) return
  await using workspace = await tmpdir({
    init: async (dir) => {
      await Bun.write(path.join(dir, ".zshenv"), "print -r -- startup-ran > startup-canary\n")
    },
  })
  const previous = process.env.OPEN_CLANK_SHELL_SANDBOX
  process.env.OPEN_CLANK_SHELL_SANDBOX = "off"
  try {
    const invocation = resolveShellInvocation({
      shell: zsh,
      command: "print -r -- command-ran",
      cwd: workspace.path,
      workspace: workspace.path,
    })
    const env = minimalShellEnvironment(
      { ...process.env, ZDOTDIR: workspace.path },
      workspace.path,
    )
    expect(env.ZDOTDIR).toBeUndefined()
    const result = Bun.spawnSync([invocation.executable, ...invocation.args], {
      cwd: workspace.path,
      env,
    })
    expect(invocation.args.slice(0, 2)).toEqual(["-f", "-c"])
    expect(result.stdout.toString().trim()).toBe("command-ran")
    expect(await Bun.file(path.join(workspace.path, "startup-canary")).exists()).toBeFalse()
  } finally {
    if (previous === undefined) delete process.env.OPEN_CLANK_SHELL_SANDBOX
    else process.env.OPEN_CLANK_SHELL_SANDBOX = previous
  }
})

test("bubblewrap keeps shell writes inside the workspace", async () => {
  if (process.platform === "win32" || !Bun.which("bwrap")) return
  await using tmp = await tmpdir()
  const outside = path.join(os.homedir(), `open-clank-mimo-shell-escape-${crypto.randomUUID()}`)
  const previous = process.env.OPEN_CLANK_SHELL_SANDBOX
  process.env.OPEN_CLANK_SHELL_SANDBOX = "required"
  try {
    const invocation = resolveShellInvocation({
      shell: Bun.which("bash") ?? "/bin/sh",
      command: `printf ok > inside; printf nope > ${quote(outside)}`,
      cwd: tmp.path,
      workspace: tmp.path,
    })
    const runIndex = invocation.args.indexOf("/run")
    expect(invocation.args.slice(runIndex - 1, runIndex + 1)).toEqual(["--tmpfs", "/run"])
    Bun.spawnSync([invocation.executable, ...invocation.args], {
      cwd: tmp.path,
      env: minimalShellEnvironment(process.env, tmp.path),
    })
    expect(invocation.containment).toBe("bwrap")
    expect(await Bun.file(path.join(tmp.path, "inside")).text()).toBe("ok")
    expect(await Bun.file(outside).exists()).toBe(false)
  } finally {
    if (previous === undefined) delete process.env.OPEN_CLANK_SHELL_SANDBOX
    else process.env.OPEN_CLANK_SHELL_SANDBOX = previous
    await fs.rm(outside, { force: true }).catch(() => {})
  }
})

test("bubblewrap can execute a runtime installed outside the workspace", async () => {
  if (process.platform === "win32" || !Bun.which("bwrap")) return
  await using tmp = await tmpdir()
  const previous = process.env.OPEN_CLANK_SHELL_SANDBOX
  process.env.OPEN_CLANK_SHELL_SANDBOX = "required"
  try {
    const invocation = resolveShellInvocation({
      shell: Bun.which("bash") ?? "/bin/sh",
      command: `${quote(process.execPath)} --version`,
      cwd: tmp.path,
      workspace: tmp.path,
    })
    const result = Bun.spawnSync([invocation.executable, ...invocation.args], {
      cwd: tmp.path,
      env: minimalShellEnvironment(process.env, tmp.path),
    })
    expect(invocation.containment).toBe("bwrap")
    expect(result.exitCode).toBe(0)
  } finally {
    if (previous === undefined) delete process.env.OPEN_CLANK_SHELL_SANDBOX
    else process.env.OPEN_CLANK_SHELL_SANDBOX = previous
  }
})

test("bubblewrap hides Open Clank control data inside the workspace", async () => {
  if (process.platform === "win32" || !Bun.which("bwrap")) return
  await using tmp = await tmpdir()
  const control = path.join(tmp.path, "data")
  const appDb = path.join(control, "app.db")
  await fs.mkdir(control, { recursive: true })
  await fs.writeFile(appDb, "original")
  const previousSandbox = process.env.OPEN_CLANK_SHELL_SANDBOX
  const previousControl = process.env.OPEN_CLANK_CONTROL_DATA_DIR
  process.env.OPEN_CLANK_SHELL_SANDBOX = "required"
  process.env.OPEN_CLANK_CONTROL_DATA_DIR = control
  try {
    const invocation = resolveShellInvocation({
      shell: Bun.which("bash") ?? "/bin/sh",
      command: "test ! -e data/app.db",
      cwd: tmp.path,
      workspace: tmp.path,
    })
    const result = Bun.spawnSync([invocation.executable, ...invocation.args], {
      cwd: tmp.path,
      env: minimalShellEnvironment(process.env, tmp.path),
    })
    expect(result.exitCode).toBe(0)
    expect(await Bun.file(appDb).text()).toBe("original")
  } finally {
    if (previousSandbox === undefined) delete process.env.OPEN_CLANK_SHELL_SANDBOX
    else process.env.OPEN_CLANK_SHELL_SANDBOX = previousSandbox
    if (previousControl === undefined) delete process.env.OPEN_CLANK_CONTROL_DATA_DIR
    else process.env.OPEN_CLANK_CONTROL_DATA_DIR = previousControl
  }
})

test("bubblewrap hides Open Clank control data outside the workspace", async () => {
  if (process.platform === "win32" || !Bun.which("bwrap")) return
  await using workspace = await tmpdir()
  await using control = await tmpdir()
  const appDb = path.join(control.path, "app.db")
  const runtimeCache = path.join(control.path, "mimocode", "cache")
  const runtimeTool = path.join(runtimeCache, "bin", "tool")
  await fs.writeFile(appDb, "secret")
  await fs.mkdir(path.dirname(runtimeTool), { recursive: true })
  await fs.writeFile(runtimeTool, "runtime")
  const previousSandbox = process.env.OPEN_CLANK_SHELL_SANDBOX
  const previousControl = process.env.OPEN_CLANK_CONTROL_DATA_DIR
  const previousHome = process.env.MIMOCODE_HOME
  process.env.OPEN_CLANK_SHELL_SANDBOX = "required"
  process.env.OPEN_CLANK_CONTROL_DATA_DIR = control.path
  process.env.MIMOCODE_HOME = path.join(control.path, "mimocode")
  try {
    const invocation = resolveShellInvocation({
      shell: Bun.which("bash") ?? "/bin/sh",
      command: `test ! -e ${quote(appDb)} && test "$(cat ${quote(runtimeTool)})" = runtime`,
      cwd: workspace.path,
      workspace: workspace.path,
    })
    const maskIndex = invocation.args.indexOf(control.path)
    expect(invocation.args.slice(maskIndex - 1, maskIndex + 1)).toEqual(["--tmpfs", control.path])
    const result = Bun.spawnSync([invocation.executable, ...invocation.args], {
      cwd: workspace.path,
      env: minimalShellEnvironment(process.env, workspace.path),
    })
    expect(result.exitCode).toBe(0)
    expect(await Bun.file(appDb).text()).toBe("secret")
  } finally {
    if (previousSandbox === undefined) delete process.env.OPEN_CLANK_SHELL_SANDBOX
    else process.env.OPEN_CLANK_SHELL_SANDBOX = previousSandbox
    if (previousControl === undefined) delete process.env.OPEN_CLANK_CONTROL_DATA_DIR
    else process.env.OPEN_CLANK_CONTROL_DATA_DIR = previousControl
    if (previousHome === undefined) delete process.env.MIMOCODE_HOME
    else process.env.MIMOCODE_HOME = previousHome
  }
})

test("bubblewrap restores a symlinked MiMo cache at its configured path", async () => {
  if (process.platform === "win32" || !Bun.which("bwrap")) return
  await using workspace = await tmpdir()
  await using control = await tmpdir()
  await using external = await tmpdir()
  const runtimeHome = path.join(control.path, "mimocode")
  const runtimeCache = path.join(runtimeHome, "cache")
  const runtimeTool = path.join(external.path, "bin", "tool")
  const siblingSecret = path.join(control.path, "secret")
  await fs.mkdir(runtimeHome, { recursive: true })
  await fs.mkdir(path.dirname(runtimeTool), { recursive: true })
  await fs.writeFile(runtimeTool, "runtime")
  await fs.writeFile(siblingSecret, "secret")
  await fs.symlink(external.path, runtimeCache, "dir")
  const previousSandbox = process.env.OPEN_CLANK_SHELL_SANDBOX
  const previousControl = process.env.OPEN_CLANK_CONTROL_DATA_DIR
  const previousHome = process.env.MIMOCODE_HOME
  process.env.OPEN_CLANK_SHELL_SANDBOX = "required"
  process.env.OPEN_CLANK_CONTROL_DATA_DIR = control.path
  process.env.MIMOCODE_HOME = runtimeHome
  try {
    const configuredTool = path.join(runtimeCache, "bin", "tool")
    const invocation = resolveShellInvocation({
      shell: Bun.which("bash") ?? "/bin/sh",
      command: `test ! -e ${quote(siblingSecret)} && test "$(cat ${quote(configuredTool)})" = runtime`,
      cwd: workspace.path,
      workspace: workspace.path,
    })
    expect(invocation.args).toContain(runtimeTool.replace(/\/bin\/tool$/, ""))
    const result = Bun.spawnSync([invocation.executable, ...invocation.args], {
      cwd: workspace.path,
      env: minimalShellEnvironment(process.env, workspace.path),
    })
    expect(result.exitCode).toBe(0)
  } finally {
    if (previousSandbox === undefined) delete process.env.OPEN_CLANK_SHELL_SANDBOX
    else process.env.OPEN_CLANK_SHELL_SANDBOX = previousSandbox
    if (previousControl === undefined) delete process.env.OPEN_CLANK_CONTROL_DATA_DIR
    else process.env.OPEN_CLANK_CONTROL_DATA_DIR = previousControl
    if (previousHome === undefined) delete process.env.MIMOCODE_HOME
    else process.env.MIMOCODE_HOME = previousHome
  }
})

test("bubblewrap masks protected siblings while preserving a nested workspace", async () => {
  if (process.platform === "win32" || !Bun.which("bwrap")) return
  await using parent = await tmpdir()
  const workspace = path.join(parent.path, "workspace")
  await fs.mkdir(workspace)
  const siblingSecret = path.join(parent.path, "secret")
  await fs.writeFile(siblingSecret, "secret")
  const previousSandbox = process.env.OPEN_CLANK_SHELL_SANDBOX
  const previousControl = process.env.OPEN_CLANK_CONTROL_DATA_DIR
  process.env.OPEN_CLANK_SHELL_SANDBOX = "required"
  process.env.OPEN_CLANK_CONTROL_DATA_DIR = parent.path
  try {
    const invocation = resolveShellInvocation({
      shell: Bun.which("bash") ?? "/bin/sh",
      command: `test ! -e ${quote(siblingSecret)} && printf ok > result`,
      cwd: workspace,
      workspace,
    })
    expect(
      invocation.args.some(
        (argument, index) => argument === parent.path && invocation.args[index - 1] === "--tmpfs",
      ),
    ).toBe(true)
    const result = Bun.spawnSync([invocation.executable, ...invocation.args], {
      cwd: workspace,
      env: minimalShellEnvironment(process.env, workspace),
    })
    expect(result.exitCode).toBe(0)
    expect(await Bun.file(path.join(workspace, "result")).text()).toBe("ok")
  } finally {
    if (previousSandbox === undefined) delete process.env.OPEN_CLANK_SHELL_SANDBOX
    else process.env.OPEN_CLANK_SHELL_SANDBOX = previousSandbox
    if (previousControl === undefined) delete process.env.OPEN_CLANK_CONTROL_DATA_DIR
    else process.env.OPEN_CLANK_CONTROL_DATA_DIR = previousControl
  }
})

test("bubblewrap hides symlinked Open Clank control data inside the workspace", async () => {
  if (process.platform === "win32" || !Bun.which("bwrap")) return
  await using workspace = await tmpdir()
  await using control = await tmpdir()
  const appDb = path.join(control.path, "app.db")
  await fs.writeFile(appDb, "original")
  const link = path.join(workspace.path, "data")
  await fs.symlink(control.path, link, "dir")
  const previousSandbox = process.env.OPEN_CLANK_SHELL_SANDBOX
  const previousControl = process.env.OPEN_CLANK_CONTROL_DATA_DIR
  process.env.OPEN_CLANK_SHELL_SANDBOX = "required"
  process.env.OPEN_CLANK_CONTROL_DATA_DIR = link
  try {
    const invocation = resolveShellInvocation({
      shell: Bun.which("bash") ?? "/bin/sh",
      command: "test ! -e data/app.db",
      cwd: workspace.path,
      workspace: workspace.path,
    })
    const maskIndex = invocation.args.indexOf(control.path)
    expect(invocation.args.slice(maskIndex - 1, maskIndex + 1)).toEqual(["--tmpfs", control.path])
    const result = Bun.spawnSync([invocation.executable, ...invocation.args], {
      cwd: workspace.path,
      env: minimalShellEnvironment(process.env, workspace.path),
    })
    expect(result.exitCode).toBe(0)
    expect(await Bun.file(appDb).text()).toBe("original")
  } finally {
    if (previousSandbox === undefined) delete process.env.OPEN_CLANK_SHELL_SANDBOX
    else process.env.OPEN_CLANK_SHELL_SANDBOX = previousSandbox
    if (previousControl === undefined) delete process.env.OPEN_CLANK_CONTROL_DATA_DIR
    else process.env.OPEN_CLANK_CONTROL_DATA_DIR = previousControl
  }
})

test("bubblewrap mounts an approved external workdir", async () => {
  if (process.platform === "win32" || !Bun.which("bwrap")) return
  await using workspace = await tmpdir()
  await using external = await tmpdir()
  const previous = process.env.OPEN_CLANK_SHELL_SANDBOX
  process.env.OPEN_CLANK_SHELL_SANDBOX = "required"
  try {
    const invocation = resolveShellInvocation({
      shell: Bun.which("bash") ?? "/bin/sh",
      command: "printf ok > external.txt",
      cwd: external.path,
      workspace: workspace.path,
      writableRoots: [external.path],
    })
    const result = Bun.spawnSync([invocation.executable, ...invocation.args], {
      cwd: external.path,
      env: minimalShellEnvironment(process.env, external.path),
    })
    expect(result.exitCode).toBe(0)
    expect(invocation.containment).toBe("bwrap")
    expect(await Bun.file(path.join(external.path, "external.txt")).text()).toBe("ok")
  } finally {
    if (previous === undefined) delete process.env.OPEN_CLANK_SHELL_SANDBOX
    else process.env.OPEN_CLANK_SHELL_SANDBOX = previous
  }
})

test("network-disabled shell requests a private network namespace", async () => {
  if (process.platform === "win32" || !Bun.which("bwrap")) return
  await using tmp = await tmpdir()
  const previous = process.env.OPEN_CLANK_SHELL_SANDBOX
  process.env.OPEN_CLANK_SHELL_SANDBOX = "required"
  try {
    let invocation
    try {
      invocation = resolveShellInvocation({
        shell: Bun.which("bash") ?? "/bin/sh",
        command: "cat /proc/net/dev",
        cwd: tmp.path,
        workspace: tmp.path,
        network: "disabled",
      })
    } catch (error) {
      expect(String(error)).toContain("network-disabled shell containment is unavailable")
      return
    }
    expect(invocation.containment).toBe("bwrap")
    expect(invocation.network).toBe("disabled")
    expect(invocation.args).toContain("--unshare-net")
    const result = Bun.spawnSync([invocation.executable, ...invocation.args], {
      cwd: tmp.path,
      env: minimalShellEnvironment(process.env, tmp.path),
    })
    expect(result.exitCode).toBe(0)
    const interfaces = result.stdout
      .toString()
      .split("\n")
      .filter((line) => line.includes(":"))
      .map((line) => line.split(":", 1)[0]!.trim())
    expect(interfaces).toEqual(["lo"])
  } finally {
    if (previous === undefined) delete process.env.OPEN_CLANK_SHELL_SANDBOX
    else process.env.OPEN_CLANK_SHELL_SANDBOX = previous
  }
})

test("interactive shell timeout clears its pending request", async () => {
  await using tmp = await tmpdir()
  await Instance.provide({
    directory: tmp.path,
    fn: async () => {
      await expect(
        BashInteractive.request({
          sessionID: "ses-timeout",
          callID: "call-timeout",
          command: "printf ok",
          cwd: tmp.path,
          workspace: tmp.path,
          writableRoots: [tmp.path],
          shell: Shell.acceptable(),
          timeout: 20,
          description: "timeout fixture",
        }),
      ).rejects.toThrow("Interactive command timed out")
      expect(await BashInteractive.list()).toEqual([])
    },
  })
})

test("interactive reply is bound to its session and tool call", async () => {
  await using tmp = await tmpdir()
  await Instance.provide({
    directory: tmp.path,
    fn: async () => {
      const result = BashInteractive.request({
        sessionID: "ses-owner",
        callID: "call-owner",
        command: "printf ok",
        cwd: tmp.path,
        workspace: tmp.path,
        writableRoots: [tmp.path],
        shell: Shell.acceptable(),
        timeout: 5_000,
        description: "binding fixture",
      })
      let pending = await BashInteractive.list()
      for (let attempt = 0; pending.length === 0 && attempt < 20; attempt++) {
        await Bun.sleep(5)
        pending = await BashInteractive.list()
      }
      expect(pending).toHaveLength(1)
      const id = pending[0]!.id
      await expect(
        BashInteractive.reply({
          id,
          sessionID: "ses-other",
          callID: "call-owner",
          output: "forged",
          exitCode: 0,
        }),
      ).rejects.toThrow("does not match")
      expect(await BashInteractive.list()).toHaveLength(1)
      await BashInteractive.reply({
        id,
        sessionID: "ses-owner",
        callID: "call-owner",
        output: "ok",
        exitCode: 0,
      })
      expect(await result).toEqual({ output: "ok", exitCode: 0 })
    },
  })
})

const fill = (mode: "lines" | "bytes", n: number) => {
  // Keep the containment test self-contained on Unix. Running this package's
  // Bun binary would load bunfig.toml, whose preloads live in the monorepo's
  // parent node_modules — intentionally outside this test workspace.
  if (process.platform !== "win32") {
    return mode === "lines"
      ? `seq 1 ${n}`
      : `head -c ${n} /dev/zero | tr '\\0' a`
  }
  const code =
    mode === "lines"
      ? "console.log(Array.from({length:Number(Bun.argv[1])},(_,i)=>i+1).join(String.fromCharCode(10)))"
      : "process.stdout.write(String.fromCharCode(97).repeat(Number(Bun.argv[1])))"
  const text = `${bin} -e ${evalarg(code)} ${n}`
  if (PS.has(sh())) return `& ${text}`
  return text
}
const glob = (p: string) =>
  process.platform === "win32" ? Filesystem.normalizePathPattern(p) : p.replaceAll("\\", "/")

const forms = (dir: string) => {
  if (process.platform !== "win32") return [dir]
  const full = Filesystem.normalizePath(dir)
  const slash = full.replaceAll("\\", "/")
  const root = slash.replace(/^[A-Za-z]:/, "")
  return Array.from(new Set([full, slash, root, root.toLowerCase()]))
}

// Non-login zsh still reads ~/.zshenv from the developer machine, which can emit
// startup noise into bash tool stdout (e.g. a missing ~/.cargo/env). Point ZDOTDIR
// at an empty directory so shell output matches what the tests assert on.
let zdotdirCleanup: (() => Promise<void>) | undefined

async function isolateZshDotfiles() {
  if (process.platform === "win32") return
  Shell.acceptable.reset()
  if (Shell.name(Shell.acceptable()) !== "zsh") return

  const zdotdir = path.join(os.tmpdir(), `mimocode-zdotdir-${Math.random().toString(36).slice(2)}`)
  await fs.mkdir(zdotdir, { recursive: true })
  const prev = process.env.ZDOTDIR
  process.env.ZDOTDIR = zdotdir
  zdotdirCleanup = async () => {
    if (prev === undefined) delete process.env.ZDOTDIR
    else process.env.ZDOTDIR = prev
    await fs.rm(zdotdir, { recursive: true, force: true }).catch(() => {})
  }
}

async function restoreZshDotfiles() {
  await zdotdirCleanup?.()
  zdotdirCleanup = undefined
}

beforeEach(async () => {
  await isolateZshDotfiles()
})

afterEach(async () => {
  await restoreZshDotfiles()
})

const withShell = (item: { label: string; shell: string }, fn: () => Promise<void>) => async () => {
  const prev = process.env.SHELL
  process.env.SHELL = item.shell
  Shell.acceptable.reset()
  Shell.preferred.reset()
  try {
    await fn()
  } finally {
    if (prev === undefined) delete process.env.SHELL
    else process.env.SHELL = prev
    Shell.acceptable.reset()
    Shell.preferred.reset()
  }
}

const each = (name: string, fn: (item: { label: string; shell: string }) => Promise<void>) => {
  for (const item of shells) {
    test(
      `${name} [${item.label}]`,
      withShell(item, () => fn(item)),
    )
  }
}

const capture = (requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">>, stop?: Error) => ({
  ...ctx,
  ask: (req: Omit<Permission.Request, "id" | "sessionID" | "tool">) =>
    Effect.sync(() => {
      requests.push(req)
      if (stop) throw stop
    }),
})

const mustTruncate = (result: {
  metadata: { truncated?: boolean; exit?: number | null } & Record<string, unknown>
  output: string
}) => {
  if (result.metadata.truncated) return
  throw new Error(
    [`shell: ${process.env.SHELL || ""}`, `exit: ${String(result.metadata.exit)}`, "output:", result.output].join("\n"),
  )
}

describe("tool.bash", () => {
  each("basic", async () => {
    await Instance.provide({
      directory: projectRoot,
      fn: async () => {
        const bash = await initBash()
        const result = await Effect.runPromise(
          bash.execute(
            {
              command: "echo test",
              description: "Echo test message",
            },
            ctx,
          ),
        )
        expect(result.metadata.exit).toBe(0)
        expect(result.metadata.output).toContain("test")
      },
    })
  })

  test("managed active project policy blocks shell before execution", async () => {
    await using tmp = await tmpdir()
    const previousPolicy = process.env.OPEN_CLANK_PROJECT_POLICY_BRIDGE
    const client = {
      callTool: async () => ({
        content: [{
          type: "text",
          text: JSON.stringify({
            enforced: true,
            allowed: false,
            reason: "active project policy blocks MiMo shell execution",
          }),
        }],
      }),
    } as unknown as Client
    process.env.OPEN_CLANK_PROJECT_POLICY_BRIDGE = "required"
    registerManagedMcpClient("lifetools_policy_test", client)
    bindMemorySessionClient(ctx.sessionID, "lifetools_policy_test", "alice", "global")
    try {
      await Instance.provide({
        directory: tmp.path,
        fn: async () => {
          const bash = await initBash()
          await expect(
            Effect.runPromise(
              bash.execute(
                {
                  command: "printf nope > policy-canary",
                  description: "Policy canary",
                },
                ctx,
              ),
            ),
          ).rejects.toThrow("active project policy blocks MiMo shell execution")
          expect(await Bun.file(path.join(tmp.path, "policy-canary")).exists()).toBeFalse()
        },
      })
    } finally {
      await closeSharedMcpClient()
      unregisterManagedMcpClient("lifetools_policy_test")
      if (previousPolicy === undefined) delete process.env.OPEN_CLANK_PROJECT_POLICY_BRIDGE
      else process.env.OPEN_CLANK_PROJECT_POLICY_BRIDGE = previousPolicy
    }
  })
})

describe("tool.bash permissions", () => {
  each("asks for bash permission with correct pattern", async () => {
    await using tmp = await tmpdir()
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const bash = await initBash()
        const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
        await Effect.runPromise(
          bash.execute(
            {
              command: "echo hello",
              description: "Echo hello",
            },
            capture(requests),
          ),
        )
        expect(requests.length).toBe(1)
        expect(requests[0].permission).toBe("bash")
        expect(requests[0].patterns).toContain("echo hello")
      },
    })
  })

  each("asks for bash permission with multiple commands", async () => {
    await using tmp = await tmpdir()
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const bash = await initBash()
        const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
        await Effect.runPromise(
          bash.execute(
            {
              command: "echo foo && echo bar",
              description: "Echo twice",
            },
            capture(requests),
          ),
        )
        expect(requests.length).toBe(1)
        expect(requests[0].permission).toBe("bash")
        expect(requests[0].patterns).toContain("echo foo")
        expect(requests[0].patterns).toContain("echo bar")
      },
    })
  })

  for (const item of ps) {
    test(
      `parses PowerShell conditionals for permission prompts [${item.label}]`,
      withShell(item, async () => {
        await Instance.provide({
          directory: projectRoot,
          fn: async () => {
            const bash = await initBash()
            const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
            await Effect.runPromise(
              bash.execute(
                {
                  command: "Write-Host foo; if ($?) { Write-Host bar }",
                  description: "Check PowerShell conditional",
                },
                capture(requests),
              ),
            )
            const bashReq = requests.find((r) => r.permission === "bash")
            expect(bashReq).toBeDefined()
            expect(bashReq!.patterns).toContain("Write-Host foo")
            expect(bashReq!.patterns).toContain("Write-Host bar")
            expect(bashReq!.always).toContain("Write-Host *")
          },
        })
      }),
    )
  }

  each("asks for external_directory permission for wildcard external paths", async () => {
    await Instance.provide({
      directory: projectRoot,
      fn: async () => {
        const bash = await initBash()
        const err = new Error("stop after permission")
        const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
        const file = process.platform === "win32" ? `${process.env.WINDIR!.replaceAll("\\", "/")}/*` : "/etc/*"
        const want = glob(path.join(
          AppFileSystem.resolve(process.platform === "win32" ? process.env.WINDIR! : "/etc"),
          "*",
        ))
        await expect(
          Effect.runPromise(
            bash.execute(
              {
                command: `cat ${file}`,
                description: "Read wildcard path",
              },
              capture(requests, err),
            ),
          ),
        ).rejects.toThrow(err.message)
        const extDirReq = requests.find((r) => r.permission === "external_directory")
        expect(extDirReq).toBeDefined()
        expect(extDirReq!.patterns).toContain(want)
      },
    })
  })

  each("asks for bash_destructive only (no bash prompt) when running rm inside the project", async () => {
    await using tmp = await tmpdir({
      init: async (dir) => {
        await fs.mkdir(path.join(dir, "nested"))
        await Bun.write(path.join(dir, "nested", "victim.txt"), "x")
      },
    })
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const bash = await initBash()
        const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
        await Effect.runPromise(
          bash.execute(
            {
              command: "rm victim.txt",
              workdir: "nested",
              description: "Remove victim.txt",
            },
            capture(requests),
          ),
        )
        const destructiveReq = requests.find((r) => r.permission === "bash_destructive")
        expect(destructiveReq).toBeDefined()
        expect(destructiveReq!.patterns).toContain("rm victim.txt")
        expect(destructiveReq!.metadata.command).toBe("rm victim.txt")
        expect(destructiveReq!.metadata.workdir).toBe(path.join(tmp.path, "nested"))
        // The confirmation UI shows the full command → a separate `bash` ask would
        // just be a second confirmation of the same thing.
        expect(requests.find((r) => r.permission === "bash")).toBeUndefined()
      },
    })
  })

  each("asks for bash_destructive on destructive git subcommands", async () => {
    await using tmp = await tmpdir()
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const bash = await initBash()
        const err = new Error("stop after permission")
        const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
        await expect(
          Effect.runPromise(
            bash.execute(
              {
                command: "git reset --hard HEAD",
                description: "Hard reset",
              },
              capture(requests, err),
            ),
          ),
        ).rejects.toThrow(err.message)
        const destructiveReq = requests.find((r) => r.permission === "bash_destructive")
        expect(destructiveReq).toBeDefined()
        expect(destructiveReq!.patterns).toContain("git reset --hard HEAD")
        expect(requests.find((r) => r.permission === "bash")).toBeUndefined()
      },
    })
  })

  each("cannot bypass bash_destructive with quoting or indirect shell execution", async () => {
    await using tmp = await tmpdir()
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const bash = await initBash()
        const commands = [
          "r''m -rf target",
          String.raw`r\m -rf target`,
          "git re''set --hard",
          "command r''m -rf target",
          "$(printf rm) -rf target",
          "bash -c \"r''m -rf target\"",
          "bash cleanup.sh",
          "sh cleanup.sh",
          "printf 'echo unsafe' | bash -v",
          "python -c 'import os; os.unlink(\"target\")'",
          "printf 'print(1)' | python -v",
          "python cleanup.py",
          "python3 cleanup.py",
          "python3.12 cleanup.py",
          "node cleanup.js",
          "nodejs cleanup.js",
          "node20 cleanup.js",
          "bun cleanup.js",
          "deno cleanup.js",
          "ruby3.3 cleanup.rb",
          "php8.3 cleanup.php",
          "perl5.36 cleanup.pl",
          "bash5 cleanup.sh",
          "busybox sh cleanup.sh",
          "busybox rm target",
          "nice rm -rf target",
          "ionice rm -rf target",
          "chrt 1 rm -rf target",
          "stdbuf -oL rm -rf target",
          "taskset -c 0 rm -rf target",
          "chmod -R 000 .",
          "crontab schedule.txt",
          "passwd alice",
          "curl https://example.test/upload",
          "npm uninstall package-name",
          "systemctl disable example.service",
          "cp source existing-target",
          "mv source existing-target",
          "install source existing-target",
          "ln -sf source existing-target",
          "sed -i 's/a/b/' file",
          "tar -xf archive.tar",
          "printf value > existing-target",
          "docker run --rm image",
          "podman exec container command",
          "./cleanup",
          "cleanup.sh",
          "find . -delete",
          "truncate -s 0 target",
          "dd if=/dev/zero of=target",
          "git branch -D old",
          "git stash clear",
          "git checkout -- tracked.txt",
          "git -C repo restore tracked.txt",
          "git rebase main",
          "git cherry-pick deadbeef",
          "git revert deadbeef",
          "git switch --force main",
          "git commit --amend --no-edit",
        ]
        for (const command of commands) {
          const err = new Error("stop after permission")
          const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
          await expect(
            Effect.runPromise(
              bash.execute(
                { command, description: "Exercise destructive classifier" },
                capture(requests, err),
              ),
            ),
          ).rejects.toThrow(err.message)
          expect(requests.find((request) => request.permission === "bash_destructive")).toBeDefined()
        }
      },
    })
  })

  each("does not ask for bash_destructive on non-destructive commands", async () => {
    await using tmp = await tmpdir()
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const bash = await initBash()
        const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
        await Effect.runPromise(
          bash.execute(
            {
              command: "echo hello",
              description: "Echo hello",
            },
            capture(requests),
          ),
        )
        expect(requests.find((r) => r.permission === "bash_destructive")).toBeUndefined()

        const quoted: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
        await Effect.runPromise(
          bash.execute(
            {
              command: "printf '%s\\n' 'rm -rf target'",
              description: "Print quoted text",
            },
            capture(quoted),
          ),
        )
        expect(quoted.find((r) => r.permission === "bash_destructive")).toBeUndefined()
      },
    })
  })

  if (process.platform === "win32") {
    if (bash) {
      test(
        "asks for nested bash command permissions [bash]",
        withShell({ label: "bash", shell: bash }, async () => {
          await using outerTmp = await tmpdir({
            init: async (dir) => {
              await Bun.write(path.join(dir, "outside.txt"), "x")
            },
          })
          await Instance.provide({
            directory: projectRoot,
            fn: async () => {
              const bash = await initBash()
              const file = path.join(outerTmp.path, "outside.txt").replaceAll("\\", "/")
              const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
              await Effect.runPromise(
                bash.execute(
                  {
                    command: `echo $(cat "${file}")`,
                    description: "Read nested bash file",
                  },
                  capture(requests),
                ),
              )
              const extDirReq = requests.find((r) => r.permission === "external_directory")
              const bashReq = requests.find((r) => r.permission === "bash")
              expect(extDirReq).toBeDefined()
              expect(extDirReq!.patterns).toContain(glob(path.join(outerTmp.path, "*")))
              expect(bashReq).toBeDefined()
              expect(bashReq!.patterns).toContain(`cat "${file}"`)
            },
          })
        }),
      )
    }
  }

  if (process.platform === "win32") {
    for (const item of ps) {
      test(
        `asks for external_directory permission for PowerShell paths after switches [${item.label}]`,
        withShell(item, async () => {
          await Instance.provide({
            directory: projectRoot,
            fn: async () => {
              const bash = await initBash()
              const err = new Error("stop after permission")
              const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
              await expect(
                Effect.runPromise(
                  bash.execute(
                    {
                      command: `Copy-Item -PassThru "${process.env.WINDIR!.replaceAll("\\", "/")}/win.ini" ./out`,
                      description: "Copy Windows ini",
                    },
                    capture(requests, err),
                  ),
                ),
              ).rejects.toThrow(err.message)
              const extDirReq = requests.find((r) => r.permission === "external_directory")
              expect(extDirReq).toBeDefined()
              expect(extDirReq!.patterns).toContain(glob(path.join(process.env.WINDIR!, "*")))
            },
          })
        }),
      )
    }

    for (const item of ps) {
      test(
        `asks for nested PowerShell command permissions [${item.label}]`,
        withShell(item, async () => {
          await Instance.provide({
            directory: projectRoot,
            fn: async () => {
              const bash = await initBash()
              const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
              const file = `${process.env.WINDIR!.replaceAll("\\", "/")}/win.ini`
              await Effect.runPromise(
                bash.execute(
                  {
                    command: `Write-Output $(Get-Content ${file})`,
                    description: "Read nested PowerShell file",
                  },
                  capture(requests),
                ),
              )
              const extDirReq = requests.find((r) => r.permission === "external_directory")
              const bashReq = requests.find((r) => r.permission === "bash")
              expect(extDirReq).toBeDefined()
              expect(extDirReq!.patterns).toContain(glob(path.join(process.env.WINDIR!, "*")))
              expect(bashReq).toBeDefined()
              expect(bashReq!.patterns).toContain(`Get-Content ${file}`)
            },
          })
        }),
      )
    }

    for (const item of ps) {
      test(
        `asks for external_directory permission for drive-relative PowerShell paths [${item.label}]`,
        withShell(item, async () => {
          await using tmp = await tmpdir()
          await Instance.provide({
            directory: tmp.path,
            fn: async () => {
              const bash = await initBash()
              const err = new Error("stop after permission")
              const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
              await expect(
                Effect.runPromise(
                  bash.execute(
                    {
                      command: 'Get-Content "C:../outside.txt"',
                      description: "Read drive-relative file",
                    },
                    capture(requests, err),
                  ),
                ),
              ).rejects.toThrow(err.message)
              expect(requests[0]?.permission).toBe("external_directory")
              if (requests[0]?.permission !== "external_directory") return
              expect(requests[0].patterns).toContain(glob(path.join(path.dirname(tmp.path), "*")))
            },
          })
        }),
      )
    }

    for (const item of ps) {
      test(
        `asks for external_directory permission for $HOME PowerShell paths [${item.label}]`,
        withShell(item, async () => {
          await Instance.provide({
            directory: projectRoot,
            fn: async () => {
              const bash = await initBash()
              const err = new Error("stop after permission")
              const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
              await expect(
                Effect.runPromise(
                  bash.execute(
                    {
                      command: 'Get-Content "$HOME/.ssh/config"',
                      description: "Read home config",
                    },
                    capture(requests, err),
                  ),
                ),
              ).rejects.toThrow(err.message)
              expect(requests[0]?.permission).toBe("external_directory")
              if (requests[0]?.permission !== "external_directory") return
              expect(requests[0].patterns).toContain(glob(path.join(os.homedir(), ".ssh", "*")))
            },
          })
        }),
      )
    }

    for (const item of ps) {
      test(
        `asks for external_directory permission for $PWD PowerShell paths [${item.label}]`,
        withShell(item, async () => {
          await using tmp = await tmpdir()
          await Instance.provide({
            directory: tmp.path,
            fn: async () => {
              const bash = await initBash()
              const err = new Error("stop after permission")
              const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
              await expect(
                Effect.runPromise(
                  bash.execute(
                    {
                      command: 'Get-Content "$PWD/../outside.txt"',
                      description: "Read pwd-relative file",
                    },
                    capture(requests, err),
                  ),
                ),
              ).rejects.toThrow(err.message)
              expect(requests[0]?.permission).toBe("external_directory")
              if (requests[0]?.permission !== "external_directory") return
              expect(requests[0].patterns).toContain(glob(path.join(path.dirname(tmp.path), "*")))
            },
          })
        }),
      )
    }

    for (const item of ps) {
      test(
        `asks for external_directory permission for $PSHOME PowerShell paths [${item.label}]`,
        withShell(item, async () => {
          await Instance.provide({
            directory: projectRoot,
            fn: async () => {
              const bash = await initBash()
              const err = new Error("stop after permission")
              const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
              await expect(
                Effect.runPromise(
                  bash.execute(
                    {
                      command: 'Get-Content "$PSHOME/outside.txt"',
                      description: "Read pshome file",
                    },
                    capture(requests, err),
                  ),
                ),
              ).rejects.toThrow(err.message)
              expect(requests[0]?.permission).toBe("external_directory")
              if (requests[0]?.permission !== "external_directory") return
              expect(requests[0].patterns).toContain(glob(path.join(path.dirname(item.shell), "*")))
            },
          })
        }),
      )
    }

    for (const item of ps) {
      test(
        `asks for external_directory permission for missing PowerShell env paths [${item.label}]`,
        withShell(item, async () => {
          const key = "MIMOCODE_TEST_MISSING"
          const prev = process.env[key]
          delete process.env[key]
          try {
            await Instance.provide({
              directory: projectRoot,
              fn: async () => {
                const bash = await initBash()
                const err = new Error("stop after permission")
                const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
                const root = path.parse(process.env.WINDIR!).root.replace(/[\\/]+$/, "")
                await expect(
                  Effect.runPromise(
                    bash.execute(
                      {
                        command: `Get-Content -Path "${root}$env:${key}\\Windows\\win.ini"`,
                        description: "Read Windows ini with missing env",
                      },
                      capture(requests, err),
                    ),
                  ),
                ).rejects.toThrow(err.message)
                const extDirReq = requests.find((r) => r.permission === "external_directory")
                expect(extDirReq).toBeDefined()
                expect(extDirReq!.patterns).toContain(glob(path.join(process.env.WINDIR!, "*")))
              },
            })
          } finally {
            if (prev === undefined) delete process.env[key]
            else process.env[key] = prev
          }
        }),
      )
    }

    for (const item of ps) {
      test(
        `asks for external_directory permission for PowerShell env paths [${item.label}]`,
        withShell(item, async () => {
          await Instance.provide({
            directory: projectRoot,
            fn: async () => {
              const bash = await initBash()
              const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
              await Effect.runPromise(
                bash.execute(
                  {
                    command: "Get-Content $env:WINDIR/win.ini",
                    description: "Read Windows ini from env",
                  },
                  capture(requests),
                ),
              )
              const extDirReq = requests.find((r) => r.permission === "external_directory")
              expect(extDirReq).toBeDefined()
              expect(extDirReq!.patterns).toContain(
                Filesystem.normalizePathPattern(path.join(process.env.WINDIR!, "*")),
              )
            },
          })
        }),
      )
    }

    for (const item of ps) {
      test(
        `asks for external_directory permission for PowerShell FileSystem paths [${item.label}]`,
        withShell(item, async () => {
          await Instance.provide({
            directory: projectRoot,
            fn: async () => {
              const bash = await initBash()
              const err = new Error("stop after permission")
              const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
              await expect(
                Effect.runPromise(
                  bash.execute(
                    {
                      command: `Get-Content -Path FileSystem::${process.env.WINDIR!.replaceAll("\\", "/")}/win.ini`,
                      description: "Read Windows ini from FileSystem provider",
                    },
                    capture(requests, err),
                  ),
                ),
              ).rejects.toThrow(err.message)
              expect(requests[0]?.permission).toBe("external_directory")
              if (requests[0]?.permission !== "external_directory") return
              expect(requests[0].patterns).toContain(
                Filesystem.normalizePathPattern(path.join(process.env.WINDIR!, "*")),
              )
            },
          })
        }),
      )
    }

    for (const item of ps) {
      test(
        `asks for external_directory permission for braced PowerShell env paths [${item.label}]`,
        withShell(item, async () => {
          await Instance.provide({
            directory: projectRoot,
            fn: async () => {
              const bash = await initBash()
              const err = new Error("stop after permission")
              const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
              await expect(
                Effect.runPromise(
                  bash.execute(
                    {
                      command: "Get-Content ${env:WINDIR}/win.ini",
                      description: "Read Windows ini from braced env",
                    },
                    capture(requests, err),
                  ),
                ),
              ).rejects.toThrow(err.message)
              expect(requests[0]?.permission).toBe("external_directory")
              if (requests[0]?.permission !== "external_directory") return
              expect(requests[0].patterns).toContain(
                Filesystem.normalizePathPattern(path.join(process.env.WINDIR!, "*")),
              )
            },
          })
        }),
      )
    }

    for (const item of ps) {
      test(
        `treats Set-Location like cd for permissions [${item.label}]`,
        withShell(item, async () => {
          await Instance.provide({
            directory: projectRoot,
            fn: async () => {
              const bash = await initBash()
              const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
              await Effect.runPromise(
                bash.execute(
                  {
                    command: "Set-Location C:/Windows",
                    description: "Change location",
                  },
                  capture(requests),
                ),
              )
              const extDirReq = requests.find((r) => r.permission === "external_directory")
              const bashReq = requests.find((r) => r.permission === "bash")
              expect(extDirReq).toBeDefined()
              expect(extDirReq!.patterns).toContain(
                Filesystem.normalizePathPattern(path.join(process.env.WINDIR!, "*")),
              )
              expect(bashReq).toBeUndefined()
            },
          })
        }),
      )
    }

    for (const item of ps) {
      test(
        `does not add nested PowerShell expressions to permission prompts [${item.label}]`,
        withShell(item, async () => {
          await Instance.provide({
            directory: projectRoot,
            fn: async () => {
              const bash = await initBash()
              const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
              await Effect.runPromise(
                bash.execute(
                  {
                    command: "Write-Output ('a' * 3)",
                    description: "Write repeated text",
                  },
                  capture(requests),
                ),
              )
              const bashReq = requests.find((r) => r.permission === "bash")
              expect(bashReq).toBeDefined()
              expect(bashReq!.patterns).not.toContain("a * 3")
              expect(bashReq!.always).not.toContain("a *")
            },
          })
        }),
      )
    }
  }

  each("asks for external_directory permission when cd to parent", async () => {
    // git: true keeps worktree scoped to tmp.path; otherwise project detection
    // walks up to the repo root and treats sibling fixture dirs as in-worktree.
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const bash = await initBash()
        const err = new Error("stop after permission")
        const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
        await expect(
          Effect.runPromise(
            bash.execute(
              {
                command: "cd ../",
                description: "Change to parent directory",
              },
              capture(requests, err),
            ),
          ),
        ).rejects.toThrow(err.message)
        const extDirReq = requests.find((r) => r.permission === "external_directory")
        expect(extDirReq).toBeDefined()
      },
    })
  })

  each("asks for external_directory permission when workdir is outside project", async () => {
    await using tmp = await tmpdir()
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const bash = await initBash()
        const err = new Error("stop after permission")
        const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
        await expect(
          Effect.runPromise(
            bash.execute(
              {
                command: "echo ok",
                workdir: os.tmpdir(),
                description: "Echo from temp dir",
              },
              capture(requests, err),
            ),
          ),
        ).rejects.toThrow(err.message)
        const extDirReq = requests.find((r) => r.permission === "external_directory")
        expect(extDirReq).toBeDefined()
        expect(extDirReq!.patterns).toContain(glob(path.join(AppFileSystem.resolve(os.tmpdir()), "*")))
      },
    })
  })

  test("canonicalizes Unix temporary aliases without broadening directory authority", async () => {
    if (process.platform === "win32") return
    const literalRoot = await fs.mkdtemp(path.join("/tmp", "mimocode-external-root-"))
    const siblingRoot = await fs.mkdtemp(path.join("/tmp", "mimocode-external-sibling-"))
    const alias = path.join(os.tmpdir(), `mimocode-external-alias-${crypto.randomUUID()}`)
    await fs.symlink(literalRoot, alias, "dir")
    try {
      const canonicalRoot = AppFileSystem.resolve(literalRoot)
      if (process.platform === "darwin") {
        expect(AppFileSystem.resolve("/tmp")).toBe(AppFileSystem.resolve("/private/tmp"))
        expect(canonicalRoot.startsWith("/private/tmp/")).toBe(true)
      } else if (process.platform === "linux") {
        expect(AppFileSystem.resolve("/tmp")).toBe(path.resolve("/tmp"))
      }

      await using workspace = await tmpdir({ git: true })
      await Instance.provide({
        directory: workspace.path,
        fn: async () => {
          const bash = await initBash()
          const permissionPattern = async (workdir: string) => {
            const err = new Error("stop after permission")
            const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
            await expect(
              Effect.runPromise(
                bash.execute(
                  { command: "echo ok", workdir, description: "Echo from canonical temp dir" },
                  capture(requests, err),
                ),
              ),
            ).rejects.toThrow(err.message)
            const request = requests.find((item) => item.permission === "external_directory")
            expect(request).toBeDefined()
            return request!.patterns[0]
          }

          const equivalent = await Promise.all(
            [literalRoot, canonicalRoot, alias].map(permissionPattern),
          )
          expect(new Set(equivalent).size).toBe(1)
          expect(equivalent[0]).toBe(glob(path.join(canonicalRoot, "*")))
          expect(await permissionPattern(siblingRoot)).not.toBe(equivalent[0])
        },
      })
    } finally {
      await fs.rm(alias, { force: true })
      await fs.rm(literalRoot, { recursive: true, force: true })
      await fs.rm(siblingRoot, { recursive: true, force: true })
    }
  })

  if (process.platform === "win32") {
    test("normalizes external_directory workdir variants on Windows", async () => {
      const err = new Error("stop after permission")
      await using outerTmp = await tmpdir()
      await using tmp = await tmpdir()
      await Instance.provide({
        directory: tmp.path,
        fn: async () => {
          const bash = await initBash()
          const want = Filesystem.normalizePathPattern(path.join(outerTmp.path, "*"))

          for (const dir of forms(outerTmp.path)) {
            const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
            await expect(
              Effect.runPromise(
                bash.execute(
                  {
                    command: "echo ok",
                    workdir: dir,
                    description: "Echo from external dir",
                  },
                  capture(requests, err),
                ),
              ),
            ).rejects.toThrow(err.message)

            const extDirReq = requests.find((r) => r.permission === "external_directory")
            expect({ dir, patterns: extDirReq?.patterns, always: extDirReq?.always }).toEqual({
              dir,
              patterns: [want],
              always: [want],
            })
          }
        },
      })
    })

    if (bash) {
      test(
        "uses Git Bash /tmp semantics for external workdir",
        withShell({ label: "bash", shell: bash }, async () => {
          await Instance.provide({
            directory: projectRoot,
            fn: async () => {
              const bash = await initBash()
              const err = new Error("stop after permission")
              const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
              const want = glob(path.join(os.tmpdir(), "*"))
              await expect(
                Effect.runPromise(
                  bash.execute(
                    {
                      command: "echo ok",
                      workdir: "/tmp",
                      description: "Echo from Git Bash tmp",
                    },
                    capture(requests, err),
                  ),
                ),
              ).rejects.toThrow(err.message)
              expect(requests[0]).toMatchObject({
                permission: "external_directory",
                patterns: [want],
                always: [want],
              })
            },
          })
        }),
      )

      test(
        "uses Git Bash /tmp semantics for external file paths",
        withShell({ label: "bash", shell: bash }, async () => {
          await Instance.provide({
            directory: projectRoot,
            fn: async () => {
              const bash = await initBash()
              const err = new Error("stop after permission")
              const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
              const want = glob(path.join(os.tmpdir(), "*"))
              await expect(
                Effect.runPromise(
                  bash.execute(
                    {
                      command: "cat /tmp/opencode-does-not-exist",
                      description: "Read Git Bash tmp file",
                    },
                    capture(requests, err),
                  ),
                ),
              ).rejects.toThrow(err.message)
              expect(requests[0]).toMatchObject({
                permission: "external_directory",
                patterns: [want],
                always: [want],
              })
            },
          })
        }),
      )
    }
  }

  each("asks for external_directory permission when file arg is outside project", async () => {
    await using outerTmp = await tmpdir({
      init: async (dir) => {
        await Bun.write(path.join(dir, "outside.txt"), "x")
      },
    })
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const bash = await initBash()
        const err = new Error("stop after permission")
        const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
        const filepath = path.join(outerTmp.path, "outside.txt")
        await expect(
          Effect.runPromise(
            bash.execute(
              {
                command: `cat ${filepath}`,
                description: "Read external file",
              },
              capture(requests, err),
            ),
          ),
        ).rejects.toThrow(err.message)
        const extDirReq = requests.find((r) => r.permission === "external_directory")
        const expected = glob(path.join(outerTmp.path, "*"))
        expect(extDirReq).toBeDefined()
        expect(extDirReq!.patterns).toContain(expected)
        expect(extDirReq!.always).toContain(expected)
      },
    })
  })

  each("does not ask for external_directory permission when rm inside project", async () => {
    await using tmp = await tmpdir({
      init: async (dir) => {
        await Bun.write(path.join(dir, "tmpfile"), "x")
      },
    })
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const bash = await initBash()
        const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
        await Effect.runPromise(
          bash.execute(
            {
              command: `rm -rf ${path.join(tmp.path, "nested")}`,
              description: "Remove nested dir",
            },
            capture(requests),
          ),
        )
        const extDirReq = requests.find((r) => r.permission === "external_directory")
        expect(extDirReq).toBeUndefined()
      },
    })
  })

  each("includes always patterns for auto-approval", async () => {
    await using tmp = await tmpdir()
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const bash = await initBash()
        const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
        await Effect.runPromise(
          bash.execute(
            {
              command: "git log --oneline -5",
              description: "Git log",
            },
            capture(requests),
          ),
        )
        expect(requests.length).toBe(1)
        expect(requests[0].always.length).toBeGreaterThan(0)
        expect(requests[0].always.some((item) => item.endsWith("*"))).toBe(true)
      },
    })
  })

  each("does not ask for bash permission when command is cd only", async () => {
    await using tmp = await tmpdir()
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const bash = await initBash()
        const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
        await Effect.runPromise(
          bash.execute(
            {
              command: "cd .",
              description: "Stay in current directory",
            },
            capture(requests),
          ),
        )
        const bashReq = requests.find((r) => r.permission === "bash")
        expect(bashReq).toBeUndefined()
      },
    })
  })

  each("matches redirects in destructive permission pattern", async () => {
    await using tmp = await tmpdir()
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const bash = await initBash()
        const err = new Error("stop after permission")
        const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
        await expect(
          Effect.runPromise(
            bash.execute(
              { command: "echo test > output.txt", description: "Redirect test output" },
              capture(requests, err),
            ),
          ),
        ).rejects.toThrow(err.message)
        const destructiveReq = requests.find((r) => r.permission === "bash_destructive")
        expect(destructiveReq).toBeDefined()
        expect(destructiveReq!.patterns).toContain("echo test > output.txt")
      },
    })
  })

  each("always pattern has space before wildcard to not include different commands", async () => {
    await using tmp = await tmpdir()
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const bash = await initBash()
        const requests: Array<Omit<Permission.Request, "id" | "sessionID" | "tool">> = []
        await Effect.runPromise(bash.execute({ command: "ls -la", description: "List" }, capture(requests)))
        const bashReq = requests.find((r) => r.permission === "bash")
        expect(bashReq).toBeDefined()
        expect(bashReq!.always[0]).toBe("ls *")
      },
    })
  })
})

describe("tool.bash abort", () => {
  test("preserves output when aborted", async () => {
    await Instance.provide({
      directory: projectRoot,
      fn: async () => {
        const bash = await initBash()
        const controller = new AbortController()
        const collected: string[] = []
        const res = await Effect.runPromise(
          bash.execute(
            {
              command: `echo before && sleep 30`,
              description: "Long running command",
            },
            {
              ...ctx,
              abort: controller.signal,
              metadata: (input) =>
                Effect.sync(() => {
                  const output = (input.metadata as { output?: string })?.output
                  if (output && output.includes("before") && !controller.signal.aborted) {
                    collected.push(output)
                    controller.abort()
                  }
                }),
            },
          ),
        )
        expect(res.output).toContain("before")
        expect(res.output).toContain("User aborted the command")
        expect(collected.length).toBeGreaterThan(0)
      },
    })
  }, 15_000)

  test("terminates command on timeout", async () => {
    await Instance.provide({
      directory: projectRoot,
      fn: async () => {
        const bash = await initBash()
        const result = await Effect.runPromise(
          bash.execute(
            {
              command: `echo started && sleep 60`,
              description: "Timeout test",
              timeout: 500,
            },
            ctx,
          ),
        )
        expect(result.output).toContain("started")
        expect(result.output).toContain("bash tool terminated command after exceeding timeout")
        expect(result.output).toContain("retry with a larger timeout value in milliseconds")
      },
    })
  }, 15_000)

  test.skipIf(process.platform === "win32")("captures stderr in output", async () => {
    await Instance.provide({
      directory: projectRoot,
      fn: async () => {
        const bash = await initBash()
        const result = await Effect.runPromise(
          bash.execute(
            {
              command: `echo stdout_msg && echo stderr_msg >&2`,
              description: "Stderr test",
            },
            ctx,
          ),
        )
        expect(result.output).toContain("stdout_msg")
        expect(result.output).toContain("stderr_msg")
        expect(result.metadata.exit).toBe(0)
      },
    })
  })

  test("returns non-zero exit code", async () => {
    await Instance.provide({
      directory: projectRoot,
      fn: async () => {
        const bash = await initBash()
        const result = await Effect.runPromise(
          bash.execute(
            {
              command: `exit 42`,
              description: "Non-zero exit",
            },
            ctx,
          ),
        )
        expect(result.metadata.exit).toBe(42)
      },
    })
  })

  test("streams metadata updates progressively", async () => {
    await Instance.provide({
      directory: projectRoot,
      fn: async () => {
        const bash = await initBash()
        const updates: string[] = []
        const result = await Effect.runPromise(
          bash.execute(
            {
              command: `echo first && sleep 0.1 && echo second`,
              description: "Streaming test",
            },
            {
              ...ctx,
              metadata: (input) =>
                Effect.sync(() => {
                  const output = (input.metadata as { output?: string })?.output
                  if (output) updates.push(output)
                }),
            },
          ),
        )
        expect(result.output).toContain("first")
        expect(result.output).toContain("second")
        expect(updates.length).toBeGreaterThan(1)
      },
    })
  })
})

describe("tool.bash truncation", () => {
  test("truncates output exceeding line limit", async () => {
    await Instance.provide({
      directory: projectRoot,
      fn: async () => {
        const bash = await initBash()
        const lineCount = Truncate.MAX_LINES + 500
        const result = await Effect.runPromise(
          bash.execute(
            {
              command: fill("lines", lineCount),
              description: "Generate lines exceeding limit",
            },
            ctx,
          ),
        )
        mustTruncate(result)
        expect(result.output).toMatch(/\.\.\.output truncated\.\.\./)
        expect(result.output).toMatch(/Full output saved to:\s+\S+/)
      },
    })
  })

  test("truncates output exceeding byte limit", async () => {
    await Instance.provide({
      directory: projectRoot,
      fn: async () => {
        const bash = await initBash()
        const byteCount = Truncate.MAX_BYTES + 10000
        const result = await Effect.runPromise(
          bash.execute(
            {
              command: fill("bytes", byteCount),
              description: "Generate bytes exceeding limit",
            },
            ctx,
          ),
        )
        mustTruncate(result)
        expect(result.output).toMatch(/\.\.\.output truncated\.\.\./)
        expect(result.output).toMatch(/Full output saved to:\s+\S+/)
      },
    })
  })

  test("does not truncate small output", async () => {
    await Instance.provide({
      directory: projectRoot,
      fn: async () => {
        const bash = await initBash()
        const result = await Effect.runPromise(
          bash.execute(
            {
              command: "echo hello",
              description: "Echo hello",
            },
            ctx,
          ),
        )
        expect((result.metadata as { truncated?: boolean }).truncated).toBe(false)
        expect(result.output).toContain("hello")
      },
    })
  })

  test("full output is saved to file when truncated", async () => {
    await Instance.provide({
      directory: projectRoot,
      fn: async () => {
        const bash = await initBash()
        const lineCount = Truncate.MAX_LINES + 100
        const result = await Effect.runPromise(
          bash.execute(
            {
              command: fill("lines", lineCount),
              description: "Generate lines for file check",
            },
            ctx,
          ),
        )
        mustTruncate(result)

        const filepath = (result.metadata as { outputPath?: string }).outputPath
        expect(filepath).toBeTruthy()

        const saved = await Filesystem.readText(filepath!)
        const lines = saved.trim().split(/\r?\n/)
        expect(lines.length).toBe(lineCount)
        expect(lines[0]).toBe("1")
        expect(lines[lineCount - 1]).toBe(String(lineCount))
      },
    })
  })
})
