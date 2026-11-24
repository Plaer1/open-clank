import { Effect } from "effect"
import type { Lane } from "./schema"

/**
 * Lane kick hook registered by SessionPrompt after services exist.
 * Avoids a circular import: Controller.accept only knows this ref.
 * kick is fire-and-forget — scheduling owner is still inbox receipts;
 * the kick only starts a Runner lease when the lane is idle.
 */
export type LaneScheduler = {
  readonly guard?: (lane: Lane) => Effect.Effect<Effect.Effect<void>>
  readonly kick: (lane: Lane, guard?: Effect.Effect<void>) => Effect.Effect<void>
}

export const schedulerRef: { current: LaneScheduler | undefined } = { current: undefined }
