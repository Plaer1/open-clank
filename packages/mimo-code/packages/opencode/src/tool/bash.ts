import z from "zod"
import os from "os"
import { createWriteStream, readFileSync } from "node:fs"
import * as Tool from "./tool"
import path from "path"
import DESCRIPTION from "./bash.txt"
import GPT_DESCRIPTION from "./bash.gpt.txt"
import { Log } from "../util"
import { Instance } from "../project/instance"
import { lazy } from "@/util/lazy"
import { Language, type Node } from "web-tree-sitter"

import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { fileURLToPath } from "url"
import { Flag } from "@/flag/flag"
import { Shell } from "@/shell/shell"

import { SessionCwd } from "./session-cwd"
import { BashArity } from "@/permission/arity"
import * as Truncate from "./truncate"
import { Plugin } from "@/plugin"
import { Effect, Stream } from "effect"
import { ChildProcess } from "effect/unstable/process"
import { ChildProcessSpawner } from "effect/unstable/process/ChildProcessSpawner"
import * as BashInteractive from "./bash-interactive"
import * as BashTokenEfficient from "./bash_token_efficient_pipeline"
import * as BashTokenEfficientHeuristic from "./bash_token_efficient_heuristic"
import { filterShellEnvironment, minimalShellEnvironment, resolveShellInvocation } from "./shell-containment"
import { StreamingSecurityRedactor } from "@/util/security-redact"
import { assertProjectShellPolicy } from "./project-policy"

const MAX_METADATA_LENGTH = 30_000
const DEFAULT_TIMEOUT = Flag.MIMOCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS || 2 * 60 * 1000
const PS = new Set(["powershell", "pwsh"])
const CWD = new Set(["cd", "push-location", "set-location"])
const FILES = new Set([
  ...CWD,
  "rm",
  "cp",
  "mv",
  "mkdir",
  "touch",
  "chmod",
  "chown",
  "cat",
  // Leave PowerShell aliases out for now. Common ones like cat/cp/mv/rm/mkdir
  // already hit the entries above, and alias normalization should happen in one
  // place later so we do not risk double-prompting.
  "get-content",
  "set-content",
  "add-content",
  "copy-item",
  "move-item",
  "remove-item",
  "new-item",
  "rename-item",
])
const FLAGS = new Set(["-destination", "-literalpath", "-path"])
const SWITCHES = new Set(["-confirm", "-debug", "-force", "-nonewline", "-recurse", "-verbose", "-whatif"])

export function bashDescription(gpt = false) {
  const name = Shell.name(Shell.acceptable())
  const chaining =
    name === "powershell"
      ? "If the commands depend on each other and must run sequentially, avoid '&&' in this shell because Windows PowerShell 5.1 does not support it. Use PowerShell conditionals such as `cmd1; if ($?) { cmd2 }` when later commands must depend on earlier success."
      : "If the commands depend on each other and must run sequentially, use a single Bash call with '&&' to chain them together (e.g., `git add . && git commit -m \"message\" && git push`). For instance, if one operation must complete before another starts (like mkdir before cp, apply_patch before Bash for git operations, or git add before git commit), run these operations sequentially instead."
  return (gpt ? GPT_DESCRIPTION : DESCRIPTION)
    .replaceAll("${directory}", Instance.directory)
    .replaceAll("${os}", process.platform)
    .replaceAll("${shell}", name)
    .replaceAll("${chaining}", chaining)
    .replaceAll("${maxLines}", String(Truncate.MAX_LINES))
    .replaceAll("${maxBytes}", String(Truncate.MAX_BYTES))
}

export function sanitizeShellEnvironment(env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  return filterShellEnvironment(env)
}

// Irreversible file/directory removal commands. Names are matched
// case-insensitively for PowerShell; bash is case-sensitive.
const DELETE_COMMANDS = new Set([
  "rm",
  "rmdir",
  "unlink",
  "shred",
  // Windows / PowerShell removal verbs and their common aliases. `remove-item`
  // is the canonical verb; `ri`, `rd`, `del`, `erase` are aliases.
  "del",
  "erase",
  "rd",
  "remove-item",
  "ri",
])

// Git subcommands that replace history, working-tree state, or remote refs.
// Value is the set of tokens (flag or subcommand keyword) that must appear
// anywhere in the argv for the invocation to require confirmation. An empty
// set means the subcommand is destructive on its own.
const GIT_DESTRUCTIVE = new Map<string, Set<string>>([
  ["reset", new Set(["--hard"])],
  ["clean", new Set(["-f", "-ff", "-fd", "-fdx", "-df", "-dfx", "-fx", "--force"])],
  ["branch", new Set(["-D", "--delete"])],
  ["tag", new Set(["-d", "--delete"])],
  ["worktree", new Set(["remove"])],
  ["push", new Set(["--force", "--force-with-lease", "-f"])],
  ["stash", new Set(["drop", "clear"])],
  ["checkout", new Set()],
  ["restore", new Set()],
  ["rebase", new Set()],
  ["cherry-pick", new Set()],
  ["revert", new Set()],
  ["switch", new Set(["-f", "--force", "--discard-changes"])],
  ["commit", new Set(["--amend"])],
])
const INDIRECT_COMMANDS = new Set([
  "alias",
  "builtin",
  "busybox",
  "busybox.exe",
  "chroot",
  "chrt",
  "command",
  "env",
  "exec",
  "fakeroot",
  "ionice",
  "nice",
  "nohup",
  "setsid",
  "stdbuf",
  "systemd-run",
  "taskset",
  "time",
  "timeout",
  "unshare",
  "watch",
  "xargs",
])
const SHELL_INTERPRETERS = new Set(["bash", "cmd", "cmd.exe", "dash", "fish", "ksh", "powershell", "powershell.exe", "pwsh", "sh", "zsh"])
const CODE_EVAL = new Map<string, Set<string>>([
  ["node", new Set(["-e", "--eval"])],
  ["node.exe", new Set(["-e", "--eval"])],
  ["nodejs", new Set(["-e", "--eval"])],
  ["bun", new Set(["-e", "--eval"])],
  ["bun.exe", new Set(["-e", "--eval"])],
  ["deno", new Set(["eval"])],
  ["deno.exe", new Set(["eval"])],
  ["perl", new Set(["-e", "-E"])],
  ["php", new Set(["-r"])],
  ["python", new Set(["-c"])],
  ["python3", new Set(["-c"])],
  ["ruby", new Set(["-e"])],
])
const INTERPRETER_INSPECTION_FLAGS = new Set(["--help", "--version"])
const SCRIPT_SUFFIXES = [".bash", ".bat", ".cmd", ".cjs", ".js", ".mjs", ".php", ".pl", ".ps1", ".py", ".rb", ".sh", ".zsh"]
const FILESYSTEM_MUTATORS = new Set(["chmod", "chown", "chgrp", "setfacl", "takeown", "icacls"])
const REMOTE_CONTROL_COMMANDS = new Set(["crictl", "ctr", "docker", "kubectl", "machinectl", "nerdctl", "podman", "virsh"])
const PERSISTENCE_COMMANDS = new Set(["at", "batch", "crontab", "launchctl", "schtasks"])
const NETWORK_COMMANDS = new Set(["curl", "ftp", "nc", "ncat", "netcat", "rsync", "scp", "sftp", "socat", "ssh", "telnet", "wget"])
const PACKAGE_COMMANDS = new Set([
  "apk", "apt", "apt-get", "brew", "cargo", "choco", "dnf", "dotnet", "gem", "go", "npm", "pacman",
  "pip", "pip3", "pipx", "pnpm", "poetry", "scoop", "uv", "winget", "yarn", "yum", "zypper",
])
const PACKAGE_MUTATIONS = new Set([
  "add", "build", "develop", "global", "install", "link", "publish", "remove", "sync", "uninstall", "unlink",
  "update", "upgrade",
])
const SERVICE_COMMANDS = new Set(["launchctl", "rc-service", "sc", "service", "systemctl"])
const SERVICE_MUTATIONS = new Set([
  "add-wants", "daemon-reexec", "daemon-reload", "delete", "disable", "edit", "enable", "kill", "link", "mask",
  "preset", "reload", "restart", "revert", "set-default", "start", "stop", "unmask",
])

const Parameters = z.object({
  command: z.string().describe("The command to execute"),
  network: z
    .enum(["enabled", "disabled"])
    .describe("Network policy for this command. Defaults to enabled.")
    .optional(),
  timeout: z.number().describe("Optional timeout in milliseconds").optional(),
  workdir: z
    .string()
    .describe(
      `The working directory to run the command in. Defaults to the current directory. Use this instead of 'cd' commands.`,
    )
    .optional(),
  interactive: z
    .boolean()
    .describe(
      "Set to true when the command requires user interaction (password input, y/N confirmation, SSH key passphrase, etc). The terminal will be handed to the user for direct interaction.",
    )
    .optional(),
  description: z
    .string()
    .describe(
      "Clear, concise description of what this command does in 5-10 words. Examples:\nInput: ls\nOutput: Lists files in current directory\n\nInput: git status\nOutput: Shows working tree status\n\nInput: npm install\nOutput: Installs package dependencies\n\nInput: mkdir foo\nOutput: Creates directory 'foo'",
    ),
})

type Part = {
  type: string
  text: string
}

type Scan = {
  dirs: Set<string>
  patterns: Set<string>
  always: Set<string>
  destructive: Set<string>
}

type Chunk = {
  text: string
  size: number
}

export const log = Log.create({ service: "bash-tool" })

const resolveWasm = (asset: string) => {
  if (asset.startsWith("file://")) return fileURLToPath(asset)
  if (asset.startsWith("/") || /^[a-z]:/i.test(asset)) return asset
  const url = new URL(asset, import.meta.url)
  return fileURLToPath(url)
}

function parts(node: Node) {
  const out: Part[] = []
  for (let i = 0; i < node.childCount; i++) {
    const child = node.child(i)
    if (!child) continue
    if (child.type === "command_elements") {
      for (let j = 0; j < child.childCount; j++) {
        const item = child.child(j)
        if (!item || item.type === "command_argument_sep" || item.type === "redirection") continue
        out.push({ type: item.type, text: item.text })
      }
      continue
    }
    if (
      child.type !== "command_name" &&
      child.type !== "command_name_expr" &&
      child.type !== "word" &&
      child.type !== "string" &&
      child.type !== "raw_string" &&
      child.type !== "concatenation"
    ) {
      continue
    }
    out.push({ type: child.type, text: child.text })
  }
  return out
}

function source(node: Node) {
  return (node.parent?.type === "redirected_statement" ? node.parent.text : node.text).trim()
}

function commands(node: Node) {
  return node.descendantsOfType("command").filter((child): child is Node => Boolean(child))
}

// Returns true when one command directly removes data or invokes a Git
// operation that can replace history or working-tree state.
function gitCommand(tokens: string[]) {
  const optionsWithValue = new Set([
    "-C",
    "-c",
    "--config-env",
    "--exec-path",
    "--git-dir",
    "--namespace",
    "--super-prefix",
    "--work-tree",
  ])
  let index = 1
  while (index < tokens.length) {
    const token = tokens[index]
    if (token === "--") {
      index++
      break
    }
    if (!token.startsWith("-")) break
    const key = token.includes("=") ? token.slice(0, token.indexOf("=")) : token
    index += optionsWithValue.has(key) && !token.includes("=") ? 2 : 1
  }
  const subcommand = tokens[index]?.toLowerCase()
  if (!subcommand) return
  return { subcommand, args: tokens.slice(index + 1) }
}

function isDestructiveCommand(tokens: string[], ps: boolean) {
  if (tokens.length === 0) return false
  const head = ps ? tokens[0].toLowerCase() : tokens[0]
  if (DELETE_COMMANDS.has(head)) return true
  if (head === "git" && tokens.length >= 2) {
    const invocation = gitCommand(tokens)
    if (!invocation) return false
    const flags = GIT_DESTRUCTIVE.get(invocation.subcommand)
    if (!flags) return false
    if (flags.size === 0) return true
    return invocation.args.some((token) => flags.has(token))
  }
  return false
}

function staticShellWord(text: string, ps: boolean) {
  let out = ""
  let quote = ""
  for (let index = 0; index < text.length; index++) {
    const char = text[index]
    if (quote) {
      if (char === quote) quote = ""
      else out += char
      continue
    }
    if (char === "'" || char === '"') {
      quote = char
      continue
    }
    if (!ps && char === "\\") {
      index++
      if (index >= text.length) return
      out += text[index]
      continue
    }
    if (char === "$" || char === "`" || "*?[]{}".includes(char)) return
    out += char
  }
  if (quote) return
  return ps ? out.toLowerCase() : out
}

function activeDynamicShell(text: string) {
  let quote = ""
  let escaped = false
  for (let index = 0; index < text.length; index++) {
    const char = text[index]
    if (escaped) {
      escaped = false
      continue
    }
    if (quote === "'") {
      if (char === "'") quote = ""
      continue
    }
    if (char === "\\") {
      escaped = true
      continue
    }
    if (char === "'" || char === '"') {
      quote = quote === char ? "" : quote || char
      continue
    }
    if (char === "`") return true
    const next = text[index + 1]
    if ((char === "$" && next === "(") || (!quote && (char === "<" || char === ">") && next === "(")) return true
  }
  return false
}

function activeOutputRedirect(text: string) {
  let quote = ""
  let escaped = false
  for (const char of text) {
    if (escaped) {
      escaped = false
      continue
    }
    if (quote === "'") {
      if (char === "'") quote = ""
      continue
    }
    if (char === "\\") {
      escaped = true
      continue
    }
    if (char === "'" || char === '"') {
      quote = quote === char ? "" : quote || char
      continue
    }
    if (!quote && char === ">") return true
  }
  return false
}

function approvalWorthy(tokens: string[], ps: boolean) {
  if (!tokens.length) return false
  const command = staticShellWord(tokens[0], ps)
  if (!command) return true
  const words = [command, ...tokens.slice(1).map((token) => staticShellWord(token, ps) ?? token)]
  const head = path.basename(words[0]).toLowerCase()
  const args = words.slice(1)
  const loweredArgs = args.map((arg) => arg.toLowerCase())
  if (isDestructiveCommand(words, ps)) return true
  if (["clear-content", "truncate"].includes(head)) return true
  if (head === "dd" && args.some((arg) => arg.startsWith("of="))) return true
  if (head.startsWith("mkfs") || ["format", "format-volume", "wipefs", "clear-disk"].includes(head)) return true
  if (["sudo", "doas", "pkexec", "su", "eval", "source", ".", "trap"].includes(head)) return true
  if (INDIRECT_COMMANDS.has(head)) return true
  const pythonInterpreter = /^(?:python|pypy)\d*(?:\.\d+)*(?:\.exe)?$/.test(head)
  const codeInterpreter = /^(?:python|pypy|node|nodejs|bun|deno|perl|php|ruby)\d*(?:\.\d+)*(?:\.exe)?$/.test(head)
  const shellInterpreter = /^(?:bash|dash|fish|ksh|zsh)\d*(?:\.\d+)*(?:\.exe)?$/.test(head)
  const inspectionOnly =
    args.length > 0 &&
    args.every((arg) => INTERPRETER_INSPECTION_FLAGS.has(arg) || (pythonInterpreter && arg === "-V"))
  if ((SHELL_INTERPRETERS.has(head) || shellInterpreter) && !inspectionOnly) return true
  const evalFlags = CODE_EVAL.get(head)
  if ((evalFlags || codeInterpreter) && !inspectionOnly) return true
  const executable = words[0].replaceAll("\\", "/").toLowerCase()
  if (SCRIPT_SUFFIXES.some((suffix) => executable.endsWith(suffix))) return true
  if (
    executable.includes("/") &&
    !["/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/"].some((prefix) => executable.startsWith(prefix))
  ) return true
  if (FILESYSTEM_MUTATORS.has(head)) return true
  if (["cp", "mv", "install", "patch", "tee", "unzip"].includes(head)) return true
  if (head === "ln" && args.some((arg) => arg === "-f" || arg === "--force" || (/^-[^-]/.test(arg) && arg.includes("f")))) return true
  if (head === "sed" && args.some((arg) => arg === "--in-place" || arg.startsWith("-i"))) return true
  if (
    ["tar", "bsdtar", "gtar"].includes(head) &&
    args.some((arg) => ["--extract", "--get"].includes(arg) || (/^-[^-]/.test(arg) && arg.slice(1).includes("x")))
  ) return true
  if (head === "git" && loweredArgs.includes("apply")) return true
  if (args.some((arg) => arg.includes(">"))) return true
  if (REMOTE_CONTROL_COMMANDS.has(head)) return true
  if (PERSISTENCE_COMMANDS.has(head)) return true
  if (NETWORK_COMMANDS.has(head)) return true
  if (PACKAGE_COMMANDS.has(head) && loweredArgs.some((arg) => PACKAGE_MUTATIONS.has(arg))) return true
  if (SERVICE_COMMANDS.has(head) && loweredArgs.some((arg) => SERVICE_MUTATIONS.has(arg))) return true
  if (
    ["passwd", "chpasswd", "htpasswd", "git-credential", "kinit"].includes(head) ||
    (head === "gh" && loweredArgs.includes("auth")) ||
    (head === "gcloud" && loweredArgs.includes("auth")) ||
    (head === "npm" && loweredArgs.some((arg) => ["login", "logout", "token"].includes(arg))) ||
    (head === "security" && loweredArgs.some((arg) => ["add-generic-password", "delete-generic-password"].includes(arg)))
  ) return true
  if (
    (["npm", "pnpm", "yarn"].includes(head) && loweredArgs.some((arg) => ["exec", "dlx"].includes(arg))) ||
    (["uv", "pipx"].includes(head) && loweredArgs.includes("run"))
  ) return true
  if (head === "find" && args.some((arg) => ["-delete", "-exec", "-execdir", "-ok", "-okdir"].includes(arg))) return true
  return false
}

function unquote(text: string) {
  if (text.length < 2) return text
  const first = text[0]
  const last = text[text.length - 1]
  if ((first === '"' || first === "'") && first === last) return text.slice(1, -1)
  return text
}

function home(text: string) {
  if (text === "~") return os.homedir()
  if (text.startsWith("~/") || text.startsWith("~\\")) return path.join(os.homedir(), text.slice(2))
  return text
}

function envValue(key: string) {
  if (process.platform !== "win32") return process.env[key]
  const name = Object.keys(process.env).find((item) => item.toLowerCase() === key.toLowerCase())
  return name ? process.env[name] : undefined
}

function auto(key: string, cwd: string, shell: string) {
  const name = key.toUpperCase()
  if (name === "HOME") return os.homedir()
  if (name === "PWD") return cwd
  if (name === "PSHOME") return path.dirname(shell)
}

function expand(text: string, cwd: string, shell: string) {
  const out = unquote(text)
    .replace(/\$\{env:([^}]+)\}/gi, (_, key: string) => envValue(key) || "")
    .replace(/\$env:([A-Za-z_][A-Za-z0-9_]*)/gi, (_, key: string) => envValue(key) || "")
    .replace(/\$(HOME|PWD|PSHOME)(?=$|[\\/])/gi, (_, key: string) => auto(key, cwd, shell) || "")
  return home(out)
}

function provider(text: string) {
  const match = text.match(/^([A-Za-z]+)::(.*)$/)
  if (match) {
    if (match[1].toLowerCase() !== "filesystem") return
    return match[2]
  }
  const prefix = text.match(/^([A-Za-z]+):(.*)$/)
  if (!prefix) return text
  if (prefix[1].length === 1) return text
  return
}

function dynamic(text: string, ps: boolean) {
  if (text.startsWith("(") || text.startsWith("@(")) return true
  if (text.includes("$(") || text.includes("${") || text.includes("`")) return true
  if (ps) return /\$(?!env:)/i.test(text)
  return text.includes("$")
}

function prefix(text: string) {
  const match = /[?*[]/.exec(text)
  if (!match) return text
  if (match.index === 0) return
  return text.slice(0, match.index)
}

function pathArgs(list: Part[], ps: boolean) {
  if (!ps) {
    return list
      .slice(1)
      .filter((item) => !item.text.startsWith("-") && !(list[0]?.text === "chmod" && item.text.startsWith("+")))
      .map((item) => item.text)
  }

  const out: string[] = []
  let want = false
  for (const item of list.slice(1)) {
    if (want) {
      out.push(item.text)
      want = false
      continue
    }
    if (item.type === "command_parameter") {
      const flag = item.text.toLowerCase()
      if (SWITCHES.has(flag)) continue
      want = FLAGS.has(flag)
      continue
    }
    out.push(item.text)
  }
  return out
}

function preview(text: string) {
  if (text.length <= MAX_METADATA_LENGTH) return text
  return "...\n\n" + text.slice(-MAX_METADATA_LENGTH)
}

const ERROR_PATTERN = /error|exception|failed|fatal|traceback|panic|exit code/i
const HEAD_BYTES = Math.floor(Truncate.MAX_BYTES * 0.7)
const HEAD_LINES = Math.floor(Truncate.MAX_LINES * 0.7)

function head(text: string, maxLines: number, maxBytes: number): string {
  const lines = text.split("\n")
  const out: string[] = []
  let bytes = 0
  for (let i = 0; i < lines.length && out.length < maxLines; i++) {
    const size = Buffer.byteLength(lines[i], "utf-8") + (i > 0 ? 1 : 0)
    if (bytes + size > maxBytes) break
    out.push(lines[i])
    bytes += size
  }
  return out.join("\n")
}

function tail(text: string, maxLines: number, maxBytes: number) {
  const lines = text.split("\n")
  if (lines.length <= maxLines && Buffer.byteLength(text, "utf-8") <= maxBytes) {
    return {
      text,
      cut: false,
    }
  }

  const out: string[] = []
  let bytes = 0
  for (let i = lines.length - 1; i >= 0 && out.length < maxLines; i--) {
    const size = Buffer.byteLength(lines[i], "utf-8") + (out.length > 0 ? 1 : 0)
    if (bytes + size > maxBytes) {
      if (out.length === 0) {
        const buf = Buffer.from(lines[i], "utf-8")
        let start = buf.length - maxBytes
        if (start < 0) start = 0
        while (start < buf.length && (buf[start] & 0xc0) === 0x80) start++
        out.unshift(buf.subarray(start).toString("utf-8"))
      }
      break
    }
    out.unshift(lines[i])
    bytes += size
  }
  return {
    text: out.join("\n"),
    cut: true,
  }
}

const parse = Effect.fn("BashTool.parse")(function* (command: string, ps: boolean) {
  const tree = yield* Effect.promise(() => parser().then((p) => (ps ? p.ps : p.bash).parse(command)))
  if (!tree) throw new Error("Failed to parse command")
  return tree.rootNode
})

const ask = Effect.fn("BashTool.ask")(function* (ctx: Tool.Context, scan: Scan) {
  if (scan.dirs.size > 0) {
    const globs = Array.from(scan.dirs).map((dir) => {
      if (process.platform === "win32") return AppFileSystem.normalizePathPattern(path.join(dir, "*"))
      return path.join(dir, "*")
    })
    yield* ctx.ask({
      permission: "external_directory",
      patterns: globs,
      always: globs,
      metadata: {},
    })
  }

  if (scan.patterns.size === 0) return
  yield* ctx.ask({
    permission: "bash",
    patterns: Array.from(scan.patterns),
    always: Array.from(scan.always),
    metadata: {},
  })
})

// Secondary confirmation for commands that can overwrite state, cross a
// trust boundary, or produce external side effects. Uses its own permission
// type ("bash_destructive"), which the Permission layer flags as
// forced-ask: no `allow` rule (not even a broad `"*": allow`) can silently
// pre-approve it — only an explicit `deny` blocks. `always` is empty because
// a persisted blanket grant is exactly what forced-ask exists to prevent.
// The confirmation UI shows the full command, so this ask fully replaces
// the regular bash/external_directory prompts when it fires (see the caller
// below) — the action is authorized in one unambiguous confirmation.
const askDestructive = Effect.fn("BashTool.askDestructive")(function* (
  ctx: Tool.Context,
  scan: Scan,
  command: string,
  workdir: string,
) {
  const patterns = Array.from(scan.destructive)
  yield* ctx.ask({
    permission: "bash_destructive",
    patterns,
    always: [],
    metadata: { command, workdir, actions: patterns },
  })
})

function cmd(
  shell: string,
  command: string,
  cwd: string,
  env: NodeJS.ProcessEnv,
  writableRoots: string[],
  network: "enabled" | "disabled" | undefined,
) {
  const invocation = resolveShellInvocation({
    shell,
    command,
    cwd,
    workspace: Instance.directory,
    writableRoots,
    network,
  })
  return {
    containment: invocation.containment,
    network: invocation.network,
    process: ChildProcess.make(invocation.executable, invocation.args, {
      cwd,
      env,
      stdin: "ignore",
      detached: process.platform !== "win32",
    }),
  }
}

function shellEnvironment(env: NodeJS.ProcessEnv, cwd: string) {
  return minimalShellEnvironment(env, cwd)
}

function safeOutput(text: string) {
  return BashTokenEfficient.securityRedact(text)
}

function shellProcess(
  shell: string,
  command: string,
  cwd: string,
  env: NodeJS.ProcessEnv,
  writableRoots: string[],
  network: "enabled" | "disabled" | undefined,
) {
  const invocation = cmd(shell, command, cwd, env, writableRoots, network)
  return {
    containment: invocation.containment,
    network: invocation.network,
    process: invocation.process,
  }
}

const parser = lazy(async () => {
  const { Parser } = await import("web-tree-sitter")
  const { default: treeWasm } = await import("web-tree-sitter/tree-sitter.wasm" as string, {
    with: { type: "wasm" },
  })
  const treePath = resolveWasm(treeWasm)
  await Parser.init({
    locateFile() {
      return treePath
    },
  })
  const { default: bashWasm } = await import("tree-sitter-bash/tree-sitter-bash.wasm" as string, {
    with: { type: "wasm" },
  })
  const { default: psWasm } = await import("tree-sitter-powershell/tree-sitter-powershell.wasm" as string, {
    with: { type: "wasm" },
  })
  const bashPath = resolveWasm(bashWasm)
  const psPath = resolveWasm(psWasm)
  const [bashLanguage, psLanguage] = await Promise.all([Language.load(bashPath), Language.load(psPath)])
  const bash = new Parser()
  bash.setLanguage(bashLanguage)
  const ps = new Parser()
  ps.setLanguage(psLanguage)
  return { bash, ps }
})

// TODO: we may wanna rename this tool so it works better on other shells
export const BashTool = Tool.define(
  "bash",
  Effect.gen(function* () {
    const spawner = yield* ChildProcessSpawner
    const fs = yield* AppFileSystem.Service
    const trunc = yield* Truncate.Service
    const plugin = yield* Plugin.Service

    const cygpath = Effect.fn("BashTool.cygpath")(function* (shell: string, text: string) {
      const lines = yield* spawner
        .lines(ChildProcess.make(shell, ["-lc", 'cygpath -w -- "$1"', "_", text]))
        .pipe(Effect.catch(() => Effect.succeed([] as string[])))
      const file = lines[0]?.trim()
      if (!file) return
      return AppFileSystem.normalizePath(file)
    })

    const resolvePath = Effect.fn("BashTool.resolvePath")(function* (text: string, root: string, shell: string) {
      if (process.platform === "win32") {
        if (Shell.posix(shell) && text.startsWith("/") && AppFileSystem.windowsPath(text) === text) {
          const file = yield* cygpath(shell, text)
          if (file) return AppFileSystem.resolve(file)
        }
        return AppFileSystem.resolve(path.resolve(root, AppFileSystem.windowsPath(text)))
      }
      return AppFileSystem.resolve(path.resolve(root, text))
    })

    const argPath = Effect.fn("BashTool.argPath")(function* (arg: string, cwd: string, ps: boolean, shell: string) {
      const text = ps ? expand(arg, cwd, shell) : home(unquote(arg))
      const file = text && prefix(text)
      if (!file || dynamic(file, ps)) return
      const next = ps ? provider(file) : file
      if (!next) return
      return yield* resolvePath(next, cwd, shell)
    })

    const collect = Effect.fn("BashTool.collect")(function* (root: Node, cwd: string, ps: boolean, shell: string) {
      const scan: Scan = {
        dirs: new Set<string>(),
        patterns: new Set<string>(),
        always: new Set<string>(),
        destructive: new Set<string>(),
      }

      for (const node of commands(root)) {
        const command = parts(node)
        const tokens = command.map((item) => item.text)
        const cmd = ps ? tokens[0]?.toLowerCase() : tokens[0]

        if (cmd && FILES.has(cmd)) {
          for (const arg of pathArgs(command, ps)) {
            const resolved = yield* argPath(arg, cwd, ps, shell)
            log.info("resolved path", { arg, resolved })
            if (!resolved || Instance.containsPath(resolved)) continue
            const dir = (yield* fs.isDir(resolved)) ? resolved : path.dirname(resolved)
            scan.dirs.add(dir)
          }
        }

        if (tokens.length && (!cmd || !CWD.has(cmd))) {
          scan.patterns.add(source(node))
          scan.always.add(BashArity.prefix(tokens).join(" ") + " *")
      }

        const commandSource = source(node)
        if (approvalWorthy(tokens, ps) || activeDynamicShell(commandSource) || activeOutputRedirect(commandSource)) {
          scan.destructive.add(commandSource)
        }
      }

      return scan
    })

    const shellEnv = Effect.fn("BashTool.shellEnv")(function* (ctx: Tool.Context, cwd: string) {
      const extra = yield* plugin.trigger(
        "shell.env",
        { cwd, sessionID: ctx.sessionID, callID: ctx.callID },
        { env: {} },
      )
      return shellEnvironment({
        ...process.env,
        // Python ignores the console code page when stdout is a pipe and falls
        // back to the ANSI code page (GBK on zh-CN), producing mojibake. Force
        // UTF-8 for child Python processes on Windows.
        ...(process.platform === "win32" ? { PYTHONIOENCODING: "utf-8" } : {}),
        ...extra.env,
      }, cwd)
    })

    const run = Effect.fn("BashTool.run")(function* (
      input: {
        shell: string
        name: string
        command: string
        cwd: string
        env: NodeJS.ProcessEnv
        writableRoots: string[]
        network?: "enabled" | "disabled"
        timeout: number
        description: string
      },
      ctx: Tool.Context,
    ) {
      const bytes = Truncate.MAX_BYTES
      const lines = Truncate.MAX_LINES
      const keep = bytes * 2
      let full = ""
      let last = ""
      const list: Chunk[] = []
      let used = 0
      let file = ""
      let sink: ReturnType<typeof createWriteStream> | undefined
      let cut = false
      let expired = false
      let aborted = false
      const redactor = new StreamingSecurityRedactor()
      const invocation = shellProcess(
        input.shell,
        input.command,
        input.cwd,
        input.env,
        input.writableRoots,
        input.network,
      )
      const containment: string = invocation.containment
      const network = invocation.network
      const ownership: Truncate.Ownership = {
        owner: process.env.OPEN_CLANK_OWNER ?? "",
        workspace: input.cwd,
        sessionID: ctx.sessionID,
        ...(ctx.callID ? { callID: ctx.callID } : {}),
      }

      yield* ctx.metadata({
        metadata: {
          output: "",
          description: input.description,
          containment,
          network,
        },
      })

      const retain = (safeChunk: string) => {
        if (!safeChunk) return Effect.void
        const size = Buffer.byteLength(safeChunk, "utf-8")
        list.push({ text: safeChunk, size })
        used += size
        while (used > keep && list.length > 1) {
          const item = list.shift()
          if (!item) break
          used -= item.size
          cut = true
        }

        last = preview(last + safeChunk)

        if (file) {
          sink?.write(safeChunk)
        } else {
          full += safeChunk
          if (Buffer.byteLength(full, "utf-8") > bytes) {
            return trunc.write(full, ownership).pipe(
              Effect.andThen((next) =>
                Effect.sync(() => {
                  file = next
                  cut = true
                  sink = createWriteStream(next, { flags: "a" })
                  full = ""
                }),
              ),
              Effect.andThen(
                ctx.metadata({
                  metadata: {
                    output: last,
                    description: input.description,
                    containment,
                    network,
                  },
                }),
              ),
            )
          }
        }

        return ctx.metadata({
          metadata: {
            output: last,
            description: input.description,
            containment,
            network,
          },
        })
      }

      const code: number | null = yield* Effect.scoped(
        Effect.gen(function* () {
          const handle = yield* spawner.spawn(invocation.process)

          yield* Effect.forkScoped(
            Stream.runForEach(Stream.decodeText(handle.all), (chunk) => retain(redactor.push(chunk))),
          )

          const abort = Effect.callback<void>((resume) => {
            if (ctx.abort.aborted) return resume(Effect.void)
            const handler = () => resume(Effect.void)
            ctx.abort.addEventListener("abort", handler, { once: true })
            return Effect.sync(() => ctx.abort.removeEventListener("abort", handler))
          })

          const timeout = Effect.sleep(`${input.timeout + 100} millis`)

          const exit = yield* Effect.raceAll([
            handle.exitCode.pipe(Effect.map((code) => ({ kind: "exit" as const, code }))),
            abort.pipe(Effect.map(() => ({ kind: "abort" as const, code: null }))),
            timeout.pipe(Effect.map(() => ({ kind: "timeout" as const, code: null }))),
          ])

          if (exit.kind === "abort") {
            aborted = true
            yield* handle.kill({ forceKillAfter: "3 seconds" }).pipe(Effect.orDie)
          }
          if (exit.kind === "timeout") {
            expired = true
            yield* handle.kill({ forceKillAfter: "3 seconds" }).pipe(Effect.orDie)
          }

          return exit.kind === "exit" ? exit.code : null
        }),
      ).pipe(Effect.orDie)
      yield* retain(redactor.finish())

      const meta: string[] = []
      if (expired) {
        meta.push(
          `bash tool terminated command after exceeding timeout ${input.timeout} ms. If this command is expected to take longer and is not waiting for interactive input, retry with a larger timeout value in milliseconds.`,
        )
      }
      if (aborted) meta.push("User aborted the command")
      const raw = list.map((item) => item.text).join("")
      const end = tail(raw, lines, bytes)
      if (end.cut) cut = true
      if (!file && end.cut) {
        file = yield* trunc.write(raw, ownership)
      }

      // Token-efficient post-cleanse: RTK-style ANSI strip / progress fold /
      // secret redact / long-line elide. Spill files already contain mandatory
      // sink-redacted output, so optional cleaning only affects inline output.
      const cleaned =
        !file && Flag.MIMOCODE_EXPERIMENTAL_TOKEN_EFFICIENCY
          ? BashTokenEfficient.clean(end.text, { command: input.command })
          : null
      if (cleaned && cleaned.bytesOut < cleaned.bytesIn) {
        log.info("bash output cleaned", {
          bytesIn: cleaned.bytesIn,
          bytesOut: cleaned.bytesOut,
          saved: cleaned.bytesIn - cleaned.bytesOut,
        })
      }

      // Heuristic (shape-based) pipeline runs AFTER the common pipeline and
      // only when both flags are on. Same never-worse contract — a shape that
      // doesn't shrink the bytes is discarded.
      const heuristic =
        !file &&
        Flag.MIMOCODE_EXPERIMENTAL_TOKEN_EFFICIENCY &&
        Flag.MIMOCODE_EXPERIMENTAL_TOKEN_EFFICIENCY_HEURISTIC
          ? BashTokenEfficientHeuristic.cleanHeuristic(cleaned?.text ?? end.text, { command: input.command })
          : null
      if (heuristic && heuristic.bytesOut < heuristic.bytesIn) {
        log.info("bash output heuristic cleaned", {
          shape: heuristic.shape,
          bytesIn: heuristic.bytesIn,
          bytesOut: heuristic.bytesOut,
          saved: heuristic.bytesIn - heuristic.bytesOut,
        })
      }

      let output = heuristic?.text ?? cleaned?.text ?? end.text
      if (!output) output = "(no output)"

      if (cut && file) {
        // Check if tail contains error patterns — if so, prepend head for context
        const tailScan = end.text.length > 2048 ? end.text.slice(-2048) : end.text
        const hasErrors = ERROR_PATTERN.test(tailScan)
        if (hasErrors) {
          let fileContent: string | undefined
          try {
            fileContent = readFileSync(file, "utf-8")
          } catch {
            fileContent = undefined
          }
          if (fileContent) {
            const headText = head(fileContent, HEAD_LINES, HEAD_BYTES)
            output = `...output truncated (head+tail shown due to errors)...\n\nFull output saved to: ${file}\n\n${headText}\n\n...middle omitted...\n\n${end.text}`
          } else {
            output = `...output truncated...\n\nFull output saved to: ${file}\n\n` + output
          }
        } else {
          output = `...output truncated...\n\nFull output saved to: ${file}\n\n` + output
        }
      }

      if (meta.length > 0) {
        output += "\n\n<bash_metadata>\n" + meta.join("\n") + "\n</bash_metadata>"
      }
      if (sink) {
        const stream = sink
        yield* Effect.promise(
          () =>
            new Promise<void>((resolve) => {
              stream.end(() => resolve())
              stream.on("error", () => resolve())
            }),
        )
      }

      return {
        title: input.description,
        metadata: {
          output: last || preview(output),
          exit: code,
          description: input.description,
          truncated: cut,
          containment,
          network,
          ...(cut && file ? { outputPath: file } : {}),
        },
        output,
      }
    })

    return () =>
      Effect.sync(() => {
        const shell = Shell.acceptable()
        const name = Shell.name(shell)
        log.info("bash tool using shell", { shell })

        return {
          description: bashDescription(),
          parameters: Parameters,
          execute: (params: z.infer<typeof Parameters>, ctx: Tool.Context) =>
            Effect.gen(function* () {
              const effectiveCwd = SessionCwd.get(ctx.sessionID)
              const cwd = params.workdir
                ? yield* resolvePath(params.workdir, effectiveCwd, shell)
                : AppFileSystem.resolve(effectiveCwd)
              if (params.timeout !== undefined && params.timeout < 0) {
                throw new Error(`Invalid timeout value: ${params.timeout}. Timeout must be a positive number.`)
              }
              const timeout = params.timeout ?? DEFAULT_TIMEOUT
              yield* Effect.promise(() => assertProjectShellPolicy(ctx))
              const ps = PS.has(name)
              const root = yield* parse(params.command, ps)
              const scan = yield* collect(root, cwd, ps, shell)
              if (!Instance.containsPath(cwd)) scan.dirs.add(cwd)
              // Sensitive/destructive commands are authorized by one forced
              // ask. Its UI shows the full command (including any external
              // paths it touches), so a separate bash/external_directory
              // prompt would just be a second confirmation of the same thing.
              // The explicit auto-approve escape hatch falls back to the
              // regular ask, where a `bash: deny` rule still blocks.
              if (scan.destructive.size > 0 && !Flag.MIMOCODE_AUTO_APPROVE_DESTRUCTIVE) {
                yield* askDestructive(ctx, scan, params.command, cwd)
              } else {
                yield* ask(ctx, scan)
              }

              // Interactive mode: hand terminal to user for direct interaction
              if (params.interactive) {
                const callID = ctx.callID
                if (!callID) throw new Error("Interactive shell requires a bound tool call")
                const env = yield* shellEnv(ctx, cwd)
                yield* ctx.metadata({
                  metadata: {
                    output: "(waiting for user interaction...)",
                    description: params.description,
                  },
                })
                const interactiveResult = yield* Effect.tryPromise(() =>
                  BashInteractive.request({
                    sessionID: ctx.sessionID,
                    callID,
                    command: params.command,
                    cwd,
                    workspace: Instance.directory,
                    writableRoots: [Instance.directory, ...scan.dirs],
                    shell,
                    network: params.network,
                    timeout: Math.max(timeout, 1000),
                    env: env as Record<string, string>,
                    description: params.description,
                  }),
                ).pipe(
                  Effect.catch((error) =>
                    Effect.succeed({
                      output: `(interactive command ended without a reply: ${String(error)})`,
                      exitCode: 124,
                    }),
                  ),
                )
                const output = safeOutput(interactiveResult.output)
                return {
                  title: params.description,
                  metadata: {
                    output: output || "(interactive command completed)",
                    exit: interactiveResult.exitCode,
                    description: params.description,
                    truncated: false,
                    containment: "interactive-client",
                    network: params.network ?? "enabled",
                  },
                  output:
                    output ||
                    `(interactive command completed with exit code ${interactiveResult.exitCode})`,
                }
              }

              return yield* run(
                {
                  shell,
                  name,
                  command: params.command,
                  cwd,
                  env: yield* shellEnv(ctx, cwd),
                  writableRoots: [Instance.directory, ...scan.dirs],
                  network: params.network,
                  timeout,
                  description: params.description,
                },
                ctx,
              )
            }),
        }
      })
  }),
)
