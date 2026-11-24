/** Closed, provenance-only evidence for a single unresumed root launch. */
export type LaunchWitness = { version: 1; account_id: string; workspace_id: string; session_id: string; run_id: string;
  root_operation_id: string; actor_id: "main"; execution_revision: number; inner_turn_id: string }
const keys = ["account_id", "actor_id", "execution_revision", "inner_turn_id", "root_operation_id", "run_id", "session_id", "version", "workspace_id"] as const
const item = (value: unknown): value is string => typeof value === "string" && value.length > 0 && value === value.trim() &&
  !value.includes("\0") && Buffer.byteLength(value, "utf8") <= 256 && Buffer.from(value, "utf8").toString("utf8") === value
export function parseLaunchWitness(raw: unknown, sessionID: string, runID: string): LaunchWitness | undefined {
  if (typeof raw !== "string" || Buffer.byteLength(raw, "utf8") > 4096) return
  try {
    const value = JSON.parse(raw)
    if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).sort().join("\0") !== keys.join("\0") ||
        value.version !== 1 || value.actor_id !== "main" || !Number.isSafeInteger(value.execution_revision) || value.execution_revision < 0 ||
        ![value.account_id,value.workspace_id,value.session_id,value.run_id,value.root_operation_id,value.inner_turn_id].every(item) ||
        value.session_id !== sessionID || value.run_id !== runID) return
    const canonical = JSON.stringify(Object.fromEntries(keys.map((key) => [key, value[key]])))
    return canonical === raw ? value as LaunchWitness : undefined
  } catch { return }
}
export function encodeLaunchWitness(value: LaunchWitness): string {
  const raw = JSON.stringify(Object.fromEntries(keys.map((key) => [key, value[key]])))
  if (!parseLaunchWitness(raw, value.session_id, value.run_id)) throw new Error("workflow launch witness unavailable")
  return raw
}
