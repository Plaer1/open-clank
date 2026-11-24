import { BusEvent } from "@/bus/bus-event"
import { Instance } from "@/project/instance"
import { managedSessionBinding } from "@/memory/session-scope"
import { SessionID } from "@/session/schema"
import z from "zod"

// Kept for SDK compatibility. Session cwd overrides are no longer mutable.
export const Event = {
  Changed: BusEvent.define(
    "session.cwd",
    z.object({
      sessionID: SessionID.zod,
      cwd: z.string(),
    }),
  ),
}

// Open Clank: a managed session's working directory is host-authoritative.
export function get(sessionID: SessionID): string {
  const managed = managedSessionBinding(sessionID)
  if (managed) {
    if (managed.transition) throw new Error("managed session cwd is reconciling")
    return managed.physicalCwd
  }
  return Instance.directory
}

export * as SessionCwd from "./session-cwd"
