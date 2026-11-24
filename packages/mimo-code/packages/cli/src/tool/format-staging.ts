import path from "path"
import { Effect } from "effect"
import type { AppFileSystem } from "@mimo-ai/shared/filesystem"
import type { Format } from "@/format"

/** Format the candidate away from the live target; commit its final bytes once. */
export function stageFormatted(
  fs: AppFileSystem.Interface,
  format: Format.Interface,
  target: string,
  content: string | Uint8Array,
  mode?: number,
) {
  return Effect.gen(function* () {
    // Keep configuration discovery under the nearest existing target ancestor.
    let parent = path.dirname(target)
    while (!(yield* fs.existsSafe(parent))) {
      const next = path.dirname(parent)
      if (next === parent) throw new Error(`No existing parent for ${target}`)
      parent = next
    }
    return yield* Effect.acquireUseRelease(
    fs.makeTempDirectory({ directory: parent, prefix: ".open-clank-format-" }),
    (dir) => Effect.gen(function* () {
      const staged = path.join(dir, path.basename(target))
      yield* fs.writeWithDirs(staged, content, mode)
      const result = yield* Effect.exit(format.file(staged))
      if (result._tag === "Failure") {
        yield* fs.writeWithDirs(staged, content, mode)
        return { content, text: (yield* fs.readTextSnapshot(staged)).text, status: "failed" as const }
      }
      return { content: yield* fs.readFile(staged), text: (yield* fs.readTextSnapshot(staged)).text, status: "completed" as const }
    }),
    (dir) => fs.remove(dir, { recursive: true, force: true }).pipe(Effect.ignore),
    )
  })
}
