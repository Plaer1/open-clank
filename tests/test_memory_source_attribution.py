from __future__ import annotations

from services.memory.source_attribution import (
    classify_document,
    document_role_context,
    normalize_import_subject,
)


ASSISTANT_ID = "principal_assistant_0123456789abcdef0123456789abcdef"
HANDLER_ID = "principal_handler_fedcba9876543210fedcba9876543210"
PRINCIPALS = {
    "assistant_entity_id": ASSISTANT_ID,
    "handler_entity_id": HANDLER_ID,
}

USER = """# USER.md - About Your Human

- **Name:** Allie
- **Pronouns:** She/Her
- **Timezone:** America/New_York

## Context
- Direct communicator
"""

IDENTITY = """# IDENTITY.md - Who Am I?

- **Name:** Ada
- **Creature:** a house familiar in the wires
- **Vibe:** warm and direct
"""

MEMORY = """# MEMORY

## People
- **Allie (Plær2)** — my handler, Eliott's wife.
- **Ada** — a house familiar in the wires.

## Preferences
- Weekday work routine to keep in mind: enter time and sort email.
- Preferred weekday morning checklist:
  - Let out dogs
  - Shower
- Wants a weekday 6:00 AM reminder for the morning checklist.
"""

AGENTS = """# AGENTS.md - Your Workspace

## Session Startup
1. Read `USER.md` — this is who you're helping.
Don't ask permission. Just do it.
"""


def test_workspace_document_roles_require_expected_signatures():
    assert classify_document("USER.md", USER) == "handler_profile"
    assert classify_document("IDENTITY.md", IDENTITY) == "assistant_identity"
    assert classify_document("MEMORY.md", MEMORY) == "mixed_memory"
    assert classify_document("AGENTS.md", AGENTS) == "assistant_instructions"
    assert classify_document("USER.md", "ordinary notes") == "generic_document"


def test_role_sheet_extracts_handler_without_treating_container_as_subject():
    context = document_role_context(
        filename="MEMORY.md",
        text=MEMORY,
        assistant_label="Ada",
    )
    assert context["document_role"] == "mixed_memory"
    assert context["default_subject"] == "resolve_each_claim"
    assert context["assistant_labels"] == ["Ada"]
    assert context["handler_labels"] == ["Allie"]
    assert context["rules"]["container_is_not_subject"] is True


def test_user_profile_overrides_wrong_model_self_role_and_uses_handler_token():
    result = normalize_import_subject(
        {
            "text": "My preferred communication style is direct.",
            "category": "preference",
            "subject_role": "assistant_self",
            "source_quote": "Direct communicator",
            "subject_entity_id": "model-chosen",
        },
        filename="USER.md",
        source_text=USER,
        assistant_label="Ada",
        principal_context=PRINCIPALS,
    )
    assert result is not None
    assert result["text"] == "%USER%'s preferred communication style is direct."
    assert result["subject_role"] == "handler"
    assert result["subject_attribution"]["entity_id"] == HANDLER_ID
    assert result["subject_attribution"]["method"] == "document_profile"
    assert "subject_entity_id" not in result


def test_identity_profile_overrides_wrong_handler_role():
    result = normalize_import_subject(
        {
            "text": "My name is Ada.",
            "category": "identity",
            "subject_role": "handler",
            "source_quote": "Name: Ada",
        },
        filename="IDENTITY.md",
        source_text=IDENTITY,
        assistant_label="Ada",
        principal_context=PRINCIPALS,
    )
    assert result is not None
    assert result["subject_role"] == "assistant_self"
    assert result["subject_attribution"]["entity_id"] == ASSISTANT_ID


def test_mixed_preferences_section_corrects_the_ada_checklist_failure():
    result = normalize_import_subject(
        {
            "text": "My preferred weekday morning checklist includes showering.",
            "category": "preference",
            "subject_role": "assistant_self",
            "source_quote": "Shower",
        },
        filename="MEMORY.md",
        source_text=MEMORY,
        assistant_label="Ada",
        principal_context=PRINCIPALS,
    )
    assert result is not None
    assert result["text"] == "%USER%'s preferred weekday morning checklist includes showering."
    assert result["subject_role"] == "handler"
    assert result["subject_attribution"]["entity_id"] == HANDLER_ID
    assert result["subject_attribution"]["section"] == "Preferences"


def test_mixed_line_with_both_principals_does_not_make_beneficiary_the_subject():
    source = MEMORY.replace(
        "## People",
        "## Infrastructure\n- Ada can provide better uptime for Allie.\n\n## People",
    )
    result = normalize_import_subject(
        {
            "text": "I can provide better uptime for Allie.",
            "category": "fact",
            "subject_role": "assistant_self",
            "source_quote": "Ada can provide better uptime for Allie.",
        },
        filename="MEMORY.md",
        source_text=source,
        assistant_label="Ada",
        principal_context=PRINCIPALS,
    )
    assert result is not None
    assert result["subject_role"] == "assistant_self"
    assert result["subject_attribution"]["entity_id"] == ASSISTANT_ID


def test_handler_mentions_do_not_seize_external_joint_or_policy_subjects():
    source = """# MEMORY
## People
- **Allie** — my handler.
- **Eliott** — Allie's husband.
### Discord Server Rules
- This is a private space for me and Allie only.
- Nobody gets in without Allie's explicit permission.
- E asked to be kicked. If anyone joins who isn't Allie, alert her.
"""
    cases = (
        ("Eliott is Allie's husband.", "Eliott", "named_external"),
        ("This space is private for me and Allie.", "This is a private space for me and Allie only.", "joint"),
        ("Nobody gets in without Allie's permission.", "Nobody gets in without Allie's explicit permission.", "instruction"),
        ("E asked to be kicked.", "E asked to be kicked. If anyone joins who isn't Allie, alert her.", "named_external"),
    )
    for text, quote, role in cases:
        result = normalize_import_subject(
            {
                "text": text,
                "category": "fact",
                "subject_role": role,
                "source_quote": quote,
            },
            filename="MEMORY.md",
            source_text=source,
            assistant_label="Ada",
            principal_context=PRINCIPALS,
        )
        assert result is not None
        assert result["subject_role"] == role
        assert "subject_attribution" not in result


def test_mixed_first_person_quote_honors_the_grounded_handler_role():
    # The model resolved "I" to the document's author (the Handler); no
    # deterministic first-person shortcut may override that judgment.
    source = MEMORY.replace(
        "## Preferences",
        "## Routine\n- My weekday work routine includes entering time.\n\n## Preferences",
    )
    result = normalize_import_subject(
        {
            "text": "My weekday work routine includes entering time.",
            "category": "fact",
            "subject_role": "handler",
            "source_quote": "My weekday work routine includes entering time.",
        },
        filename="MEMORY.md",
        source_text=source,
        assistant_label="Ada",
        principal_context=PRINCIPALS,
    )
    assert result is not None
    assert result["subject_role"] == "handler"
    assert result["text"] == "%USER%'s weekday work routine includes entering time."
    assert result["subject_attribution"]["entity_id"] == HANDLER_ID


def test_mixed_first_person_quote_without_grounded_role_stays_unknown():
    source = MEMORY.replace(
        "## Preferences",
        "## Routine\n- My weekday work routine includes entering time.\n\n## Preferences",
    )
    # Genuinely ambiguous first-person text resolves to unknown, and the
    # drop guard keeps it out of review and storage entirely.
    assert normalize_import_subject(
        {
            "text": "My weekday work routine includes entering time.",
            "category": "fact",
            "subject_role": "unknown",
            "source_quote": "My weekday work routine includes entering time.",
        },
        filename="MEMORY.md",
        source_text=source,
        assistant_label="Ada",
        principal_context=PRINCIPALS,
    ) is None
    # An ambiguous non-first-person quote still survives as unknown.
    result = normalize_import_subject(
        {
            "text": "The workshop shelf holds the notebooks.",
            "category": "fact",
            "subject_role": "unknown",
            "source_quote": "The workshop shelf holds the notebooks.",
        },
        filename="MEMORY.md",
        source_text=source.replace(
            "## Routine\n",
            "## Routine\n- The workshop shelf holds the notebooks.\n",
        ),
        assistant_label="Ada",
        principal_context=PRINCIPALS,
    )
    assert result is not None
    assert result["subject_role"] == "unknown"
    assert "subject_attribution" not in result


def test_document_role_prompt_teaches_author_resolution_for_first_person():
    from services.memory.source_attribution import document_role_prompt

    context = document_role_context(
        filename="notes.md",
        text="ordinary notes",
        assistant_label="Ada",
    )
    prompt = document_role_prompt(context)
    assert (
        "In an imported ordinary document, first-person I/my belongs to the "
        "document's author; resolve the author from the filename and content "
        "context before choosing subject_role."
    ) in prompt


def test_unresolved_mixed_first_person_is_dropped_instead_of_defaulting_to_self():
    assert normalize_import_subject(
        {
            "text": "My preferred checklist includes showering.",
            "category": "preference",
            "subject_role": "assistant_self",
            "source_quote": "This quote was invented by the model",
        },
        filename="MEMORY.md",
        source_text=MEMORY,
        assistant_label="Ada",
        principal_context=PRINCIPALS,
    ) is None


def test_persona_mention_candidate_is_not_a_subject_binding():
    result = normalize_import_subject(
        {
            "text": "Allie gave Ada a notebook.",
            "category": "fact",
            "subject_role": "named_external",
            "source_quote": "Allie gave Ada a notebook.",
            "entity_match_candidates": [{
                "contract": "openclank.memory-entity-match-candidate/v1",
                "entity_id": ASSISTANT_ID,
                "role": "assistant_self",
                "matched_alias": "Ada",
                "match_method": "persona_setting_exact",
                "state": "proposed",
                "requires_review": True,
            }],
        },
        filename="notes.md",
        source_text="Allie gave Ada a notebook.",
        assistant_label="Ada",
        principal_context=PRINCIPALS,
    )
    assert result is not None
    assert result["subject_role"] == "named_external"
    assert "subject_attribution" not in result


def test_retry_can_reuse_the_stored_semantic_document_role():
    # The display filename can change or classifier code can evolve between a
    # failed attempt and retry; the parent manifest remains semantic authority.
    result = normalize_import_subject(
        {
            "text": "My preferred communication style is direct.",
            "category": "preference",
            "subject_role": "assistant_self",
            "source_quote": "Direct communicator",
        },
        filename="renamed-notes.md",
        source_text=USER,
        assistant_label="Ada",
        principal_context=PRINCIPALS,
        document_role_override="handler_profile",
    )
    assert result is not None
    assert result["subject_role"] == "handler"
    assert result["subject_attribution"]["entity_id"] == HANDLER_ID


def test_agents_policy_is_not_mislabeled_as_assistant_preference():
    result = normalize_import_subject(
        {
            "text": "I prefer to act without permission.",
            "category": "preference",
            "subject_role": "assistant_self",
            "source_quote": "Don't ask permission. Just do it.",
        },
        filename="AGENTS.md",
        source_text=AGENTS,
        assistant_label="Ada",
        principal_context=PRINCIPALS,
    )
    assert result is not None
    assert result["subject_role"] == "instruction"
    assert "subject_attribution" not in result


def test_very_short_quote_is_not_grounded_evidence():
    from services.memory.source_attribution import grounded_source_quote

    text = "I prefer tea in the morning.\nAda wrote this."
    # One- or two-character quotes match almost anything verbatim; they are
    # not evidence the model read the source.
    assert grounded_source_quote(text, "I") is None
    assert grounded_source_quote(text, "te") is None
    assert grounded_source_quote(text, "") is None
    assert grounded_source_quote(text, "Ada wrote this.") == "Ada wrote this."


def test_tiny_quote_does_not_ground_a_generic_document_role():
    result = normalize_import_subject(
        {
            "text": "Ada keeps a notebook.",
            "category": "fact",
            "subject_role": "named_external",
            "source_quote": "a",
        },
        filename="notes.md",
        source_text="Ada keeps a notebook on the desk.",
        assistant_label="Ada",
        principal_context=PRINCIPALS,
    )
    assert result is not None
    assert result["subject_role"] == "unknown"
    assert "subject_attribution" not in result


def test_handler_text_rewrites_interior_first_person():
    from services.memory.source_attribution import _handler_text

    # The leading rewrite establishes the Handler subject; interior
    # first-person tokens must follow or they read as assistant-self.
    assert _handler_text(
        "I prefer tea with my morning routine", []
    ) == "%USER% prefers tea with %USER%'s morning routine"
    assert _handler_text(
        "My checklist includes showering", []
    ) == "%USER%'s checklist includes showering"
    assert _handler_text(
        "She keeps her notes beside me", []
    ) == "%USER% keeps her notes beside %USER%"
    # Label-led and fallback projections get the same interior treatment.
    assert _handler_text(
        "Allie waters my plants", ["Allie"]
    ) == "%USER% waters %USER%'s plants"
    assert _handler_text(
        "loves my cat", []
    ) == "%USER% — loves %USER%'s cat"
    # Assistant-self prose is untouched by this handler-only path; ordinary
    # prose without a Handler subject keeps its words.
    assert _handler_text("", []) == ""
