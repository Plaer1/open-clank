import { $ } from "bun"
import path from "path"

const rootPkgPath = path.resolve(import.meta.dir, "../package.json")
const rootPkg = await Bun.file(rootPkgPath).json()
const expectedBunVersion = rootPkg.packageManager?.split("@")[1]

if (!expectedBunVersion) {
  throw new Error("packageManager field not found in root package.json")
}

if (process.versions.bun !== expectedBunVersion) {
  throw new Error(`This script requires exactly bun@${expectedBunVersion}, but you are using bun@${process.versions.bun}`)
}

const env = {
  OPENCLANK_CHANNEL: process.env["OPENCLANK_CHANNEL"],
  OPENCLANK_BUMP: process.env["OPENCLANK_BUMP"],
  OPENCLANK_VERSION: process.env["OPENCLANK_VERSION"],
}
const CHANNEL = await (async () => {
  if (env.OPENCLANK_CHANNEL) return env.OPENCLANK_CHANNEL
  if (env.OPENCLANK_BUMP) return "latest"
  if (env.OPENCLANK_VERSION && !env.OPENCLANK_VERSION.startsWith("0.0.0-")) return "latest"
  return await $`git branch --show-current`.text().then((x) => x.trim()) || "latest"
})()
const IS_PREVIEW = CHANNEL !== "latest"

const SHORT_SHA = await (async () => {
  if (process.env["OPENCLANK_COMMIT_SHA"]) return process.env["OPENCLANK_COMMIT_SHA"]!
  try {
    const sha = await $`git rev-parse --short HEAD`.text()
    return sha.trim()
  } catch {
    return null
  }
})()

const VERSION = await (async () => {
  if (env.OPENCLANK_VERSION) return env.OPENCLANK_VERSION
  if (IS_PREVIEW) {
    if (SHORT_SHA) return `0.0.0-${CHANNEL}-${SHORT_SHA}`
    const ts = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 12)
    return `0.0.0-${CHANNEL}-${ts}`
  }
  const version = await Bun.file(path.resolve(import.meta.dir, "../packages/cli/package.json"))
    .json()
    .then((data: any) => data.version)
  const t = env.OPENCLANK_BUMP?.toLowerCase()
  if (!t) return version
  const [major, minor, patch] = version.split(".").map((x: string) => Number(x) || 0)
  if (t === "major") return `${major + 1}.0.0`
  if (t === "minor") return `${major}.${minor + 1}.0`
  return `${major}.${minor}.${patch + 1}`
})()

export const Script = {
  get channel() {
    return CHANNEL
  },
  get version() {
    return VERSION
  },
  get preview() {
    return IS_PREVIEW
  },
}
