import type { Hooks, PluginInput } from "@mimo-ai/plugin"
import { isMemoryWriteEnabled, type MemoryWriteConfig } from "../memory/write-gate"
import { workflowTaskAssignmentCurrent, workflowTaskCommitted } from "../session/workflow-source"

const TEMPLATE = `## §1 Task identity
- task_id: <TID>
- short summary: <one line>

## §2 Subagent intent
What this subagent was asked to do.

## §3 Files and code sections
Files and the work performed.

## §4 Verbatim commands
Exact commands used or needed later.

## §5 Outcome and discoveries
Outcome and discoveries relevant to other tasks.`

export async function SubagentProgressCheckerPlugin(pluginInput: PluginInput): Promise<Hooks> {
  return {
    "actor.postStop": {
      matcher: { agentType: { excludeOnly: ["checkpoint-writer", "title", "summary", "dream", "distill", "compaction", "main"] } },
      run: async (input, output) => {
        const taskID = input.task_id
        if (!taskID || input.canWorkflowWrite !== true) return
        const config = await pluginInput.client.config.get().catch(() => undefined)
        if (!isMemoryWriteEnabled(config?.data as MemoryWriteConfig | undefined)) return
        if (!workflowTaskAssignmentCurrent(input.sessionID, input.actorID, taskID)) return
        if (workflowTaskCommitted(input.sessionID, input.actorID, taskID)) return
        output.continue = true
        output.reason = [
          "Before terminating, use workflow_artifact to read and write the progress for your assigned task.",
          "Use role progress and name null. Read the current revision first, then write the complete journal using that revision.",
          "The write must complete successfully and include these exact headings:",
          TEMPLATE.replace("<TID>", taskID),
          "If the workflow service is unavailable, report that outcome rather than writing a local file.",
        ].join("\n\n")
      },
    },
  }
}
