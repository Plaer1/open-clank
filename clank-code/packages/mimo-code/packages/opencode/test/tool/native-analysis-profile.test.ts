import { describe, expect, test } from "bun:test"
import { ANALYSIS_PROFILES, activeAnalysisProfile, profileResultIdentity, type AnalysisProfile } from "../../src/tool/native-analysis-profile"

const sha = "a".repeat(64)
const rizinEnv = { OPEN_CLANK_RIZIN_EXECUTABLE: "/x/rizin", OPEN_CLANK_RIZIN_SHA256: sha, OPEN_CLANK_RIZIN_VERSION: "0.8.1" }

describe("native-analysis profile", () => {
  test("table has one entry equal to the Host table", () => {
    // Expected values copied from PROFILES in src/openclank/analysis_profiles.py.
    expect(ANALYSIS_PROFILES.length).toBe(1)
    const p = ANALYSIS_PROFILES[0]!
    expect([p.provider, p.version, p.producerEvidence, p.versionEvidence, p.resultVersionEvidence]).toEqual([
      "rizin", "0.8.1", "native_producer_attested", "operator_configured_unverified", "operator_configured_unverified",
    ])
    expect(Object.isFrozen(ANALYSIS_PROFILES)).toBe(true)
    expect(Object.isFrozen(p)).toBe(true)
  })

  test("active only for the environment that enabled the tool before", () => {
    expect(activeAnalysisProfile(rizinEnv)).toBe(ANALYSIS_PROFILES[0])
    expect(activeAnalysisProfile({ ...rizinEnv, OPEN_CLANK_RIZIN_VERSION: "0.8.2" })).toBeUndefined()
    expect(activeAnalysisProfile({ ...rizinEnv, OPEN_CLANK_RIZIN_VERSION: "0.8.1\n" })).toBeUndefined()
    expect(activeAnalysisProfile({ ...rizinEnv, OPEN_CLANK_RIZIN_VERSION: undefined })).toBeUndefined()
    expect(activeAnalysisProfile({ ...rizinEnv, OPEN_CLANK_RIZIN_EXECUTABLE: "" })).toBeUndefined()
    expect(activeAnalysisProfile({ ...rizinEnv, OPEN_CLANK_RIZIN_SHA256: "xyz" })).toBeUndefined()
    expect(activeAnalysisProfile({})).toBeUndefined()
  })

  test("result identity for the Rizin profile is unchanged", () => {
    const out = profileResultIdentity(ANALYSIS_PROFILES[0]!)
    expect(out).toEqual({ provider: "rizin", providerVersion: "0.8.1", providerVersionEvidence: "operator_configured_unverified" })
    expect(Object.keys(out)).toEqual(["provider", "providerVersion", "providerVersionEvidence"])
  })

  test("a synthetic second profile is selected without any Rizin literal", () => {
    const synthetic: AnalysisProfile = {
      ...ANALYSIS_PROFILES[0]!, provider: "synthetic", version: "2", versionPattern: /^2$/, permission: "native-analysis:synthetic",
      env: { ...ANALYSIS_PROFILES[0]!.env, executable: "SYN_EXE", sha256: "SYN_SHA", version: "SYN_VER" },
    }
    const env = { SYN_EXE: "/x/syn", SYN_SHA: sha, SYN_VER: "2" }
    expect(activeAnalysisProfile(env, [synthetic])).toBe(synthetic)
    expect(activeAnalysisProfile(rizinEnv, [synthetic])).toBeUndefined()
    expect(activeAnalysisProfile({ ...rizinEnv, ...env }, [...ANALYSIS_PROFILES, synthetic])).toBe(ANALYSIS_PROFILES[0])
    expect(profileResultIdentity(synthetic).provider).toBe("synthetic")
  })
})
