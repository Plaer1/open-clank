export * as ConfigManaged from "./managed"

import { existsSync } from "fs"
import os from "os"
import path from "path"
import { Log, Process } from "../util"
import { warn } from "console"

const log = Log.create({ service: "config" })

export const MANAGED_PLIST_DOMAIN = "ai.openclank.engine.managed"

// Keys injected by macOS/MDM into the managed plist that are not engine config
const PLIST_META = new Set([
  "PayloadDisplayName",
  "PayloadIdentifier",
  "PayloadType",
  "PayloadUUID",
  "PayloadVersion",
  "_manualProfile",
])

export function systemManagedConfigDir(platform: NodeJS.Platform = process.platform, programData: string | undefined = process.env.ProgramData): string {
  switch (platform) {
    case "darwin":
      return "/Library/Application Support/openclank-engine"
    case "win32":
      return path.join(programData || "C:\\ProgramData", "OpenClankEngine")
    default:
      return "/etc/openclank-engine"
  }
}

export function managedConfigDir() {
  return process.env.OPENCLANK_TEST_MANAGED_CONFIG_DIR || systemManagedConfigDir()
}

export function parseManagedPlist(json: string): string {
  const raw = JSON.parse(json)
  for (const key of Object.keys(raw)) {
    if (PLIST_META.has(key)) delete raw[key]
  }
  return JSON.stringify(raw)
}

export async function readManagedPreferences() {
  if (process.platform !== "darwin") return

  const user = os.userInfo().username
  const paths = [
    path.join("/Library/Managed Preferences", user, `${MANAGED_PLIST_DOMAIN}.plist`),
    path.join("/Library/Managed Preferences", `${MANAGED_PLIST_DOMAIN}.plist`),
  ]

  for (const plist of paths) {
    if (!existsSync(plist)) continue
    log.info("reading macOS managed preferences", { path: plist })
    const result = await Process.run(["plutil", "-convert", "json", "-o", "-", plist], { nothrow: true })
    if (result.code !== 0) {
      log.warn("failed to convert managed preferences plist", { path: plist })
      continue
    }
    return {
      source: `mobileconfig:${plist}`,
      text: parseManagedPlist(result.stdout.toString()),
    }
  }

  return
}
