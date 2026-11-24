import type { TuiPlugin, TuiPluginApi, TuiPluginModule } from "@mimo-ai/plugin/tui"
import { createMemo, For, Show } from "solid-js"
import * as GoalState from "@/session/goal-state"

const id = "internal:sidebar-goal"

function View(props: { api: TuiPluginApi; session_id: string }) {
  const theme = () => props.api.theme.current
  const goal = createMemo(() => props.api.state.session.goal(props.session_id))
  // The latest verdict (keyed by the most recently judged turn) drives the
  // status line; per-turn reasons live inline on the message stream.
  const latest = createMemo(() => {
    const g = goal()
    if (!g?.lastMessageID) return undefined
    return g.verdicts[g.lastMessageID]
  })

  // Show whenever there is an active goal, or a verdict survives from a goal
  // that just cleared (so the ✓/⊘ result lingers briefly).
  const lines = createMemo(() => {
    const current = goal()
    if (!current?.state) return []
    return GoalState.summaryLines(GoalState.Envelope.parse(current.state), current.analytics ?? {})
  })
  const show = createMemo(() => Boolean(lines().length || latest()))

  const status = createMemo(() => {
    const v = latest()
    if (!v) return undefined
    if (v.error) return { dot: theme().textMuted, label: "error (stopped)" }
    if (v.ok) return { dot: theme().success, label: "met" }
    if (v.impossible) return { dot: theme().error, label: "impossible" }
    return { dot: theme().warning, label: `round ${v.attempt} · not met` }
  })

  return (
    <Show when={show()}>
      <box>
        <box flexDirection="row" gap={1}>
          <text fg={theme().text}>
            <b>Goal</b>
          </text>
        </box>
        <For each={lines()}>
          {(line, index) => (
            <box flexDirection="row" gap={1}>
              <text flexShrink={0} fg={index() === 1 ? theme().primary : theme().textMuted}>
                {index() === 1 ? "•" : " "}
              </text>
              <text fg={theme().textMuted} wrapMode="word">
                {line}
              </text>
            </box>
          )}
        </For>
        <Show when={status()}>
          {(s) => (
            <box flexDirection="row" gap={1}>
              <text flexShrink={0} fg={s().dot}>
                •
              </text>
              <text fg={theme().textMuted} wrapMode="word">
                Judge: {s().label}
              </text>
            </box>
          )}
        </Show>
      </box>
    </Show>
  )
}

const tui: TuiPlugin = async (api) => {
  api.slots.register({
    // Just below LSP (300) so the goal status sits beneath the LSP block.
    order: 350,
    slots: {
      sidebar_content(_ctx, props) {
        return <View api={api} session_id={props.session_id} />
      },
    },
  })
}

const plugin: TuiPluginModule & { id: string } = {
  id,
  tui,
}

export default plugin
