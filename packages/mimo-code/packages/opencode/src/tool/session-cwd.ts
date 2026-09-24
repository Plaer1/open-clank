import { BusEvent } from "@/bus/bus-event"
import { Instance } from "@/project/instance"
import { registerDisposer } from "@/effect/instance-registry"
import { managedSessionBinding } from "@/memory/session-scope"
import { SessionID } from "@/session/schema"
import z from "zod"

interface Entry {
  directory: string
  cwd: string
}

const store = new Map<string, Entry>()

registerDisposer(async (directory) => {
  for (const [sessionID, entry] of store) {
    if (entry.directory === directory) store.delete(sessionID)
  }
})

export const Event = {
  Changed: BusEvent.define(
    "session.cwd",
    z.object({
      sessionID: SessionID.zod,
      cwd: z.string(),
    }),
  ),
}

export function get(sessionID: SessionID): string {
  const managed = managedSessionBinding(sessionID)
  if (managed) {
    if (managed.transition) throw new Error("managed session cwd is reconciling")
    return managed.physicalCwd
  }
  return store.get(sessionID)?.cwd ?? Instance.directory
}

export function set(sessionID: SessionID, dir: string): void {
  const managed = managedSessionBinding(sessionID)
  if (managed) {
    if (managed.transition) throw new Error("managed session cwd is reconciling")
    if (managed.physicalCwd !== dir) throw new Error("managed session cwd is host-authoritative")
    return
  }
  store.set(sessionID, { directory: Instance.directory, cwd: dir })
}

export function clear(sessionID: SessionID): void {
  store.delete(sessionID)
}

export * as SessionCwd from "./session-cwd"
