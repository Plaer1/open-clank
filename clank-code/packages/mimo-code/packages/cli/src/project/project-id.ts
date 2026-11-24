import * as fs from "fs"
import * as path from "path"
import * as crypto from "crypto"
import { spawnSync } from "node:child_process"
import { ProjectID } from "./schema"
import { childProcessEnv } from "../util/child-process-env"

function resolveGitCommonDir(startDir: string): string | null {
  const result = spawnSync("git", ["rev-parse", "--git-common-dir"], {
    cwd: startDir,
    encoding: "utf-8",
    env: childProcessEnv({ GIT_CEILING_DIRECTORIES: process.env.GIT_CEILING_DIRECTORIES }),
  })
  if (result.error) throw result.error
  if (result.status !== 0) return null
  return path.resolve(startDir, result.stdout.trim())
}

export function resolveMainGitDir(startDir: string): string | null {
  // Git 负责显式发现边界的路径列表、符号链接和平台语义。
  if (process.env.GIT_CEILING_DIRECTORIES) return resolveGitCommonDir(startDir)
  let dir = path.resolve(startDir)
  while (true) {
    const candidate = path.join(dir, ".git")
    if (fs.existsSync(candidate)) {
      const stat = fs.statSync(candidate)
      if (stat.isDirectory()) return candidate
      return resolveGitCommonDir(startDir)
    }
    const parent = path.dirname(dir)
    if (parent === dir) return null
    dir = parent
  }
}

function readFileTrimmedOrNull(p: string): string | null {
  if (!fs.existsSync(p)) return null
  const text = fs.readFileSync(p, "utf-8").trim()
  return text || null
}

export function resolveProjectId(workingDir: string): ProjectID {
  const mainGit = resolveMainGitDir(workingDir)

  if (mainGit) {
    const idFile = path.join(mainGit, "openclank-project-id")
    const cached = readFileTrimmedOrNull(idFile)
    if (cached) return ProjectID.make(cached)
    const newId = crypto.randomUUID()
    fs.writeFileSync(idFile, newId)
    return ProjectID.make(newId)
  }

  const localFile = path.join(workingDir, ".openclank-project-id")
  const cached = readFileTrimmedOrNull(localFile)
  if (cached) return ProjectID.make(cached)
  const newId = crypto.randomUUID()
  fs.writeFileSync(localFile, newId)
  return ProjectID.make(newId)
}
