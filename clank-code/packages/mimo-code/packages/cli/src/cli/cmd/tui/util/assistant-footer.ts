/**
 * Completion-footer visibility for an assistant message row.
 *
 * The footer (`▣ Mode · model · duration`) is the last row of the scrollable
 * transcript and must stay mounted — including mid-loop `finish === "tool-calls"`
 * steps. The old gate hid the row whenever the last message finished with
 * tool-calls, so the last line vanished for the whole tool window and flickered
 * at the process→complete transition. Orchestrator (the only reason that gate
 * existed) is gone, so a trailing tool-calls row no longer needs suppressing.
 */

/** A settled turn step: finish is known and is not a mid-loop tool-calls hop. */
export function isFinalAssistantFinish(finish: string | undefined): boolean {
  return Boolean(finish) && finish !== "tool-calls"
}

export function shouldShowAssistantFooter(input: {
  recovering: boolean
  aborted: boolean
  finish: string | undefined
  isLast: boolean
}): boolean {
  if (input.recovering) return true
  if (input.aborted) return true
  if (isFinalAssistantFinish(input.finish)) return true
  return input.isLast
}
