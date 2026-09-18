import json
import logging

logger = logging.getLogger(__name__)

class AskUserTool:
    async def execute(self, content, ctx):
        """
        ask_user: the agent poses a multiple-choice question to the user to get a
        decision/clarification. This is a pure UI-control marker — no subprocess,
        no filesystem. It returns an `ask_user` payload that the agent loop turns
        into an `ask_user` SSE event and then ENDS the turn, so the chat waits for
        the user's selection (their choice arrives as the next message).
        """
        question, options, multi = "", [], False
        raw = (content or "").strip()
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            parsed = {}

        if isinstance(parsed, dict):
            question = str(parsed.get("question", "")).strip()
            multi = bool(parsed.get("multi") or parsed.get("multiSelect"))
            for opt in (parsed.get("options") or []):
                if isinstance(opt, dict):
                    label = str(opt.get("label", "")).strip()
                    descr = str(opt.get("description", "")).strip()
                elif isinstance(opt, str):
                    label, descr = opt.strip(), ""
                else:
                    continue
                if label:
                    options.append({"label": label, "description": descr})
        else:
            question = raw

        if not question or len(options) < 2:
            return "ask_user: invalid", {
                "error": (
                    "ask_user needs a non-empty `question` and at least 2 `options` "
                    "(each an object with a `label`, optional `description`)."
                ),
                "exit_code": 1,
            }

        options = options[:6]  # keep the choice list sane
        # Permission mode "auto": never block the turn on a question — hand
        # the agent a plain tool result telling it to proceed on its own
        # judgment instead of emitting the interactive card.
        try:
            owner = str((ctx or {}).get("owner") or "") if isinstance(ctx, dict) else ""
            from src.permission_mode import suppresses_questions

            if owner and suppresses_questions(owner):
                logger.info("ask_user suppressed by owner permission mode (auto)")
                return "ask_user: auto mode", {
                    "output": (
                        "Auto mode: the user is not answering questions this turn. "
                        "Do NOT ask again — proceed with the most reasonable option "
                        "and note the assumption in your reply."
                    ),
                    "exit_code": 0,
                }
        except Exception:
            pass
        desc = f"ask_user: {question[:80]}"
        labels = ", ".join(o["label"] for o in options)
        result = {
            "ask_user": {"question": question, "options": options, "multi": multi},
            "output": f"Asked the user: {question}\nOptions: {labels}\nAwaiting their selection.",
            "exit_code": 0,
        }
        logger.info("Tool executed: %s (%d options, multi=%s)", desc, len(options), multi)
        return desc, result

class UpdatePlanTool:
    async def execute(self, content, ctx):
        """
        update_plan: the agent writes back to the active plan — tick an item done
        or revise steps (e.g. when the user asks to change something). Pure UI
        marker: returns a `plan_update` payload the agent loop turns into a
        `plan_update` SSE event; the frontend replaces the stored plan and refreshes
        the docked plan window. Does NOT end the turn.
        """
        raw = (content or "").strip()
        plan = ""
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            parsed = {}

        if isinstance(parsed, dict) and parsed.get("plan"):
            plan = str(parsed.get("plan", "")).strip()
        else:
            plan = raw

        if not plan:
            return "update_plan: invalid", {
                "error": "update_plan needs a non-empty `plan` (the full updated checklist as markdown).",
                "exit_code": 1,
            }

        plan = plan[:8192]
        done = plan.count("- [x]") + plan.count("- [X]")
        total = done + plan.count("- [ ]")
        desc = f"update_plan: {done}/{total} done" if total else "update_plan"
        output = f"Plan updated ({done}/{total} steps complete)." if total else "Plan updated."
        # Persist homegrown progress through the same owner/session-scoped plan
        # state used by ACP.  A changed checklist is a new draft revision and
        # therefore clears Execute approval until the owner explicitly
        # re-approves that exact revision.  This is deliberately fail-closed;
        # a browser/SSE marker is never the authority for later turns.
        plan_state = None
        try:
            session_id = str((ctx or {}).get("session_id") or "") if isinstance(ctx, dict) else ""
            owner = str((ctx or {}).get("owner") or "") if isinstance(ctx, dict) else ""
            if session_id and owner:
                from src.plan_approval import save_plan_draft

                plan_state = save_plan_draft(session_id, owner, plan)
        except Exception as exc:
            logger.warning("update_plan: session state mirror failed: %s", exc)

        # Mirror progress to the on-disk plan file when a workspace is active
        # (owner convention: <workspace>/.clanker/futures/<metaplan>.md).
        # Best-effort:
        # a write failure must never break the turn. Reuse the server-bound
        # relative path so heading/checklist edits cannot fork artifacts.
        try:
            from src.plan_files import materialize_plan
            from src.tool_execution import get_active_workspace

            workspace = get_active_workspace()
            if workspace:
                rel = materialize_plan(
                    workspace,
                    plan,
                    relative_path=(plan_state or {}).get("artifact_relpath"),
                )
                if rel:
                    output += f" Saved to {rel}."
        except Exception as exc:
            logger.warning("update_plan: on-disk plan mirror failed: %s", exc)
        if plan_state:
            # Keep the wire shape consumed by the existing plan dock while
            # carrying the durable revision/digest/status/path metadata.
            result_plan = plan_state
        else:
            result_plan = {"plan": plan}
        result = {
            "plan_update": result_plan,
            "output": output,
            "exit_code": 0,
        }
        logger.info("Tool executed: %s", desc)
        return desc, result
