import { afterEach, describe, expect } from "bun:test"
import { Effect, Layer } from "effect"
import path from "path"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { MessageV2 } from "../../src/session/message-v2"
import { SessionPrompt } from "../../src/session/prompt"
import { Command } from "../../src/command"
import { Skill } from "../../src/skill"
import { Log } from "../../src/util"
import { provideTmpdirServer } from "../fixture/fixture"
import { testEffect } from "../lib/effect"
import { withEnv } from "../lib/env"
import { makeLayer, providerCfg, ref } from "../workflow/lib"

withEnv({ MIMOCODE_DISABLE_BUILTIN_SKILLS: "true", MIMOCODE_DISABLE_COMPOSE_SKILLS: "true" })

void Log.init({ print: false })

afterEach(async () => {
  await Instance.disposeAll()
})

const it = testEffect(Layer.mergeAll(makeLayer(), Skill.defaultLayer))

function writeSkill(dir: string, name: string, frontmatter: string, marker: string) {
  return Effect.promise(() =>
    Bun.write(
      path.join(dir, ".mimocode", "skill", name, "SKILL.md"),
      `---\nname: ${name}\ndescription: ${name} trust-gate test skill.\n${frontmatter}---\n\n# ${name}\n\n${marker}\n`,
    ),
  )
}

const injected = (parts: MessageV2.WithParts["parts"]) =>
  parts.flatMap((p) => (p.type === "text" ? (p.text.match(/^<skill_content name="([^"]+)">/)?.[1] ?? []) : []))

// F1 regression: the slash/mention injection path must use the same trust
// filter as the skill tool (evaluateInvocation/getForInvocation("explicit")).
// Disabled, untrusted, revoked, and staged skills must never be injected or
// become Command entries — even when the user themselves types /name.
describe("slash/mention trust gate", () => {
  it.live(
    "slash/mention cannot inject disabled/untrusted/revoked/staged skills",
    () =>
      provideTmpdirServer(
        Effect.fnUntraced(function* ({ dir, llm }) {
          yield* writeSkill(dir, "open-skill", "", "OPEN_BODY_MARKER")
          yield* writeSkill(dir, "disabled-skill", "status: disabled\n", "DISABLED_BODY_MARKER")
          yield* writeSkill(dir, "revoked-skill", "status: revoked\n", "REVOKED_BODY_MARKER")
          yield* writeSkill(dir, "staged-skill", "status: staged\n", "STAGED_BODY_MARKER")
          // Hidden without positive trust is denied as untrusted (hidden alone is never trust).
          yield* writeSkill(dir, "hidden-untrusted", "hidden: true\n", "UNTRUSTED_BODY_MARKER")
          yield* llm.text("ok")

          const prompt = yield* SessionPrompt.Service
          const sessions = yield* Session.Service
          const session = yield* sessions.create({ title: "slash trust gate" })

          yield* prompt.prompt({
            sessionID: session.id,
            parts: [
              {
                type: "text",
                text: "use /open-skill and also /disabled-skill /revoked-skill /staged-skill /hidden-untrusted",
              },
            ],
            model: { providerID: ref.providerID, modelID: ref.modelID },
          })

          const msgs = yield* sessions.messages({ sessionID: session.id })
          const user = msgs.find((m) => m.info.role === "user")
          expect(user).toBeDefined()

          const loaded = injected(user!.parts)
          expect(loaded).toContain("open-skill")
          expect(loaded).not.toContain("disabled-skill")
          expect(loaded).not.toContain("revoked-skill")
          expect(loaded).not.toContain("staged-skill")
          expect(loaded).not.toContain("hidden-untrusted")

          const text = user!.parts.flatMap((p) => (p.type === "text" ? [p.text] : [])).join("\n")
          expect(text).toContain("OPEN_BODY_MARKER")
          expect(text).not.toContain("DISABLED_BODY_MARKER")
          expect(text).not.toContain("REVOKED_BODY_MARKER")
          expect(text).not.toContain("STAGED_BODY_MARKER")
          expect(text).not.toContain("UNTRUSTED_BODY_MARKER")

          yield* sessions.remove(session.id)
        }),
        { git: true, config: providerCfg },
      ),
    30_000,
  )

  it.live(
    "Command entries exclude disabled/untrusted/revoked/staged skills",
    () =>
      provideTmpdirServer(
        Effect.fnUntraced(function* ({ dir, llm }) {
          yield* writeSkill(dir, "open-skill", "", "OPEN_BODY_MARKER")
          yield* writeSkill(dir, "disabled-skill", "status: disabled\n", "DISABLED_BODY_MARKER")
          yield* writeSkill(dir, "revoked-skill", "status: revoked\n", "REVOKED_BODY_MARKER")
          yield* writeSkill(dir, "staged-skill", "status: staged\n", "STAGED_BODY_MARKER")
          yield* writeSkill(dir, "hidden-untrusted", "hidden: true\n", "UNTRUSTED_BODY_MARKER")
          yield* llm.text("ok")

          const commands = yield* Command.Service
          const names = (yield* commands.list()).map((c) => c.name)
          expect(names).toContain("open-skill")
          expect(names).not.toContain("disabled-skill")
          expect(names).not.toContain("revoked-skill")
          expect(names).not.toContain("staged-skill")
          expect(names).not.toContain("hidden-untrusted")
        }),
        { git: true, config: providerCfg },
      ),
    30_000,
  )

  it.live(
    "getForInvocation(explicit) is the slash/mention trust filter",
    () =>
      provideTmpdirServer(
        Effect.fnUntraced(function* ({ dir, llm }) {
          yield* writeSkill(dir, "open-skill", "", "OPEN_BODY_MARKER")
          yield* writeSkill(dir, "disabled-skill", "status: disabled\n", "DISABLED_BODY_MARKER")
          yield* writeSkill(dir, "revoked-skill", "status: revoked\n", "REVOKED_BODY_MARKER")
          yield* writeSkill(dir, "staged-skill", "status: staged\n", "STAGED_BODY_MARKER")
          yield* writeSkill(dir, "hidden-untrusted", "hidden: true\n", "UNTRUSTED_BODY_MARKER")
          yield* writeSkill(dir, "gated-skill", "disable-model-invocation: true\n", "GATED_BODY_MARKER")
          yield* llm.text("ok")

          const skill = yield* Skill.Service
          expect((yield* skill.getForInvocation("open-skill", "explicit")).ok).toBe(true)
          expect((yield* skill.getForInvocation("gated-skill", "explicit")).ok).toBe(true)

          for (const name of ["disabled-skill", "revoked-skill", "staged-skill", "hidden-untrusted"]) {
            const result = yield* skill.getForInvocation(name, "explicit")
            expect(result.ok).toBe(false)
          }
        }),
        { git: true, config: providerCfg },
      ),
    30_000,
  )

  it.live(
    "synthetic /name cannot inject explicit bodies",
    () =>
      provideTmpdirServer(
        Effect.fnUntraced(function* ({ dir, llm }) {
          yield* writeSkill(dir, "open-skill", "", "OPEN_BODY_MARKER")
          yield* writeSkill(dir, "gated-skill", "disable-model-invocation: true\n", "GATED_BODY_MARKER")
          yield* writeSkill(dir, "hidden-trusted", "hidden: true\ntrust: verified\n", "HIDDEN_BODY_MARKER")
          yield* llm.text("ok")

          const prompt = yield* SessionPrompt.Service
          const sessions = yield* Session.Service
          const session = yield* sessions.create({ title: "synthetic slash mention" })

          // Synthetic /name is not user-typed and must not drive explicit body injection.
          yield* prompt.prompt({
            sessionID: session.id,
            parts: [
              { type: "text", text: "please help with the task" },
              { type: "text", text: "also load /gated-skill /hidden-trusted /open-skill", synthetic: true },
            ],
            model: { providerID: ref.providerID, modelID: ref.modelID },
          })

          const msgs = yield* sessions.messages({ sessionID: session.id })
          const user = msgs.find((m) => m.info.role === "user")
          expect(user).toBeDefined()
          expect(injected(user!.parts)).toEqual([])

          const text = user!.parts.flatMap((p) => (p.type === "text" ? [p.text] : [])).join("\n")
          expect(text).not.toContain("OPEN_BODY_MARKER")
          expect(text).not.toContain("GATED_BODY_MARKER")
          expect(text).not.toContain("HIDDEN_BODY_MARKER")

          yield* sessions.remove(session.id)
        }),
        { git: true, config: providerCfg },
      ),
    30_000,
  )
})

describe("user-explicit request binding", () => {
  it.live("isUserExplicitRequest requires a user slash/mention or host token", () =>
    Effect.sync(() => {
      const userMsg = (text: string) => [
        {
          info: { role: "user" },
          parts: [{ type: "text", text }],
        },
      ]
      const assistantMsg = (text: string) => [
        {
          info: { role: "assistant" },
          parts: [{ type: "text", text }],
        },
      ]

      expect(
        Skill.isUserExplicitRequest({
          name: "hidden-trusted",
          messages: userMsg("please run /hidden-trusted now"),
        }),
      ).toBe(true)

      expect(
        Skill.isUserExplicitRequest({
          name: "hidden-trusted",
          messages: userMsg("run /other-skill"),
        }),
      ).toBe(false)

      // Model-authored text is never a user-originated signal.
      expect(
        Skill.isUserExplicitRequest({
          name: "hidden-trusted",
          messages: assistantMsg("I will run /hidden-trusted"),
        }),
      ).toBe(false)

      // Synthetic injected skill bodies are not user-typed.
      expect(
        Skill.isUserExplicitRequest({
          name: "hidden-trusted",
          messages: [
            {
              info: { role: "user" },
              parts: [{ type: "text", text: "load /hidden-trusted", synthetic: true }],
            },
          ],
        }),
      ).toBe(false)

      // Host-issued user-explicit token from the request envelope.
      expect(
        Skill.isUserExplicitRequest({
          name: "hidden-trusted",
          messages: [],
          hostTokens: ["hidden-trusted"],
        }),
      ).toBe(true)

      expect(
        Skill.isUserExplicitRequest({
          name: "hidden-trusted",
          messages: [],
          hostTokens: ["other-skill"],
        }),
      ).toBe(false)

      // A model parameter alone (no messages, no host token) never binds.
      expect(
        Skill.isUserExplicitRequest({
          name: "hidden-trusted",
          messages: [],
        }),
      ).toBe(false)
    }),
  )
})
