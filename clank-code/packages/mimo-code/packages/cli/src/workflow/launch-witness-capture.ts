import { ManagedProvider } from "@/acp/managed-provider"
import { managedSessionBinding, markerMatches } from "@/memory/session-scope"
import { isAdmittedToolTimingTurn } from "@/session/tool-timing"
import { encodeLaunchWitness, type LaunchWitness } from "./launch-witness"

/** Freeze the actual admitted root turn and recheck it at the owned insert. */
export function captureLaunchWitness(actor: import("@/actor/turn").ActorExecutionIdentity | undefined, sessionID: string, toolCallID: string | undefined) {
  if (!toolCallID || Buffer.byteLength(toolCallID,"utf8") > 256 || !actor || actor.sessionID !== sessionID || actor.actorID !== "main" || !actor.rootOperationID || !actor.innerTurnID) return
  const bootstrap = actor.preflightRoute?.bootstrap
  const binding = managedSessionBinding(sessionID)
  const host = ManagedProvider.currentActorHostContext(sessionID, actor.actorID, actor.executionRevision)
  const scope = ManagedProvider.currentScope(sessionID, actor.actorID, actor.executionRevision)
  const issuer = ManagedProvider.currentActorToolTimingIssuer(sessionID, actor.actorID, actor.executionRevision, actor.rootOperationID)
  const identity = {sessionID,actorID:actor.actorID,executionRevision:actor.executionRevision,rootOperationID:actor.rootOperationID,innerTurnID:actor.innerTurnID}
  if (!bootstrap || bootstrap.rootSessionID !== sessionID || bootstrap.sessionID !== sessionID || bootstrap.actorID !== "main" ||
      bootstrap.rootOperationID !== identity.rootOperationID || !binding || binding.authority || !host?.ownerAccountID || !scope || !issuer) return
  const current = () => {
    const now=managedSessionBinding(sessionID), currentHost=ManagedProvider.currentActorHostContext(sessionID,identity.actorID,identity.executionRevision)
    return !!now && markerMatches(sessionID,now) && now.registrationMarker.token===binding.registrationMarker.token && !now.transition &&
      !now.authority && now.owner===bootstrap.owner && now.stableChatID===bootstrap.stableChatID &&
      now.bindingRevision===bootstrap.workspaceRevision && now.mappingRevision===bootstrap.mappingRevision && now.mapRevision>=bootstrap.mapRevision &&
      now.authorityWorkspaceID===binding.authorityWorkspaceID && now.physicalCwd===binding.physicalCwd &&
      currentHost?.ownerAccountID===host.ownerAccountID && currentHost?.chatID===host.chatID &&
      ManagedProvider.currentScope(sessionID,identity.actorID,identity.executionRevision)===scope &&
      ManagedProvider.currentActorToolTimingIssuer(sessionID,identity.actorID,identity.executionRevision,identity.rootOperationID)===issuer && issuer.isAlive() &&
      isAdmittedToolTimingTurn(actor,identity) && actor.preflightRoute?.bootstrap===bootstrap
  }
  if (!current() || !host.ownerAccountID || !binding.authorityWorkspaceID || host.chatID!==binding.stableChatID) return
  return async (runID:string, canonicalRoot:string): Promise<(() => LaunchWitness | undefined) | undefined> => {
    if (!current() || canonicalRoot!==binding.physicalCwd) return
    try { await ManagedProvider.admitManagedTool(identity,"workflow","tool",toolCallID) } catch { return }
    if (!current() || canonicalRoot!==binding.physicalCwd) return
    const result:LaunchWitness={version:1,account_id:host.ownerAccountID!,workspace_id:binding.authorityWorkspaceID,session_id:sessionID,run_id:runID,
      root_operation_id:identity.rootOperationID,actor_id:"main",execution_revision:identity.executionRevision,inner_turn_id:identity.innerTurnID}
    try { encodeLaunchWitness(result); return () => current() && canonicalRoot===binding.physicalCwd ? result : undefined } catch { return }
  }
}
