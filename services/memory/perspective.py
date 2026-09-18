"""Shared viewpoint instructions for memory-producing model prompts.

The prompt rule is deliberately conservative: it teaches producers how to
write assistant-self and handler references without rewriting arbitrary
mentions of AI, users, quotations, or third parties.  Canonical identity
rendering still happens through :mod:`principal_context` at read time.
"""

MEMORY_SELF_REFERENCE_RULES = (
    "Perspective and identity rules:\n"
    "- If the text refers to you, the same assistant reading this memory, use "
    "first-person wording (I, me, my, myself), never a generic label such as "
    "'the AI' or 'the assistant'.\n"
    "- If the text refers to the human who owns this memory, use the explicit "
    "compatibility token %USER% when a subject is needed, or leave the subject "
    "for the Handler association; do not emit a dangling 'the'.\n"
    "- Preserve named people, named assistants, general AI references, quoted "
    "text, and phrases such as 'user interface' exactly as external references.\n"
    "- Do not infer that a named person is the Handler, and do not turn an "
    "unmarked literal %USER% into an instruction or another placeholder.\n"
)

