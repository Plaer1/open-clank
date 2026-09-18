import os from "node:os"
import path from "node:path"
import { existsSync, realpathSync, statSync } from "node:fs"
import { Shell } from "@/shell/shell"
import { Global } from "@/global"

const ENV_ALLOW = new Set([
  "PATH",
  "LANG",
  "LANGUAGE",
  "TERM",
  "COLORTERM",
  "COLUMNS",
  "LINES",
  "TMPDIR",
  "TMP",
  "TEMP",
  "SYSTEMROOT",
  "WINDIR",
  "COMSPEC",
  "PATHEXT",
  "PYTHONIOENCODING",
])

export function filterShellEnvironment(env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  return Object.fromEntries(
    Object.entries(env).filter(
      ([name, value]) => value !== undefined && (ENV_ALLOW.has(name.toUpperCase()) || name.toUpperCase().startsWith("LC_")),
    ),
  )
}

export function minimalShellEnvironment(env: NodeJS.ProcessEnv, cwd: string): NodeJS.ProcessEnv {
  const result = filterShellEnvironment(env)
  result.PATH ??= process.platform === "win32" ? process.env.PATH ?? "" : "/usr/local/bin:/usr/bin:/bin"
  result.LANG ??= "C.UTF-8"
  result.TERM ??= "xterm-256color"
  result.COLUMNS ??= "120"
  result.LINES ??= "40"
  result.HOME = cwd
  if (process.platform === "win32") {
    result.USERPROFILE = cwd
    result.PYTHONIOENCODING ??= "utf-8"
  }
  return result
}

export type ShellInvocation = {
  executable: string
  args: string[]
  containment: "bwrap" | "off"
  network: "enabled" | "disabled"
}

function canonical(file: string) {
  try {
    return realpathSync(file)
  } catch {
    return path.resolve(file)
  }
}

function contains(root: string, target: string) {
  const relative = path.relative(root, target)
  return relative === "" || (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`))
}

function dirMounts(parent: string, child: string) {
  const relative = path.relative(parent, child)
  if (!relative || relative === "." || relative === ".." || relative.startsWith(`..${path.sep}`)) return []
  const result: string[] = []
  let current = parent
  for (const part of relative.split(path.sep)) {
    current = path.join(current, part)
    result.push("--dir", current)
  }
  return result
}

let workingBwrap: { path: string; executable: string | null } | undefined
let workingNetworkBwrap: { path: string; executable: string | null } | undefined

function bwrap() {
  const searchPath = process.env.PATH ?? ""
  if (workingBwrap?.path === searchPath) return workingBwrap.executable
  if (process.platform === "win32") {
    workingBwrap = { path: searchPath, executable: null }
    return null
  }
  const executable = searchPath ? Bun.which("bwrap") : null
  if (!executable) {
    workingBwrap = { path: searchPath, executable: null }
    return null
  }
  try {
    const probe = Bun.spawnSync([
      executable,
      "--die-with-parent",
      "--ro-bind",
      "/",
      "/",
      "--proc",
      "/proc",
      "--dev",
      "/dev",
      "--",
      "/bin/true",
    ])
    workingBwrap = {
      path: searchPath,
      executable: probe.exitCode === 0 ? executable : null,
    }
  } catch {
    workingBwrap = { path: searchPath, executable: null }
  }
  return workingBwrap.executable
}

function networkBwrap() {
  const searchPath = process.env.PATH ?? ""
  if (workingNetworkBwrap?.path === searchPath) {
    return workingNetworkBwrap.executable
  }
  const executable = bwrap()
  if (!executable) {
    workingNetworkBwrap = { path: searchPath, executable: null }
    return null
  }
  try {
    const probe = Bun.spawnSync([
      executable,
      "--die-with-parent",
      "--unshare-net",
      "--ro-bind",
      "/",
      "/",
      "--proc",
      "/proc",
      "--dev",
      "/dev",
      "--",
      "/bin/true",
    ])
    workingNetworkBwrap = {
      path: searchPath,
      executable: probe.exitCode === 0 ? executable : null,
    }
  } catch {
    workingNetworkBwrap = { path: searchPath, executable: null }
  }
  return workingNetworkBwrap.executable
}

function policy() {
  const value = (process.env.OPEN_CLANK_SHELL_SANDBOX ?? "auto").trim().toLowerCase()
  if (value === "required" || value === "auto" || value === "off") return value
  throw new Error("OPEN_CLANK_SHELL_SANDBOX must be required, auto, or off")
}

function networkPolicy(value?: "enabled" | "disabled") {
  const network = (value ?? process.env.OPEN_CLANK_SHELL_NETWORK ?? "enabled").trim().toLowerCase()
  if (network === "enabled" || network === "disabled") return network
  throw new Error("shell network policy must be enabled or disabled")
}

function shellArgs(shell: string, command: string) {
  const name = Shell.name(shell)
  if (name === "zsh") return ["-f", "-c", command]
  if (name === "bash") return ["--noprofile", "--norc", "-c", command]
  return ["-c", command]
}

function roots(input: { workspace: string; writableRoots?: string[] }) {
  const canonicalRoots = Array.from(
    new Set([input.workspace, ...(input.writableRoots ?? [])].map(canonical)),
  ).sort((a, b) => a.length - b.length)
  return canonicalRoots.filter(
    (candidate, index) =>
      !canonicalRoots.some((parent, parentIndex) => parentIndex < index && contains(parent, candidate)),
  )
}

function maskedRoots(writableRoots: string[]) {
  const runtimeCache = process.env.MIMOCODE_HOME
    ? path.join(process.env.MIMOCODE_HOME, "cache")
    : Global.Path.cache
  const candidates = [
    { value: Global.Path.data, maskWhenDisjoint: true },
    { value: Global.Path.config, maskWhenDisjoint: true },
    { value: Global.Path.state, maskWhenDisjoint: true },
    { value: runtimeCache, maskWhenDisjoint: false },
    { value: process.env.MIMOCODE_HOME, maskWhenDisjoint: true },
    { value: process.env.OPEN_CLANK_CONTROL_DATA_DIR, maskWhenDisjoint: true },
    { value: process.env.OPEN_CLANK_SKILLS_DIR, maskWhenDisjoint: true },
  ]
    .filter((item): item is { value: string; maskWhenDisjoint: boolean } => Boolean(item.value))
    .map((item) => ({
      lexical: path.resolve(item.value),
      path: canonical(item.value),
      maskWhenDisjoint: item.maskWhenDisjoint,
    }))
    .filter((item) => existsSync(item.path))
    .map((item) => ({ ...item, directory: statSync(item.path).isDirectory() }))
    .filter(
      (item) =>
        item.maskWhenDisjoint ||
        writableRoots.some(
          (root) => contains(root, item.path) || contains(root, item.lexical),
        ),
    )
    .sort((a, b) => a.path.length - b.path.length)
  return candidates.filter(
    (candidate, index) =>
      !candidates.some((parent, parentIndex) => parentIndex < index && contains(parent.path, candidate.path)),
  )
}

export function resolveShellInvocation(input: {
  shell: string
  command: string
  cwd: string
  workspace: string
  writableRoots?: string[]
  network?: "enabled" | "disabled"
}): ShellInvocation {
  const network = networkPolicy(input.network)
  const writableRoots = roots(input)
  const cwd = canonical(input.cwd)
  if (!writableRoots.some((root) => contains(root, cwd))) {
    throw new Error("shell cwd must stay inside an approved writable root")
  }

  const mode = policy()
  const name = Shell.name(input.shell)
  if (process.platform === "win32" && (name === "powershell" || name === "pwsh")) {
    if (network === "disabled") throw new Error("network-disabled shell execution requires bubblewrap")
    if (mode === "required") {
      throw new Error(
        "shell containment is unavailable on this platform and OPEN_CLANK_SHELL_SANDBOX=required forbids OS-boundary execution",
      )
    }
    return {
      executable: input.shell,
      args: ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", `${Shell.POWERSHELL_UTF8_PREFIX}${input.command}`],
      containment: "off",
      network,
    }
  }
  if (process.platform === "win32") {
    if (network === "disabled") throw new Error("network-disabled shell execution requires bubblewrap")
    if (mode === "required") {
      throw new Error(
        "shell containment is unavailable on this platform and OPEN_CLANK_SHELL_SANDBOX=required forbids OS-boundary execution",
      )
    }
    return {
      executable: input.shell,
      args: ["/c", `${Shell.CMD_UTF8_PREFIX}${input.command}`],
      containment: "off",
      network,
    }
  }

  if (mode === "off") {
    if (network === "disabled") {
      throw new Error("network-disabled shell execution requires bubblewrap")
    }
    return {
      executable: input.shell,
      args: shellArgs(input.shell, input.command),
      containment: "off",
      network,
    }
  }

  const executable = network === "disabled" ? networkBwrap() : bwrap()
  if (!executable) {
    if (network === "disabled") {
      throw new Error("network-disabled shell containment is unavailable")
    }
    if (mode === "required") {
      throw new Error(
        "shell containment is unavailable and OPEN_CLANK_SHELL_SANDBOX=required forbids OS-boundary execution",
      )
    }
    return {
      executable: input.shell,
      args: shellArgs(input.shell, input.command),
      containment: "off",
      network,
    }
  }

  const args = [
    "--die-with-parent",
    "--new-session",
    "--unshare-pid",
    "--unshare-ipc",
    "--unshare-uts",
    "--ro-bind",
    "/",
    "/",
    "--proc",
    "/proc",
    "--dev",
    "/dev",
    "--tmpfs",
    "/tmp",
    "--tmpfs",
    "/run",
  ]
  if (network === "disabled") args.push("--unshare-net")
  const temp = canonical(os.tmpdir())
  const masks = maskedRoots(writableRoots)
  const beforeWritable = masks.filter((mask) =>
    writableRoots.some((root) => contains(mask.path, root)),
  )
  const afterWritable = masks.filter((mask) => !beforeWritable.includes(mask))
  for (const mask of beforeWritable) {
    if (contains(temp, mask.path)) args.push(...dirMounts(temp, mask.path))
    if (contains("/run", mask.path) && mask.path !== "/run") args.push(...dirMounts("/run", mask.path))
    if (mask.directory) args.push("--tmpfs", mask.path)
    else args.push("--ro-bind", "/dev/null", mask.path)
  }
  for (const root of writableRoots) {
    const maskedParent = beforeWritable
      .filter((mask) => contains(mask.path, root))
      .sort((a, b) => b.path.length - a.path.length)[0]
    if (maskedParent) args.push(...dirMounts(maskedParent.path, root))
    else {
      if (contains(temp, root)) args.push(...dirMounts(temp, root))
      if (contains("/run", root) && root !== "/run") args.push(...dirMounts("/run", root))
    }
  }
  for (const root of writableRoots) {
    args.push("--bind", root, root)
  }
  for (const root of afterWritable) {
    if (contains("/run", root.path) && root.path !== "/run") args.push(...dirMounts("/run", root.path))
    if (root.directory) args.push("--tmpfs", root.path)
    else args.push("--ro-bind", "/dev/null", root.path)
  }
  const runtimeCacheTarget = path.resolve(
    process.env.MIMOCODE_HOME
      ? path.join(process.env.MIMOCODE_HOME, "cache")
      : Global.Path.cache,
  )
  const runtimeCache = canonical(runtimeCacheTarget)
  if (
    existsSync(runtimeCache) &&
    !writableRoots.some(
      (root) => contains(runtimeCache, root) || contains(runtimeCacheTarget, root),
    )
  ) {
    const maskedParent = masks
      .filter(
        (mask) =>
          contains(mask.path, runtimeCache) ||
          contains(mask.path, runtimeCacheTarget) ||
          contains(mask.lexical, runtimeCacheTarget),
      )
      .sort((a, b) => b.path.length - a.path.length)[0]
    if (maskedParent) args.push(...dirMounts(maskedParent.path, runtimeCacheTarget))
    args.push(
      writableRoots.some((root) => contains(root, runtimeCache)) ? "--bind" : "--ro-bind",
      runtimeCache,
      runtimeCacheTarget,
    )
  }
  args.push("--chdir", cwd, "--", input.shell, ...shellArgs(input.shell, input.command))
  return { executable, args, containment: "bwrap", network }
}
