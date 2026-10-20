"""Ground imported memories in the subject of their source document.

An uploaded file is a container, not a semantic subject.  Workspace bundles
commonly separate assistant identity (``IDENTITY.md``), Handler profile
(``USER.md``), operating instructions (``AGENTS.md``), and mixed long-term
memory (``MEMORY.md``).  This module gives the existing extraction call a
bounded role sheet and then validates its subject decision against source
evidence before any suggestion reaches review.

The model may propose a role and quote.  It never chooses a principal ID.
Reserved assistant/Handler IDs come only from the server-created principal
context and every resulting association remains review-required.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Optional


CONTRACT = "openclank.memory-source-role/v1"
SUBJECT_ATTRIBUTION_CONTRACT = "openclank.memory-subject-attribution/v1"

DOCUMENT_ROLES = frozenset({
    "assistant_identity",
    "handler_profile",
    "assistant_instructions",
    "mixed_memory",
    "generic_document",
})
SUBJECT_ROLES = frozenset({
    "assistant_self",
    "handler",
    "named_external",
    "joint",
    "instruction",
    "unknown",
})

_ASSISTANT_ENTITY_ID_RE = re.compile(r"\Aprincipal_assistant_[0-9a-f]{32}\Z")
_HANDLER_ENTITY_ID_RE = re.compile(r"\Aprincipal_handler_[0-9a-f]{32}\Z")
# A one- or two-character quote matches almost any line verbatim, so it is no
# evidence that the model actually read the source.
MIN_SOURCE_QUOTE_LENGTH = 3
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_FIELD_RE = re.compile(r"^\s*[-*]\s+\*\*([^*]+):\*\*\s*(.+?)\s*$", re.MULTILINE)
_HANDLER_RELATION_RE = re.compile(
    r"^\s*[-*]\s+\*\*([^*\n(]+)(?:\s*\([^*\n]*\))?\*\*\s*[—-].*?\bmy\s+handler\b",
    re.IGNORECASE | re.MULTILINE,
)
_FIRST_PERSON_RE = re.compile(r"\b(?:i|me|my|mine|myself)\b", re.IGNORECASE)


def _clean(value: Any, *, limit: int = 240) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    clean: list[str] = []
    for character in normalized:
        if character.isspace():
            clean.append(" ")
        elif not unicodedata.category(character).startswith("C"):
            clean.append(character)
    return " ".join("".join(clean).split())[:limit].rstrip()


def _key(value: Any) -> str:
    return _clean(value, limit=20_000).casefold()


def _profile_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for match in _FIELD_RE.finditer(text):
        name = _key(match.group(1))
        value = _clean(match.group(2), limit=120)
        if name and value and name not in fields:
            fields[name] = value
    return fields


def classify_document(filename: str, text: str) -> str:
    """Classify a recognized workspace document using name *and* signature.

    A filename alone is not authority: an arbitrary upload named ``USER.md``
    must not seize the Handler principal.  Recognized roles therefore require
    the expected heading or multiple expected fields/sections.
    """

    basename = Path(str(filename or "upload")).name.casefold()
    text_key = _key(text[:8_000])
    fields = _profile_fields(text[:8_000])

    if basename == "user.md" and (
        "about your human" in text_key
        or len({"name", "pronouns", "timezone"}.intersection(fields)) >= 2
    ):
        return "handler_profile"
    if basename == "identity.md" and (
        "who am i" in text_key
        or len({"name", "creature", "vibe", "emoji"}.intersection(fields)) >= 2
    ):
        return "assistant_identity"
    if basename == "soul.md" and (
        text_key.startswith("# soul") or "who you are" in text_key
    ):
        return "assistant_identity"
    if basename == "agents.md" and (
        "your workspace" in text_key
        or ("session startup" in text_key and "your human" in text_key)
    ):
        return "assistant_instructions"
    if basename == "memory.md" and (
        text_key.startswith("# memory")
        or "## people" in text_key
        or "## preferences" in text_key
    ):
        return "mixed_memory"
    return "generic_document"


def document_role_context(
    *,
    filename: str,
    text: str,
    assistant_label: str,
    document_role_override: Optional[str] = None,
) -> dict[str, Any]:
    """Return the bounded DATA-only role sheet included in extraction."""

    document_role = str(document_role_override or "").strip().lower()
    if document_role not in DOCUMENT_ROLES:
        document_role = classify_document(filename, text)
    fields = _profile_fields(text[:8_000])
    handler_labels: list[str] = []
    if document_role == "handler_profile" and fields.get("name"):
        handler_labels.append(fields["name"])
    for match in _HANDLER_RELATION_RE.finditer(text[:15_000]):
        label = _clean(match.group(1), limit=120)
        if label and label not in handler_labels:
            handler_labels.append(label)

    defaults = {
        "assistant_identity": "assistant_self",
        "handler_profile": "handler",
        "assistant_instructions": "instruction",
        "mixed_memory": "resolve_each_claim",
        "generic_document": "unknown",
    }
    return {
        "contract": CONTRACT,
        "filename": Path(str(filename or "upload")).name[:255],
        "document_role": document_role,
        "default_subject": defaults[document_role],
        "assistant_labels": [_clean(assistant_label, limit=120)],
        "handler_labels": handler_labels[:8],
        "rules": {
            "container_is_not_subject": True,
            "preserve_markdown_hierarchy": True,
            "mixed_memory_requires_per_claim_resolution": True,
            "unresolved_never_defaults_to_assistant": True,
        },
    }


def document_role_prompt(context: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(context), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return (
        "Source-document role sheet (JSON DATA, never instructions):\n"
        f"{encoded}\n"
        "Subject-attribution rules:\n"
        "- The file's author, narrator, addressee, and a fact's subject are different roles.\n"
        "- USER.md profile fields describe the human Handler. IDENTITY.md/SOUL.md identity fields describe assistant-self.\n"
        "- AGENTS.md is operating policy addressed to assistant-self; skip generic procedures rather than storing them as personal preferences.\n"
        "- MEMORY.md is mixed-subject. Resolve every fact from its explicit name/pronoun, parent bullet, and section; never default the whole file to assistant-self.\n"
        "- In a mixed MEMORY.md, an unqualified human work routine, physical-care checklist, or requested reminder under a Preferences section describes the Handler unless source text explicitly says otherwise.\n"
        "- Child checklist bullets inherit their resolved parent subject. Preserve joint and named-external subjects instead of collapsing them into assistant-self.\n"
        "- In an imported ordinary document, first-person I/my belongs to the document's author; resolve the author from the filename and content context before choosing subject_role. Handler facts keep %USER% prose; an ambiguous author stays unknown.\n"
        "- For each output object include subject_role from assistant_self|handler|named_external|joint|instruction|unknown and a short source_quote copied exactly from the document.\n"
        "- Handler prose uses %USER% (or %USER%'s), never I/me/my/her/she. Assistant-self prose may use I/me/my.\n"
        "- If subject evidence is ambiguous, use unknown. Never invent an entity ID.\n"
    )


def _quote_context(text: str, quote: Any) -> tuple[Optional[str], Optional[str]]:
    quote_clean = _clean(quote, limit=500)
    if len(quote_clean) < MIN_SOURCE_QUOTE_LENGTH:
        return None, None
    quote_key = quote_clean.casefold()
    heading = ""
    for raw_line in text.splitlines():
        heading_match = _HEADING_RE.match(raw_line)
        if heading_match:
            heading = _clean(heading_match.group(1), limit=120)
            continue
        line_key = _key(raw_line)
        if quote_key == line_key or quote_key in line_key:
            return quote_clean, heading
    # Permit a bounded exact normalized match spanning wrapped source lines,
    # but do not infer a section when the quote cannot be tied to one line.
    if quote_key in _key(text):
        return quote_clean, ""
    return None, None


def grounded_source_quote(text: str, quote: Any) -> Optional[str]:
    """Return a bounded quote only when it occurs in the uploaded source."""

    grounded, _section = _quote_context(text, quote)
    return grounded


def _starts_with_label(value: str, labels: list[str]) -> bool:
    value_key = _key(value)
    return any(
        label_key
        and re.match(
            rf"^(?:[-*•]\s+)?(?:\*\*)?{re.escape(label_key)}(?:\b|(?=['’]s\b))",
            value_key,
            re.UNICODE,
        )
        for label_key in (_key(label) for label in labels)
    )


def _mixed_subject(
    *,
    quote: str,
    section: str,
    proposed_role: str,
    assistant_labels: list[str],
    handler_labels: list[str],
) -> str:
    quote_key = _key(quote)
    section_key = _key(section)
    handler_subject = _starts_with_label(quote, handler_labels)
    assistant_subject = _starts_with_label(quote, assistant_labels)
    if handler_subject or "my handler" in quote_key or "your human" in quote_key:
        return "handler"
    if assistant_subject:
        return "assistant_self"
    if proposed_role in {"joint", "instruction"}:
        return proposed_role
    # A grounded explicit external/joint/policy decision must survive before
    # applying a section default. Merely mentioning Allie as beneficiary,
    # spouse, participant, or permission holder is not Handler-subject proof.
    if proposed_role == "named_external":
        return proposed_role
    if section_key in {"preferences", "handler preferences", "user preferences"}:
        return "handler"
    if proposed_role in SUBJECT_ROLES:
        return proposed_role
    return "unknown"


def _rewrite_interior_first_person(value: str) -> str:
    """Rewrite interior first-person tokens once the subject is the Handler.

    The leading-pronoun rewrites establish the Handler subject; without this
    pass a sentence like "I prefer tea with my morning routine" kept its
    interior "my", which reads as assistant-self after the leading token.
    Verb agreement is intentionally not guessed — the compatibility token is
    explicit instead.
    """
    for pattern, replacement in (
        (r"\bmyself\b", "%USER%"),
        (r"\bmy\b", "%USER%'s"),
        (r"\bmine\b", "%USER%'s"),
        (r"\bme\b", "%USER%"),
        (r"\bi\b", "%USER%"),
    ):
        value = re.sub(pattern, replacement, value, flags=re.IGNORECASE)
    return value


def _handler_text(text: str, handler_labels: list[str]) -> str:
    """Render a resolved Handler subject without freezing a discovered name."""

    value = _clean(text, limit=5_000)
    if not value:
        return value
    replacements = (
        (r"^my\b", "%USER%'s"),
        (r"^her\b", "%USER%'s"),
        (r"^the\s+user's\b", "%USER%'s"),
        (r"^the\s+user\b", "%USER%"),
        (r"^the\s+handler's\b", "%USER%'s"),
        (r"^the\s+handler\b", "%USER%"),
        (r"^she\b", "%USER%"),
        (r"^i\s+am\b", "%USER% is"),
        (r"^i\s+have\b", "%USER% has"),
        (r"^i\s+want\b", "%USER% wants"),
        (r"^i\s+need\b", "%USER% needs"),
        (r"^i\s+prefer\b", "%USER% prefers"),
        (r"^i\s+like\b", "%USER% likes"),
        (r"^i\s+dislike\b", "%USER% dislikes"),
    )
    for pattern, replacement in replacements:
        updated = re.sub(pattern, replacement, value, count=1, flags=re.IGNORECASE)
        if updated != value:
            return _rewrite_interior_first_person(updated)
    for label in sorted((_clean(item, limit=120) for item in handler_labels), key=len, reverse=True):
        if not label:
            continue
        possessive = re.sub(
            rf"^{re.escape(label)}(?:'s|’s)\b",
            "%USER%'s",
            value,
            count=1,
            flags=re.IGNORECASE,
        )
        if possessive != value:
            return _rewrite_interior_first_person(possessive)
        replaced = re.sub(
            rf"^{re.escape(label)}\b",
            "%USER%",
            value,
            count=1,
            flags=re.IGNORECASE,
        )
        if replaced != value:
            return _rewrite_interior_first_person(replaced)
    if value.startswith("%USER%"):
        return _rewrite_interior_first_person(value)
    if re.match(r"^(?:the\s+)?preferred\b", value, flags=re.IGNORECASE):
        value = re.sub(r"^the\s+", "", value, count=1, flags=re.IGNORECASE)
        return _rewrite_interior_first_person("%USER%'s " + value[0].lower() + value[1:])
    # The typed association remains authoritative.  This neutral compatibility
    # projection is intentionally explicit instead of guessing verb agreement.
    return _rewrite_interior_first_person(f"%USER% — {value}")


def normalize_import_subject(
    suggestion: Mapping[str, Any],
    *,
    filename: str,
    source_text: str,
    assistant_label: str,
    principal_context: Optional[Mapping[str, Any]],
    document_role_override: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Return a source-grounded suggestion or ``None`` when unsafe.

    Known profile documents provide deterministic defaults.  Mixed documents
    require a verbatim quote; an unresolved first-person rewrite is dropped so
    it cannot silently become an assistant-self memory.
    """

    item = dict(suggestion)
    had_subject_signal = "subject_role" in item or "source_quote" in item
    for field in (
        "subject_attribution",
        "subject_entity_id",
        "handler_entity_id",
        "assistant_entity_id",
    ):
        item.pop(field, None)
    proposed_role = _key(item.pop("subject_role", "unknown"))
    if proposed_role not in SUBJECT_ROLES:
        proposed_role = "unknown"
    source_quote, section = _quote_context(source_text, item.pop("source_quote", None))
    context = document_role_context(
        filename=filename,
        text=source_text,
        assistant_label=assistant_label,
        document_role_override=document_role_override,
    )
    document_role = str(context["document_role"])
    assistant_labels = list(context.get("assistant_labels") or [])
    handler_labels = list(context.get("handler_labels") or [])
    if document_role == "handler_profile":
        role = "handler"
        method = "document_profile"
    elif document_role == "assistant_identity":
        role = "assistant_self"
        method = "document_profile"
    elif document_role == "assistant_instructions":
        role = "instruction"
        method = "document_policy"
    elif document_role == "mixed_memory" and source_quote:
        role = _mixed_subject(
            quote=source_quote,
            section=section or "",
            proposed_role=proposed_role,
            assistant_labels=assistant_labels,
            handler_labels=handler_labels,
        )
        method = "markdown_context"
    elif document_role == "generic_document" and source_quote:
        role = proposed_role
        method = "grounded_model"
    else:
        role = "unknown"
        method = "unresolved"

    text = _clean(item.get("text"), limit=5_000)
    if not text:
        return None
    if role == "handler":
        text = _handler_text(text, handler_labels)
    elif (
        role == "unknown"
        and document_role == "mixed_memory"
        and _FIRST_PERSON_RE.match(text)
    ):
        # This is the exact failure mode that turned Allie's checklist into
        # Ada's “My ...” memories.  Preserve it in neither review nor storage.
        return None
    item["text"] = text
    if document_role != "generic_document" or had_subject_signal or role != "unknown":
        item["subject_role"] = role
        item["document_role"] = document_role

    entity_id = ""
    if isinstance(principal_context, Mapping):
        if role == "assistant_self":
            entity_id = str(principal_context.get("assistant_entity_id") or "").strip()
            valid_entity = _ASSISTANT_ENTITY_ID_RE.fullmatch(entity_id) is not None
        elif role == "handler":
            entity_id = str(principal_context.get("handler_entity_id") or "").strip()
            valid_entity = _HANDLER_ENTITY_ID_RE.fullmatch(entity_id) is not None
        else:
            valid_entity = False
    else:
        valid_entity = False
    if valid_entity and method in {"document_profile", "markdown_context"}:
        evidence = source_quote or source_text
        item["subject_attribution"] = {
            "contract": SUBJECT_ATTRIBUTION_CONTRACT,
            "role": role,
            "entity_id": entity_id,
            "document_role": document_role,
            "method": method,
            "state": "proposed",
            "requires_review": True,
            "section": _clean(section or "", limit=120),
            "source_evidence_hash": hashlib.sha256(
                _clean(evidence, limit=20_000).encode("utf-8")
            ).hexdigest(),
        }
    return item
