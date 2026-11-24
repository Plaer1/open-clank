// Closed table of native-analysis launch profiles. Mirrors src/openclank/analysis_profiles.py (Host):
// provider/version/evidence strings below are copied from PROFILES there; the Host table is the authority.
// env names, version pattern and permission pattern are adapter facts the Host supervisor already passes.
export type AnalysisProfile = {
  readonly provider: string
  readonly version: string
  readonly producerEvidence: string
  readonly versionEvidence: string
  readonly resultVersionEvidence: string
  readonly versionPattern: RegExp
  readonly permission: string
  readonly rootSelectorDomain: string
  readonly env: Readonly<{
    executable: string; sha256: string; version: string
    ghidraLibrary: string; ghidraSha256: string; sleighHome: string; sleighManifest: string; sleighManifestSha256: string
  }>
}

export const ANALYSIS_PROFILES: readonly AnalysisProfile[] = Object.freeze([
  Object.freeze({
    provider: 'rizin',
    version: '0.8.1',
    producerEvidence: 'native_producer_attested',
    versionEvidence: 'operator_configured_unverified',
    resultVersionEvidence: 'operator_configured_unverified',
    versionPattern: /^0\.8\.1$/,
    permission: 'native-analysis:rizin',
    rootSelectorDomain: 'openclank.rizin.sleigh-root.v1',
    env: Object.freeze({
      executable: 'OPEN_CLANK_RIZIN_EXECUTABLE', sha256: 'OPEN_CLANK_RIZIN_SHA256', version: 'OPEN_CLANK_RIZIN_VERSION',
      ghidraLibrary: 'OPEN_CLANK_RIZIN_GHIDRA_LIBRARY', ghidraSha256: 'OPEN_CLANK_RIZIN_GHIDRA_SHA256',
      sleighHome: 'OPEN_CLANK_RIZIN_SLEIGH_HOME', sleighManifest: 'OPEN_CLANK_RIZIN_SLEIGH_MANIFEST',
      sleighManifestSha256: 'OPEN_CLANK_RIZIN_SLEIGH_MANIFEST_SHA256',
    }),
  }),
])

/** The profile whose version, executable and sha env are all set; otherwise undefined. */
export function activeAnalysisProfile(env: NodeJS.ProcessEnv = process.env, table: readonly AnalysisProfile[] = ANALYSIS_PROFILES) {
  return table.find((p) => p.versionPattern.test(env[p.env.version] || '') && !!env[p.env.executable] && /^[a-f0-9]{64}$/.test(env[p.env.sha256] || ''))
}

/** Identity fields a result carries; key order is part of the emitted bytes. */
export function profileResultIdentity(profile: AnalysisProfile) {
  return { provider: profile.provider, providerVersion: profile.version, providerVersionEvidence: profile.resultVersionEvidence }
}
