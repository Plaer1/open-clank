"""model_interaction_tools.py - agent tools for talking to other models.

Owns the model-interaction tool implementations (chat_with_model, ask_teacher,
list_models) and their handler classes, registered in ``TOOL_HANDLERS``. Part
of the tool -> registry migration (#3629): the implementations were moved here
out of ``src.ai_interaction`` so dispatch flows through the registry instead of
the elif chain / dispatch_ai_tool in tool_execution.py.

Model selection and completion run through the normalized provider control
plane; this module never resolves provider transport or credentials itself.
"""
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


_TEACHER_SYSTEM_PROMPT = (
    "You are a senior AI mentor. A less capable model is stuck on a problem and asking for help. "
    "Provide clear, actionable guidance:\n"
    "1. Brief analysis of the problem\n"
    "2. Recommended approach (step by step)\n"
    "3. Key things to watch out for\n\n"
    "Be concise and practical. No preamble."
)


async def chat_with_model(
    content: str,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    root_operation_id: Optional[str] = None,
) -> Dict:
    """Send a message to a specific model and return its response.

    Content format:
      Line 1: model_name (or model_name@endpoint_name)
      Line 2+: the message to send
    """
    from src.openclank.chat_routing import resolve_chat_model_spec
    from src.openclank.modality_facade import complete_text

    lines = content.strip().split("\n", 1)
    if not lines or not lines[0].strip():
        return {"error": "First line must be the model name"}

    model_spec = lines[0].strip()
    message = lines[1].strip() if len(lines) > 1 else ""
    if not message:
        return {"error": "No message provided (line 2+ is the message)"}

    try:
        route = resolve_chat_model_spec(owner=owner, model_spec=model_spec)
    except ValueError as e:
        return {"error": str(e)}

    try:
        response = await complete_text(
            owner=owner or "local-installation",
            purpose="utility",
            messages=[{"role": "user", "content": message}],
            model_route_id=route.model_route_id,
            grant_id=route.provider_grant_id,
            root_operation_id=root_operation_id,
        )
        # Truncate very long responses
        if len(response) > 10000:
            response = response[:10000] + "\n... (truncated)"
        return {"model": route.provider_model_id, "response": response}
    except Exception as e:
        logger.error(f"chat_with_model failed: {e}")
        return {"error": f"Failed to get response from {model_spec}: {e}"}


async def ask_teacher(
    content: str,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    root_operation_id: Optional[str] = None,
) -> Dict:
    """Ask a more capable model for help.

    Content format:
      Line 1: model_name (or 'auto')
      Line 2+: the problem description
    """
    from src.openclank.chat_routing import resolve_chat_model_spec
    from src.openclank.modality_facade import complete_text
    from src.settings import get_user_setting

    lines = content.strip().split("\n", 1)
    model_spec = lines[0].strip() if lines else "auto"
    problem = lines[1].strip() if len(lines) > 1 else ""

    if not problem:
        return {"error": "No problem description provided"}

    if model_spec.lower() in ("auto", ""):
        model_spec = get_user_setting("teacher_model", owner or "", "")
        if not model_spec:
            return {"error": "No teacher model configured. Specify a model name or set teacher_model in settings."}

    try:
        route = resolve_chat_model_spec(owner=owner, model_spec=model_spec)
    except ValueError as e:
        return {"error": str(e)}

    try:
        response = await complete_text(
            owner=owner or "local-installation",
            purpose="utility",
            messages=[
                {"role": "system", "content": _TEACHER_SYSTEM_PROMPT},
                {"role": "user", "content": f"Problem:\n{problem}"},
            ],
            model_route_id=route.model_route_id,
            grant_id=route.provider_grant_id,
            root_operation_id=root_operation_id,
        )
        if len(response) > 8000:
            response = response[:8000] + "\n... (truncated)"
        return {
            "model": route.provider_model_id,
            "response": response,
            "teacher": True,
        }
    except Exception as e:
        logger.error(f"ask_teacher failed: {e}")
        return {"error": f"Teacher call failed ({model_spec}): {e}"}


async def list_models(content: str, session_id: Optional[str] = None, owner: Optional[str] = None) -> Dict:
    """List all available models in the normalized provider catalog.

    Content = optional filter keyword.
    """
    from src.openclank.chat_routing import list_chat_routes

    keyword = content.strip().lower() if content.strip() else None

    try:
        own, shared = list_chat_routes(owner)
        routes = [*own, *shared]
        result_lines = []
        for route in routes:
            if keyword and not any(
                keyword in value.casefold()
                for value in (
                    route.provider_model_id,
                    route.display_name,
                    route.connection_label,
                )
            ):
                continue
            result_lines.append(
                f"- `{route.model_route_id}` — {route.display_name} "
                f"({route.connection_label})"
            )

        if not result_lines:
            return {"results": "No models found" + (f" matching '{keyword}'" if keyword else "") + "."}

        header = f"Available models ({len(result_lines)} total):\n"
        return {"results": header + "\n".join(result_lines)}
    except Exception as e:
        logger.error(f"list_models failed: {e}")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Handler classes registered in TOOL_HANDLERS
# ---------------------------------------------------------------------------

class ChatWithModelTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        return await chat_with_model(
            content,
            ctx.get("session_id"),
            owner=ctx.get("owner"),
            root_operation_id=ctx.get("root_operation_id"),
        )


class AskTeacherTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        return await ask_teacher(
            content,
            ctx.get("session_id"),
            owner=ctx.get("owner"),
            root_operation_id=ctx.get("root_operation_id"),
        )


class ListModelsTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        return await list_models(content, ctx.get("session_id"), owner=ctx.get("owner"))
