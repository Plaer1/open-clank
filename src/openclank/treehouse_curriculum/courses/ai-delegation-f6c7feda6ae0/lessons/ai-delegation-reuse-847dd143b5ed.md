# Reuse the right kind of state

- Definition reuse starts a fresh worker with the same instructions. Session reuse retains history. Artifact reuse reconstructs relevant findings from durable records.
- Continue a coherent investigation when its history remains useful. Start from a concise artifact when stale assumptions or unrelated history would dominate.
- Continuation and interruption behavior depends on the harness. Inspect actual status and preserve unfinished work; session history is not automatically free processing.

Synthetic record: yesterday's parser worker knows why parser A failed. Today's task continues that investigation; an unrelated image task needs different evidence. An interrupted worker has a saved handoff but no confirmed completed result. Recover from the supported continuation path or explicit handoff rather than declaring its assignment finished.
