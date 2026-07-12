from __future__ import annotations

from henxels import statement


ENGAGEMENT_MARKERS = (
    "Manual Henxel: cant stop dont stop wont stop",
    "cant-stop-dont-stop-wont-stop: engaged",
)

REQUIRED_PHRASES = (
    "bun run henxels:manual:cant-stop-dont-stop-wont-stop",
    "No Goal mode",
    "No Plan mode",
    "No subagents",
    "No workspace agents",
    "one agent",
    "chunk",
    "user-specified chunk",
    "Do not stop after any slice",
    "future recursive plan",
    "copy this manual henxel block",
)


@statement(
    "cant_stop_dont_stop_wont_stop",
    help="engaged recursive plans carry the manual one-agent chunk-completion guardrails",
)
def cant_stop_dont_stop_wont_stop(file, scope):
    text = scope.read_text(file)
    if not any(marker in text for marker in ENGAGEMENT_MARKERS):
        return None

    missing = [phrase for phrase in REQUIRED_PHRASES if phrase not in text]
    if missing:
        return [
            f"{file}: engaged cant-stop plan missing required phrase: {phrase}"
            for phrase in missing
        ]
    return None
