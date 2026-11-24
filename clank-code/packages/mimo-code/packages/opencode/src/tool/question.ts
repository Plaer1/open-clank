import z from "zod"
import { Effect } from "effect"
import * as Tool from "./tool"
import { Question } from "../question"
import DESCRIPTION from "./question.txt"

const parameters = z.object({
  questions: z.array(Question.Prompt.zod).describe("Questions to ask"),
  blocking: z.boolean().optional().describe("Wait for an answer before continuing; set false only when independent work can continue safely"),
})

type Metadata = {
  answers: ReadonlyArray<Question.Answer>
  requestID?: string
  blocking?: boolean
}

export const QuestionTool = Tool.define<typeof parameters, Metadata, Question.Service>(
  "question",
  Effect.gen(function* () {
    const question = yield* Question.Service

    return {
      description: DESCRIPTION,
      parameters,
      execute: (params: z.infer<typeof parameters>, ctx: Tool.Context<Metadata>) =>
        Effect.gen(function* () {
          // never-ask mode: the tool stays visible so the model keeps routing
          // every decision through it, but instead of blocking for a human we
          // hand the decision back to the model — it knows which option fits
          // headless execution better than a hardcoded pick would.
          if (yield* question.neverAsk()) {
            const autoAnswer = "[Never-Ask] The model will decide autonomously"
            return {
              title: `Auto-resolved ${params.questions.length} question${params.questions.length > 1 ? "s" : ""}`,
              output:
                "[Never-Ask] No user is available to answer (never-ask mode is on). " +
                "Re-evaluate the options you just proposed for unattended/headless execution — " +
                "prefer text-only, non-interactive, minimal-scope paths and avoid anything that needs a GUI or the user to be present. " +
                "Pick the best option yourself and continue. " +
                "IMPORTANT: You MUST explicitly state which option you chose and why in your response text (not just in thinking) — " +
                "the user will review the conversation history later and needs to see what you decided without expanding thinking blocks. " +
                "This applies only to this question; never-ask may be turned off later, so keep using the question tool at future decision points (the user may have returned).",
              metadata: {
                answers: params.questions.map(() => [autoAnswer]),
              },
            }
          }

          if (params.blocking === false) {
            const actorID = typeof ctx.extra?.registeredActorID === "string" ? ctx.extra.registeredActorID : undefined
            if (!actorID) throw new Error("optional question requires a registered actor identity")
            const actorExecution = ctx.extra?.actorExecution as { actorID?: string; executionRevision?: number } | undefined
            if (!actorExecution || actorExecution.actorID !== actorID || actorExecution.executionRevision === undefined)
              throw new Error("optional question requires its exact actor execution")
            // Main user messages may have no agentID in the in-memory message;
            // persistence and slice reads normalize that default to `main`.
            const origin = [...ctx.messages].reverse().find((message) =>
              message.info.role === "user" && (message.info.agentID ?? "main") === actorID,
            )
            if (!origin || !ctx.callID) throw new Error("optional question requires its originating user turn and tool call")
            const request = yield* question.askOptional({
              sessionID: ctx.sessionID,
              actorID,
              executionRevision: actorExecution.executionRevision,
              originTurnID: origin.info.id,
              questions: params.questions,
              tool: { messageID: ctx.messageID, callID: ctx.callID },
            })
            return {
              title: `Saved ${params.questions.length} optional question${params.questions.length > 1 ? "s" : ""}`,
              output: "The question is saved for the user. Continue with independent work; do not assume an answer or approval.",
              metadata: { requestID: String(request.id), blocking: false, answers: [] },
            }
          }

          const answers = yield* question.ask({
            sessionID: ctx.sessionID,
            questions: params.questions,
            tool: ctx.callID ? { messageID: ctx.messageID, callID: ctx.callID } : undefined,
          })

          const formatted = params.questions
            .map((q, i) => `"${q.question}"="${answers[i]?.length ? answers[i].join(", ") : "Unanswered"}"`)
            .join(", ")

          return {
            title: `Asked ${params.questions.length} question${params.questions.length > 1 ? "s" : ""}`,
            output: `User has answered your questions: ${formatted}. You can now continue with the user's answers in mind.`,
            metadata: {
              answers,
            },
          }
        }).pipe(Effect.orDie),
    }
  }),
)
