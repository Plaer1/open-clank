declare global {
  const OPENCLANK_VERSION: string
  const OPENCLANK_CHANNEL: string
}

export const InstallationVersion = typeof OPENCLANK_VERSION === "string" ? OPENCLANK_VERSION : "local"
export const InstallationChannel = typeof OPENCLANK_CHANNEL === "string" ? OPENCLANK_CHANNEL : "local"
export const InstallationLocal = InstallationChannel === "local"
