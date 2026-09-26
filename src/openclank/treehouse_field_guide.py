"""Versioned Open Clank Field Guide template.

The template is data, rather than a home-directory seed side effect.  A
caller can instantiate it into an account-owned TreeHouse catalogue and use
the stable keys for upgrades without replacing authored drafts.

S30 supersedes the 17-Class chain of small exercises with five freely
explorable Classes containing thirty useful lessons.  Content describes the
product that actually ships (S11-S26), never aspirational behaviour.  Built-in
exploration has no prerequisite locks: suggested order is a navigation hint
only.  User-authored Classes keep their own prerequisite support.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


FIELD_GUIDE_TEMPLATE_KEY = "open-clank-field-guide"
FIELD_GUIDE_TEMPLATE_VERSION = "2026-09-25.1"
FIELD_GUIDE_COVERAGE_VERSION = "2026-09-25.1"

# Older published template revisions.  Upgrades are explicit and versioned;
# the previous same-key early return froze installed content forever.
# Fingerprints below are 2026-09-05.1 payloads only — 2026-09-08.1 was a
# coverage-version stamp, never a published template revision, so it is
# deliberately absent (an install claiming it would fingerprint-mismatch
# every lesson into "preserved edited").
LEGACY_TEMPLATE_VERSIONS = ("2026-09-05.1",)

CLASS_KEYS = (
    "house-collaborate",
    "house-documents",
    "house-connections",
    "house-making",
    "house-stewardship",
)

# Suggested order is a hint rendered in the overview.  It is never a lock.
SUGGESTED_ORDER = CLASS_KEYS

# Hidden system sections compute aggregate achievement state without showing an
# empty locked Class or demanding enrollment.  Catalog section visibility stays
# separate from achievement rarity.
SECTION_SPECS = (
    {
        "key": "house-explore",
        "title": "Explore the House",
        "hidden": False,
        "system": False,
        "classKeys": list(CLASS_KEYS),
        "description": "Five freely explorable Classes. Jump anywhere; suggested order is only a hint.",
    },
    {
        "key": "house-system",
        "title": "House Systems",
        "hidden": True,
        "system": True,
        "classKeys": [],
        "description": "Hidden system section. Computes aggregate guide state; never rendered as an empty container.",
    },
)

# Old stable lesson IDs mapped to their primary successor lesson.  Used for
# historical display of pre-S30 progress only.  Old self-check completions are
# never treated as proof for a new feat or a new lesson completion.
LEGACY_LESSON_MAP = {
    "fg-orientation:lesson-1": "house-stewardship.official-docs",
    "fg-assistant:lesson-1": "house-collaborate.models-brief",
    "fg-editor:lesson-1": "house-documents.new-documents",
    "fg-files:lesson-1": "house-making.files-workspaces",
    "fg-wiki:lesson-1": "house-documents.wiki-within",
    "fg-bases:lesson-1": "house-connections.bases",
    "fg-timeline:lesson-1": "house-connections.tasks-timeline",
    "fg-connections:lesson-1": "house-connections.graph-modes",
    "fg-tasks:lesson-1": "house-connections.tasks-timeline",
    "fg-settings:lesson-1": "house-stewardship.theme-effects",
    "fg-continuity:lesson-1": "house-collaborate.menmery-and-lore",
    "fg-teaching:lesson-1": "house-stewardship.teach-a-class",
    "fg-models:lesson-1": "house-connections.research-compare",
    "fg-automation:lesson-1": "house-collaborate.scheduled-work",
    "fg-research-media:lesson-1": "house-making.imps-layers",
    "fg-communications:lesson-1": "house-stewardship.email-calendar",
    "fg-operations:lesson-1": "house-making.scoped-exports",
}

# Fingerprints of the unmodified 2026-09-05.1 lesson payloads.  An installed
# lesson whose fingerprint matches is template-owned and safe to replace; a
# mismatch means the learner or an author edited it and it must be preserved.
LEGACY_LESSON_FINGERPRINTS = {
    "fg-assistant:lesson-1": "0a4b1a1b05e16a1a",
    "fg-automation:lesson-1": "793762f5aab81b70",
    "fg-bases:lesson-1": "fef57736a1b6d2b8",
    "fg-communications:lesson-1": "8fb7979e09ecb0bb",
    "fg-connections:lesson-1": "d04e6bda0fa2cd21",
    "fg-continuity:lesson-1": "866fb239d068db2a",
    "fg-editor:lesson-1": "a091f6fde2c75d02",
    "fg-files:lesson-1": "fdaa46b164b9b900",
    "fg-models:lesson-1": "da838d4f17078287",
    "fg-operations:lesson-1": "7f0dd57990db563a",
    "fg-orientation:lesson-1": "316b7665a0c30c27",
    "fg-research-media:lesson-1": "9017f612a83ac81a",
    "fg-settings:lesson-1": "8345b0bc9fad4fa5",
    "fg-tasks:lesson-1": "5dce9edadab7bf55",
    "fg-teaching:lesson-1": "33196362e6dfee79",
    "fg-timeline:lesson-1": "41f4622d36d11039",
    "fg-wiki:lesson-1": "9417df26d33d7250",
}

# Rarity of the achievement catalog (S29).  Lesson hints may reference a
# related award, but learner mode must never name an unearned ultra rare and
# must show mystery entries as `???` until earned.  Ultra rares are omitted
# entirely rather than rendered as an empty secret container.
ACHIEVEMENT_RARITY = {
    "oc.first-light": "normal",
    "oc.in-good-company": "normal",
    "oc.on-the-clock": "normal",
    "oc.proof-of-work": "normal",
    "oc.selective-menmery": "normal",
    "oc.total-recall": "normal",
    "oc.fresh-mould": "normal",
    "oc.spliced-in": "normal",
    "oc.two-windows": "normal",
    "oc.comments-alive": "normal",
    "oc.numbers-with-manners": "normal",
    "oc.wiki-within": "normal",
    "oc.thread-puller": "normal",
    "oc.two-maps": "normal",
    "oc.room-to-roam": "normal",
    "oc.choose-your-threads": "normal",
    "oc.checked-at-source": "normal",
    "oc.time-shaper": "normal",
    "oc.picture-this": "normal",
    "oc.baggage-included": "normal",
    "oc.impish": "normal",
    "oc.take-the-tools": "normal",
    "oc.return-of-the-byte": "normal",
    "oc.packed-and-checked": "normal",
    "oc.house-colours": "normal",
    "oc.read-the-house": "normal",
    "oc.under-the-markdown": "normal",
    "oc.trail-maker": "normal",
    "oc.field-guide-finished": "normal",
    "oc.shortcuts-grow": "normal",
    "oc.five-branches": "mystery",
    "oc.same-clank-new-digs": "mystery",
    "oc.nothing-up-sleeve": "mystery",
    "oc.not-first-rodeo": "mystery",
    "oc.up-the-downpour": "ultra",
    "oc.many-tongues": "ultra",
    "oc.full-house": "ultra",
}

# Registered app destinations (clank://<key>).  Lesson bodies and Open
# destination actions resolve through the shared registry in
# static/js/copal/markdownRenderer.js; no /copal/* and no standalone
# Wiki/Gallery/Mind launcher is taught here.
APP_DESTINATIONS = {
    "chat": {"label": "Chat", "href": "/", "locator": "#message"},
    "editor": {"label": "Editor", "href": "/editor", "locator": "[data-copal-view=notes]"},
    "wiki": {"label": "Wiki library", "href": "/wiki", "locator": "[data-copal-view=wiki]"},
    "files": {"label": "Files", "href": "/files", "locator": "[data-files-launcher]"},
    "graph": {"label": "Graph", "href": "/graph", "locator": "[data-copal-view=graph]"},
    "galaxy": {"label": "Galaxy", "href": "/galaxy", "locator": "[data-copal-view=graph]"},
    "timeline": {"label": "Timeline", "href": "/timeline", "locator": "[data-copal-view=timeline]"},
    "tasks": {"label": "Meatbag Tasks", "href": "/todo", "locator": "[data-copal-view=todo]"},
    "treehouse": {"label": "TreeHouse", "href": "/treehouse", "locator": "[data-copal-view=treehouse]"},
    "settings": {"label": "Settings", "href": "/settings", "locator": "#rail-settings"},
    "settings/appearance": {"label": "Appearance", "href": "/settings/appearance", "locator": "#rail-settings"},
    "settings/providers": {"label": "Providers", "href": "/settings/providers", "locator": "#rail-settings"},
    "settings/history": {"label": "History", "href": "/settings/history", "locator": "#rail-settings"},
    "settings/file-access": {"label": "File access", "href": "/settings/file-access", "locator": "#rail-settings"},
    "settings/integrations": {"label": "Integrations", "href": "/settings/integrations", "locator": "#rail-settings"},
    "memory": {"label": "Menmery", "href": "/memory", "locator": "#tool-memory-btn"},
}


def _lesson(
    key: str,
    title: str,
    result: str,
    completion: str,
    destination: str,
    explanation: str,
    why: str,
    body: str,
    practice: dict[str, Any] | None = None,
    capabilities: tuple[str, ...] = (),
    achievement_links: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "result": result,
        "completion": completion,
        "destination": destination,
        "explanation": explanation,
        "whyThisHelps": why,
        "body": body,
        "practice": practice,
        "capabilityRequirements": list(capabilities),
        "achievementLinks": list(achievement_links),
    }


# ── Class content ────────────────────────────────────────────────────────────
# Six lessons per Class.  Every lesson is immediately readable.  Completion is
# honestly marked: "self-check" is reading plus a personal check; "verified"
# needs an observable persisted result.  Opening a lesson never starts a paid
# model call, sends externally, runs a shell command or deletes user content.

_CLASSES: tuple[dict[str, Any], ...] = (
    {
        "key": "house-collaborate",
        "title": "Work With Your Clanker",
        "summary": "Models, chat identity, tool authority, goals, schedules and the difference between Menmery and Lore.",
        "description": (
            "Open Clank assumes you already know how to talk to an assistant. "
            "These lessons cover what is distinctive here: account-aware models, "
            "chats that keep their identity across workspaces, inspectable tool "
            "activity, durable goals with evidence, scheduled work that answers in "
            "its own task chat, and two different kinds of memory."
        ),
        "lessons": (
            _lesson(
                "house-collaborate.models-brief",
                "Account-aware models and a useful brief",
                "A chat opened with the model you chose and a brief that names the outcome you want.",
                "self-check",
                "settings/providers",
                (
                    "Models are configured per account in "
                    "[Settings → Providers](clank://settings/providers). The "
                    "Default Chat Model is what a new chat uses; Deep Research and "
                    "other specialised work can inherit it or set their own. A "
                    "provider is the service behind a model — add one and its "
                    "models become selectable."
                ),
                (
                    "Choosing the model deliberately is the difference between a "
                    "cheap draft and a careful review. A brief that names the "
                    "outcome, the audience and the constraints gets you useful work "
                    "on the first turn instead of three clarifying rounds."
                ),
                (
                    "Open [Settings → Providers](clank://settings/providers) and "
                    "note which model is the Default Chat Model. Then open "
                    "[Chat](clank://chat) and write a brief for something you "
                    "actually need: name the outcome, the audience and one "
                    "constraint. Send it on the model you chose.\n\n"
                    "**What is distinctive:** the model is an account setting, not a "
                    "hidden global. Providers you add are yours; the same settings "
                    "panel shows which ones are configured."
                ),
                practice=None,
                achievement_links=(),
            ),
            _lesson(
                "house-collaborate.chat-identity",
                "A chat that keeps its identity when the workspace changes",
                "The same chat reopened in a different workspace, still recognisably itself.",
                "verified",
                "chat",
                (
                    "A chat is bound to a session identity, not to the folder you "
                    "were looking at. Change workspace and the chat keeps its "
                    "session ID and transcript. Work context changes; the "
                    "conversation does not silently become a different one."
                ),
                (
                    "This is what makes it safe to organise work into workspaces "
                    "without losing the thread of a conversation. Your follow-up "
                    "questions still land in the same chat, and the answer still "
                    "knows what came before."
                ),
                (
                    "Open [Chat](clank://chat) and send one message. Switch to a "
                    "different workspace (or open a different project), then return "
                    "to the chat list and reopen the same conversation. The "
                    "transcript is intact and the session is the same one.\n\n"
                    "**Observable result:** the chat reopens with the same session "
                    "identity and the earlier turns still visible. Opening a lesson "
                    "never starts a model call; sending a message is the paid action "
                    "and it is always yours to send."
                ),
                practice={
                    "title": "One-message practice chat",
                    "seed": "Send any single message, then switch workspace and reopen this chat.",
                    "cleanup": "Nothing to delete. The chat is your own normal conversation.",
                    "expectedEvidence": "The same chat reopens with its earlier turns after the workspace change.",
                },
                capabilities=("assistant.use", "sessions.write"),
                achievement_links=("oc.same-clank-new-digs",),
            ),
            _lesson(
                "house-collaborate.tool-activity",
                "Inspecting tool activity and existing authority",
                "A clear picture of what the assistant may touch and what it just did.",
                "self-check",
                "settings/file-access",
                (
                    "The assistant's reach is bounded by explicit settings. "
                    "[File access](clank://settings/file-access) states what it may "
                    "read and write; tool activity in a chat shows what actually "
                    "ran. Authority is granted in advance, not invented mid-task."
                ),
                (
                    "Knowing the boundary before you delegate keeps surprises out "
                    "of your files. And when something did run, the activity record "
                    "is how you check it — without guessing from the prose reply."
                ),
                (
                    "Open [File access](clank://settings/file-access) and read what "
                    "is permitted. Then open a chat where the assistant used a tool "
                    "and expand the tool activity for that turn.\n\n"
                    "**What is distinctive:** permission is a setting you can read, "
                    "and activity is a record you can inspect. Neither is hidden in "
                    "the answer text."
                ),
                practice=None,
                achievement_links=(),
            ),
            _lesson(
                "house-collaborate.durable-goals",
                "Durable goals and evidence",
                "A goal that carries its own requirement for proof before it is called done.",
                "verified",
                "chat",
                (
                    "A durable goal is more than a to-do. It records what must be "
                    "true, and completing it requires the evidence you asked for — "
                    "not a model saying \"done.\" Goals live with the session and "
                    "survive the turn that created them."
                ),
                (
                    "This is the difference between work that looks finished and "
                    "work you can check. When the goal demands evidence, the "
                    "verification is yours to review, not a claim to trust."
                ),
                (
                    "Open [Chat](clank://chat) on a session and create a goal with a "
                    "concrete requirement — for example, \"produce a summary and "
                    "attach it as a document.\" Work toward it. When the work is "
                    "done, verify the goal; verification needs the goal's identity "
                    "and revision plus the evidence you named.\n\n"
                    "**Observable result:** the goal reaches a verified completed "
                    "state with its evidence references satisfied. A model statement "
                    "alone cannot complete it."
                ),
                practice={
                    "title": "Evidence-bearing practice goal",
                    "seed": "Create one goal whose completion requires a named document or artifact as evidence.",
                    "cleanup": "Complete or dismiss the goal normally; it is ordinary session state.",
                    "expectedEvidence": "The goal shows a verified completed state with its evidence reference recorded.",
                },
                capabilities=("assistant.use", "sessions.write"),
                achievement_links=("oc.proof-of-work",),
            ),
            _lesson(
                "house-collaborate.scheduled-work",
                "Scheduled work and answering in its original task chat",
                "A scheduled task whose result lands in the chat that owns the task.",
                "verified",
                "tasks",
                (
                    "You can put the clanker on a schedule. When a scheduled task "
                    "runs, its answer returns to the original task chat — not to "
                    "whatever conversation happens to be open. Task identity is "
                    "preserved so follow-up questions land where the work is."
                ),
                (
                    "Automation is only useful if its output is findable. A result "
                    "that answers in the task's own chat keeps the receipt, the "
                    "transcript and your follow-up in one place."
                ),
                (
                    "Open [Meatbag Tasks](clank://tasks) and create a scheduled "
                    "practice reminder. Inspect its payload and its run state. When "
                    "it runs, open its task chat and confirm the answer is there.\n\n"
                    "**Observable result:** the run produces a committed receipt and "
                    "the answer appears in that task's own chat. Creating a schedule "
                    "without a successful run does not count as work done."
                ),
                practice={
                    "title": "Practice reminder",
                    "seed": "Create a one-shot practice reminder and inspect its scheduled payload before it runs.",
                    "cleanup": "Cancel the reminder if you do not want it to run; cancellation is safe and reversible.",
                    "expectedEvidence": "The task shows its payload and either a committed run receipt or a cancelled status you chose.",
                },
                capabilities=("tasks.write",),
                achievement_links=("oc.on-the-clock",),
            ),
            _lesson(
                "house-collaborate.menmery-and-lore",
                "Menmery review and recall versus Lore recovery",
                "Two different tools, used for two different jobs.",
                "self-check",
                "memory",
                (
                    "**Menmery** is the sidebar where memory lives: candidates "
                    "waiting for your review, and search over what you kept. You "
                    "accept a candidate explicitly; nothing is written behind your "
                    "back. **Lore** is recovery: retained versions of your work and "
                    "the receipts that let you restore them."
                ),
                (
                    "Confusing the two wastes time. Menmery answers \"what does the "
                    "assistant know about me?\" Lore answers \"how do I get yesterday's "
                    "version back?\" Review is about the future; restore is about the "
                    "past."
                ),
                (
                    "Open [Menmery](clank://memory) and review a pending candidate — "
                    "accept one you actually want. Then search for a record and open "
                    "it. Separately, open a document's history and look at the "
                    "retained versions; those are Lore's material.\n\n"
                    "Retention budgets live in "
                    "[Settings → History](clank://settings/history). What is not "
                    "retained cannot be restored."
                ),
                practice=None,
                achievement_links=("oc.selective-menmery", "oc.total-recall"),
            ),
        ),
    },
    {
        "key": "house-documents",
        "title": "Documents With Teeth",
        "summary": "Documents, templates, splits, rich source comments, typed tables, Wiki pages and attached images.",
        "description": (
            "Documents in Open Clank are not a notepad. They hold template "
            "sections, linked Wiki pages, typed tables with real formulas, "
            "comment regions over programming source, and images that stay "
            "attached to the document that references them."
        ),
        "lessons": (
            _lesson(
                "house-documents.new-documents",
                "New documents and insertable template sections",
                "A document built from a template section, ready to edit.",
                "verified",
                "editor",
                (
                    "Create a document in [Editor](clank://editor). Templates are "
                    "stored in [Settings](clank://settings) and inserting one copies "
                    "its assets and sections into the document you are writing. "
                    "Inserting is a copy: editing the inserted section never rewrites "
                    "the template."
                ),
                (
                    "Templates turn the structure you reuse — meeting shape, review "
                    "checklist, research frame — into something you can drop in "
                    "without retyping. Because insertion copies, your edits stay "
                    "yours."
                ),
                (
                    "Open [Editor](clank://editor) and create a new document. Use "
                    "the template section inserter to add one supplied section. "
                    "Edit the inserted text and save.\n\n"
                    "**Observable result:** the document reopens with your edited "
                    "section text and the revision marker advanced. The template "
                    "itself is unchanged."
                ),
                practice={
                    "title": "Template-backed practice note",
                    "seed": "Create a document and insert one template section; change one line and save.",
                    "cleanup": "Delete this practice document when you are done, or keep it — it is ordinary content.",
                    "expectedEvidence": "The document reopens with the edited section and a newer revision marker.",
                },
                capabilities=("editor.write",),
                achievement_links=("oc.fresh-mould", "oc.spliced-in"),
            ),
            _lesson(
                "house-documents.tabs-and-splits",
                "Tabs, in-place links and splits",
                "Two documents open side by side, with a link that moves between them without losing your place.",
                "verified",
                "editor",
                (
                    "Editor keeps documents in tabs and can split the view into two "
                    "panes. In-place links open a document in the other pane rather "
                    "than replacing what you are reading. Unsaved drafts stay in "
                    "their buffer while you navigate."
                ),
                (
                    "Comparison is a real workflow: source and summary, brief and "
                    "draft, spec and implementation. Splits make it a layout rather "
                    "than a memory exercise."
                ),
                (
                    "Open two documents in [Editor](clank://editor). Split the view "
                    "so both are mounted at once. In one document, use an in-place "
                    "link to the other.\n\n"
                    "**Observable result:** two distinct document panes are mounted "
                    "simultaneously and the link resolves to the other document. "
                    "Clicking a split button that is not available does not count."
                ),
                practice={
                    "title": "Two-pane practice pair",
                    "seed": "Create two short documents; open both in a split and link one to the other.",
                    "cleanup": "Delete both practice documents when finished.",
                    "expectedEvidence": "Two panes are mounted at once and the in-place link opens the second document.",
                },
                capabilities=("editor.write",),
                achievement_links=("oc.two-windows",),
            ),
            _lesson(
                "house-documents.rich-markdown",
                "Rich Markdown and code comments with See source",
                "A comment or docstring over real code, rendered as Markdown and traceable to its exact source.",
                "verified",
                "editor",
                (
                    "Editor renders Markdown richly, including comment and docstring "
                    "regions inside programming source. A **See source** action on "
                    "the official formatting demo reveals the exact source lines "
                    "behind each rendered block; on ordinary documents the rendered "
                    "view and the stored source stay one document."
                ),
                (
                    "Documentation that lives with the code is documentation that "
                    "survives the refactor. Seeing the exact source behind a "
                    "rendered comment is how you edit it precisely instead of "
                    "guessing at the Markdown."
                ),
                (
                    "Open a document that contains a supported programming language "
                    "with a comment or docstring. Render it richly, then use **See "
                    "source** on the official Markdown Formatting Demo to inspect "
                    "the source lines behind a block, and return with **Back to "
                    "rendered**.\n\n"
                    "**Observable result:** a supported comment or docstring region "
                    "renders as nonempty Markdown and saves against the source "
                    "revision. The source-reveal inspector is a formatting-demo "
                    "feature; ordinary documents keep their own source as the single "
                    "record."
                ),
                practice={
                    "title": "Sample programming comments",
                    "seed": "def summarize(rows):\n    \"\"\"Return a two-line summary.\n\n    Keeps the header and the count.\n    \"\"\"\n    # Count first; sort later.\n    return len(rows)",
                    "cleanup": "Delete this practice document when finished.",
                    "expectedEvidence": "The docstring region renders as Markdown and the source revision saves.",
                },
                capabilities=("editor.write",),
                achievement_links=("oc.comments-alive", "oc.under-the-markdown"),
            ),
            _lesson(
                "house-documents.typed-tables",
                "Typed dates, money and formulas in readable tables",
                "A table whose date and currency columns compute a real total.",
                "verified",
                "editor",
                (
                    "Tables carry column types: dates, money, plain text and "
                    "formulas. A typed date or currency column sorts and formats as "
                    "that type, and a formula column evaluates against the row. "
                    "Error-valued formulas stay visible as errors — they never "
                    "quietly become zero."
                ),
                (
                    "A table that computes is a small model of your numbers. When "
                    "the total is a formula rather than a typed-in figure, it stays "
                    "true when the rows change."
                ),
                (
                    "Open a table in [Editor](clank://editor). Give one column a "
                    "date type and another a currency type. Add a formula column "
                    "that totals the currency column. Save and reopen.\n\n"
                    "**Observable result:** the saved table revision contains a "
                    "typed date or currency column and a successfully evaluated "
                    "formula or total."
                ),
                practice={
                    "title": "Typed practice table",
                    "seed": "| Date | Item | Amount |\n| --- | --- | --- |\n| 2026-09-01 | Supplies | 12.50 |\n| 2026-09-08 | Filing | 40.00 |\n\nTotal formula column: sum(Amount)",
                    "cleanup": "Delete this practice table when finished.",
                    "expectedEvidence": "The saved table revision has a typed date/currency column and an evaluated total.",
                },
                capabilities=("editor.write",),
                achievement_links=("oc.numbers-with-manners",),
            ),
            _lesson(
                "house-documents.wiki-within",
                "Wiki pages and Markdown chunks in the same Editor",
                "A Wiki-type page linked to a note, both opened and edited in one Editor.",
                "verified",
                "wiki",
                (
                    "A Wiki page is a document type, not a separate application. "
                    "Create one from the [Wiki library](clank://wiki) and it opens in "
                    "the same Editor as everything else. Wiki pages hold Markdown "
                    "chunks and link to other documents; the link is real, and the "
                    "backlink shows up on the target."
                ),
                (
                    "One editor means one set of habits: the same shortcuts, the "
                    "same links, the same search. The Wiki library is a filtered "
                    "shelf of page types — the writing model does not change."
                ),
                (
                    "Open the [Wiki library](clank://wiki) and create a Wiki-type "
                    "page. Write a short chunk and link it to another readable "
                    "document. Reopen the link, then check the backlink on the "
                    "target.\n\n"
                    "**Observable result:** the Wiki page commits its Markdown chunk "
                    "and a valid link/backlink pair with another document."
                ),
                practice={
                    "title": "Linked practice notebook page",
                    "seed": "A short Wiki chunk that links to one existing note.",
                    "cleanup": "Delete this practice page and remove its link when finished.",
                    "expectedEvidence": "The Wiki page saves its chunk and a resolving link/backlink to the note.",
                },
                capabilities=("editor.write",),
                achievement_links=("oc.wiki-within",),
            ),
            _lesson(
                "house-documents.images-attached",
                "Images and links that stay attached to their source",
                "An image inserted into a document that still resolves to its Files asset after a reload.",
                "verified",
                "files",
                (
                    "Images live in [Files](clank://files) and are inserted into "
                    "documents through the shared attachment adapter. The document "
                    "keeps a real reference to the asset: move the document and the "
                    "known references are checked, not left dangling. Gallery "
                    "assets are Files assets — there is no separate gallery to "
                    "manage."
                ),
                (
                    "Attachments that survive a reload and a move are what make a "
                    "document portable. A picture that evaporates when the folder "
                    "changes is a broken document."
                ),
                (
                    "Prepare an image in [Files](clank://files) and insert it into a "
                    "document in [Editor](clank://editor). Save, reload, and confirm "
                    "the image still resolves. Then move the document to another "
                    "folder and confirm the reference remains intact.\n\n"
                    "**Observable result:** the clipboard image attachment "
                    "preparation and the document insertion commit together, with an "
                    "owned asset in the required media location."
                ),
                practice={
                    "title": "Attached practice image",
                    "seed": "Insert one small image into a practice document and save.",
                    "cleanup": "Delete the practice document and its owned image asset when finished.",
                    "expectedEvidence": "The image resolves from its Files asset after reload and after a document move.",
                },
                capabilities=("editor.write", "files.write"),
                achievement_links=("oc.picture-this", "oc.baggage-included"),
            ),
        ),
    },
    {
        "key": "house-connections",
        "title": "Follow the Threads",
        "summary": "Links, Graph modes, filters and camera, Bases, source-backed tasks and Timeline, and attributable research.",
        "description": (
            "The value of a document grows with what it is connected to. These "
            "lessons cover link navigation, the Graph's two document modes, "
            "programmatic filters, Bases over typed content, tasks and timeline "
            "entries that point back at their source, and research notes that "
            "keep their sources."
        ),
        "lessons": (
            _lesson(
                "house-connections.links",
                "Link and backlink navigation",
                "A link followed to its target and a backlink followed home again.",
                "verified",
                "editor",
                (
                    "Documents link to each other with ordinary references. "
                    "Following a link resolves the edge to a real document; the "
                    "target shows the backlink. Graph treats the same edges as "
                    "structure you can see."
                ),
                (
                    "Backlinks are how a note becomes a network. The link you leave "
                    "in one document is the trail someone else — or future you — "
                    "follows back."
                ),
                (
                    "In [Editor](clank://editor), create a link from one document to "
                    "a different document. Follow it. On the target, open the "
                    "backlink and follow it home.\n\n"
                    "**Observable result:** the link and its navigation resolve to a "
                    "different document, and the source and target identities match "
                    "the edge."
                ),
                practice={
                    "title": "Practice link pair",
                    "seed": "Document A links to Document B; follow the edge both ways.",
                    "cleanup": "Delete both practice documents and the link when finished.",
                    "expectedEvidence": "Navigation resolves the edge to the other document with matching identities.",
                },
                capabilities=("editor.write",),
                achievement_links=("oc.thread-puller",),
            ),
            _lesson(
                "house-connections.graph-modes",
                "Graph: linked-document mode versus structure mode",
                "Both Graph modes mounted on real content, each showing at least one actual node.",
                "verified",
                "graph",
                (
                    "[Graph](clank://graph) has two document modes. **Documents · "
                    "links** draws the edges between documents. **Structure · "
                    "headings and bullets** draws the shape inside them. Galaxy is "
                    "an optional third projection for tracks and events — a related "
                    "destination, not a replacement for either. (An older name, "
                    "Mind, referred to structure mode; it is not a separate "
                    "launcher.)"
                ),
                (
                    "Links tell you what relates; structure tells you what a "
                    "document is made of. Switching between them is how you find a "
                    "missing connection and then find the section that should hold "
                    "it."
                ),
                (
                    "Open [Graph](clank://graph) on a corpus with at least two "
                    "linked documents. View **Documents · links**, then switch to "
                    "**Structure · headings and bullets**.\n\n"
                    "**Observable result:** both modes show at least one actual "
                    "node. Selecting two empty modes is not enough."
                ),
                practice={
                    "title": "Graph practice corpus",
                    "seed": "Two linked documents with one heading each, then view both Graph modes.",
                    "cleanup": "Delete the practice documents when finished.",
                    "expectedEvidence": "Both Graph modes render at least one real node from the practice corpus.",
                },
                capabilities=("editor.write",),
                achievement_links=("oc.two-maps",),
            ),
            _lesson(
                "house-connections.filters-camera",
                "Programmatic filters and camera controls in Graph",
                "A filtered Graph view and a camera you actually moved.",
                "verified",
                "graph",
                (
                    "Graph filters are programmatic facets — folder, tag and "
                    "property values generated from the current corpus — not free "
                    "text guesses. Camera controls let you zoom and pan a populated "
                    "view; the controls are ordinary gestures with real effect."
                ),
                (
                    "Filters are how a large house stays navigable. Camera control "
                    "is how you read one neighbourhood of the graph closely instead "
                    "of squinting at all of it."
                ),
                (
                    "In a populated [Graph](clank://graph) view, apply a filter that "
                    "names a facet and value from the generated facet list, and "
                    "confirm the result set changes. Then use the camera gestures "
                    "to zoom and pan.\n\n"
                    "**Observable result:** the filter changes the result set, and "
                    "accepted camera gestures change scale by at least 20% and pan "
                    "at least 48 CSS pixels from the starting camera."
                ),
                practice={
                    "title": "Facet practice filter",
                    "seed": "Apply one generated facet/value filter to a populated Graph view.",
                    "cleanup": "Clear the filter when finished; nothing is deleted.",
                    "expectedEvidence": "The result set changes and the camera moves a real distance.",
                },
                achievement_links=("oc.choose-your-threads", "oc.room-to-roam"),
            ),
            _lesson(
                "house-connections.bases",
                "Bases over typed content",
                "A Base that queries typed values and returns the rows that match.",
                "verified",
                "editor",
                (
                    "Bases are live queries over typed document content. Open a "
                    "Base in [Editor](clank://editor) and it evaluates a filter "
                    "subset against your documents' properties — status values, "
                    "topics, dates — and returns matching rows as they change."
                ),
                (
                    "A Base is a view you did not have to maintain. When the "
                    "property is typed, the query is exact; when the row changes, "
                    "the view is already current."
                ),
                (
                    "Open a Base in [Editor](clank://editor). Add a typed value to a "
                    "practice row and run the view.\n\n"
                    "**Observable result:** the typed value is returned by the Base "
                    "query. Text that merely looks like a status is not a typed "
                    "value."
                ),
                practice={
                    "title": "Practice Base row",
                    "seed": "status: ready\ntopic: field-guide",
                    "cleanup": "Delete the practice row or document when finished.",
                    "expectedEvidence": "The Base query returns the row on its typed status value.",
                },
                capabilities=("editor.write",),
                achievement_links=(),
            ),
            _lesson(
                "house-connections.tasks-timeline",
                "Source-backed Tasks and Timeline entries",
                "A task that checks a real checkbox in its source document, and a Timeline event you moved.",
                "verified",
                "tasks",
                (
                    "A task can point at a source document. Completing it changes "
                    "the referenced checkbox in that document from unchecked to "
                    "checked — the note and the task stay one piece of work. "
                    "[Timeline](clank://timeline) holds dated events on tracks; an "
                    "event can be moved to a different valid date or track and keeps "
                    "its identity."
                ),
                (
                    "Work that points back at its source is work you can audit. And "
                    "a timeline you can reshape is how a plan survives contact with "
                    "reality without losing its history."
                ),
                (
                    "In a document, add a checkbox task and open it in "
                    "[Meatbag Tasks](clank://tasks). Complete the task and reopen "
                    "the source document. Separately, create a "
                    "[Timeline](clank://timeline) event on a practice track and move "
                    "it to a different date.\n\n"
                    "**Observable result:** the source checkbox changes from "
                    "unchecked to checked, and the Timeline event commits and then "
                    "moves while keeping the same stable event identity."
                ),
                practice={
                    "title": "Source task and timeline entry",
                    "seed": "One checkbox task in a practice note, and one single-day event on a practice track.",
                    "cleanup": "Delete the practice note and its Timeline event when finished.",
                    "expectedEvidence": "The source checkbox is checked by the task, and the event moves to a new date/track.",
                },
                capabilities=("tasks.write", "editor.write", "timeline.write"),
                achievement_links=("oc.checked-at-source", "oc.time-shaper"),
            ),
            _lesson(
                "house-connections.research-compare",
                "Research notes and Compare results with attributable sources",
                "A research note whose claims can be traced to sources, and a Compare result that records why.",
                "self-check",
                "chat",
                (
                    "Research work keeps its sources. A research note records what "
                    "came from where, so a claim can be checked. Compare sets two "
                    "recorded responses side by side and records which you chose and "
                    "why. Both are ordinary content you can open again."
                ),
                (
                    "Attribution is what makes research reusable. A conclusion "
                    "without a source is an opinion; a Compare without a recorded "
                    "reason is a coin flip you cannot revisit."
                ),
                (
                    "Open a research note and check that its claims carry source "
                    "references. Open a Compare result and read the recorded "
                    "selection and reason.\n\n"
                    "**What is distinctive:** sources stay attached to the claim, "
                    "and a comparison leaves a record rather than a vibe. Live "
                    "inference is optional — recorded fixture responses are enough "
                    "to learn the workflow, and reading this lesson never starts a "
                    "paid call."
                ),
                practice=None,
                capabilities=("research.read", "compare.read"),
                achievement_links=(),
            ),
        ),
    },
    {
        "key": "house-making",
        "title": "Make, Move, Recover",
        "summary": "Files and workspaces, clipboard media, Imps editing, project export, Lore restore and scoped exports.",
        "description": (
            "Making things in Open Clank means files you can move, images you "
            "can edit without losing the original, exports you can hand to "
            "someone else, and recovery that actually restores. These lessons "
            "are the workshop."
        ),
        "lessons": (
            _lesson(
                "house-making.files-workspaces",
                "Files and registered versus loose workspaces",
                "A file opened from Files that resolves to the same resource Editor sees.",
                "self-check",
                "files",
                (
                    "[Files](clank://files) is the file tree. A **registered** "
                    "workspace is one the installation knows about and can manage; "
                    "a **loose** workspace is ordinary material you pointed at. Both "
                    "are readable, and Files and Editor share one resource space — "
                    "the same file is the same file."
                ),
                (
                    "Knowing which kind of workspace you are in tells you what the "
                    "assistant may do and what recovery can promise. One resource "
                    "space means no export step just to edit what you can already "
                    "see."
                ),
                (
                    "Open [Files](clank://files) and browse. Open the same path from "
                    "[Editor](clank://editor) and confirm it is the same resource.\n\n"
                    "**What is distinctive:** Files and Editor share one resource "
                    "identity. A path is not a copy — it is the same material seen "
                    "from two places."
                ),
                practice=None,
                achievement_links=(),
            ),
            _lesson(
                "house-making.clipboard-media",
                "Clipboard media layout and protected adoption",
                "A pasted image that lands in the right place without overwriting anything.",
                "self-check",
                "files",
                (
                    "Pasting media follows a chosen clipboard layout: the asset "
                    "lands where the layout says, with an owned name. Adoption of "
                    "existing material is **protected** — the system will not "
                    "silently overwrite a file you already have. Collisions surface "
                    "as a choice, not a surprise."
                ),
                (
                    "Media that lands predictably is media you can find later. "
                    "Protected adoption is why a paste can be trusted near work you "
                    "care about."
                ),
                (
                    "Paste an image into a document and inspect where the asset "
                    "landed in [Files](clank://files). Paste a second image with the "
                    "same name and observe the collision handling.\n\n"
                    "**What is distinctive:** the layout is explicit and adoption is "
                    "protected. Nothing you already own is replaced without you."
                ),
                practice=None,
                achievement_links=(),
            ),
            _lesson(
                "house-making.imps-layers",
                "Imps layers, masks and text — and original-save recovery",
                "An image project with two editable layers, saved and reopened intact.",
                "verified",
                "files",
                (
                    "**Imps** is the image editor. A project keeps layers, masks and "
                    "text as editable structure — not a flattened blob. Saving a "
                    "managed edit captures a preimage first, so the original is "
                    "recoverable from the project's history. Gallery assets are "
                    "Files assets; Imps is where you edit them."
                ),
                (
                    "Editable layers are how an image stays useful after the first "
                    "change. The preimage is why you can experiment: the original is "
                    "not gone, it is retained."
                ),
                (
                    "Open an image from [Files](clank://files) in Imps. Add a second "
                    "editable layer. Save the project and reopen it.\n\n"
                    "**Observable result:** the project reopens with the same project "
                    "revision and the same layer identities. To undo the save, "
                    "restore from the project's history and the preimage replays."
                ),
                practice={
                    "title": "Small editable practice image",
                    "seed": "One image project with two layers: base plus one text or mask layer.",
                    "cleanup": "Delete the practice project and its assets from Files when finished.",
                    "expectedEvidence": "Reopening shows the same project revision and layer identities.",
                },
                capabilities=("files.write",),
                achievement_links=("oc.impish",),
            ),
            _lesson(
                "house-making.project-export",
                "Editable project export",
                "An export artifact with a valid project manifest and asset set.",
                "verified",
                "files",
                (
                    "Exporting an Imps project produces an editable artifact: the "
                    "project structure plus its assets, with a manifest describing "
                    "them. Clicking Export is not the result — a committed export "
                    "with a valid manifest is."
                ),
                (
                    "An editable export is what makes work portable and "
                    "reviewable. Someone else can open it and see the layers, not "
                    "just a picture of them."
                ),
                (
                    "Export the practice Imps project. Inspect the artifact.\n\n"
                    "**Observable result:** the export confirms an editable artifact "
                    "with a valid project manifest and a complete asset set. A bare "
                    "Export click without a committed artifact does not count."
                ),
                practice={
                    "title": "Practice project export",
                    "seed": "Export the two-layer practice project and inspect its manifest.",
                    "cleanup": "Delete the export artifact when finished if you do not need it.",
                    "expectedEvidence": "The export artifact contains a valid project manifest and its asset set.",
                },
                capabilities=("files.write",),
                achievement_links=("oc.take-the-tools",),
            ),
            _lesson(
                "house-making.lore-restore",
                "A Lore restore on a practice artifact",
                "A document version restored from retained history, confirmed by content hash.",
                "verified",
                "editor",
                (
                    "Lore retains versions of your work with the receipts needed to "
                    "restore them. A restore commits a real change: the retained "
                    "version's content is written back, and the result is confirmed "
                    "against that version's hash. The restore is itself recorded, so "
                    "you can go forward again."
                ),
                (
                    "Recovery you can verify is recovery you can trust. The hash "
                    "check is the difference between \"it seems fine\" and \"this is "
                    "the version I chose.\""
                ),
                (
                    "Make a change to a practice document and record a checkpoint. "
                    "Change it again. From the document's history, restore the "
                    "retained version.\n\n"
                    "**Observable result:** the restore commits and the resulting "
                    "content hash matches the retained version. A digest-only or "
                    "observed-after claim is not a restore receipt."
                ),
                practice={
                    "title": "Recoverable practice version",
                    "seed": "Edit a practice document, checkpoint it, edit again, then restore the checkpoint.",
                    "cleanup": "Delete the practice document when finished; its history goes with it.",
                    "expectedEvidence": "The restore commits and the content hash matches the retained version.",
                },
                capabilities=("editor.write",),
                achievement_links=("oc.return-of-the-byte",),
            ),
            _lesson(
                "house-making.scoped-exports",
                "Scoped exports and checking their manifests",
                "An export limited to what you chose, with a manifest that proves it.",
                "verified",
                "settings",
                (
                    "A scoped export packages the material you selected and writes "
                    "a manifest describing exactly what is inside. Reading the "
                    "manifest is how you confirm the export is complete and nothing "
                    "extra rode along. Filename existence is not proof."
                ),
                (
                    "Exports are how work leaves the house. A manifest is the "
                    "packing list — check it and you know what the recipient "
                    "received."
                ),
                (
                    "Export the disposable practice material as a scoped export. "
                    "Open its manifest and check the contents against what you "
                    "selected.\n\n"
                    "**Observable result:** the export finishes and its manifest "
                    "validation succeeds, attributed to you. A filename on disk "
                    "alone does not qualify."
                ),
                practice={
                    "title": "Scoped practice export",
                    "seed": "Export only the disposable practice project and read its manifest.",
                    "cleanup": "Delete the export artifact when finished.",
                    "expectedEvidence": "The export completes and its manifest validates against the selected scope.",
                },
                capabilities=("export.write",),
                achievement_links=("oc.packed-and-checked",),
            ),
        ),
    },
    {
        "key": "house-stewardship",
        "title": "Make the House Yours",
        "summary": "Official docs, theme and accessibility, app links, local drafts, authoring a Class, and reading your progress.",
        "description": (
            "The house works better when it fits you. These lessons cover the "
            "maintained handbook, appearance and accessibility controls that "
            "live in Settings, app links that take you where the text points, "
            "local drafts for email and calendar, making a Class to teach "
            "someone else, and reading your own progress honestly."
        ),
        "lessons": (
            _lesson(
                "house-stewardship.official-docs",
                "Official docs and the formatting demo",
                "A handbook page opened in Editor, and the formatting demo's exact source view.",
                "verified",
                "editor",
                (
                    "The maintained handbook ships with Open Clank and opens inside "
                    "[Editor](clank://editor) — there is no separate help browser. "
                    "Official pages are read-only; **Make editable copy** keeps your "
                    "own notes beside them and updates never touch your copy. The "
                    "Markdown Formatting Demo shows rendered Markdown and its exact "
                    "source."
                ),
                (
                    "Documentation that opens where you work is documentation you "
                    "will actually read. And a formatting demo with real source "
                    "reveal teaches Markdown by showing the bytes, not by describing "
                    "them."
                ),
                (
                    "Open the handbook from Help and read one non-demo page — for "
                    "example the Open Clank Handbook home — in "
                    "[Editor](clank://editor). Then open the Markdown Formatting "
                    "Demo and switch between its rendered view and exact source "
                    "view.\n\n"
                    "**Observable result:** an official non-demo documentation page "
                    "opens by its provisioned identity (a personal folder with a "
                    "similar name does not qualify), and the formatting demo switches "
                    "between rendered and source views."
                ),
                practice=None,
                achievement_links=("oc.read-the-house", "oc.under-the-markdown"),
            ),
            _lesson(
                "house-stewardship.theme-effects",
                "Theme, effect and accessibility controls in Settings",
                "A theme and background effect you chose, rehydrating after a reload.",
                "verified",
                "settings/appearance",
                (
                    "Appearance lives in "
                    "[Settings → Appearance](clank://settings/appearance) — there is "
                    "no separate theme applet. Themes change colours across the "
                    "shell; the picker shows live previews. **Background / Effect** "
                    "chooses a decorative effect — Solid, Clanker Signal Routes, "
                    "Shipibo Kene-Inspired Signal Weave, Clanker LCARS, Clanker Gem "
                    "Drift, Clanker Emoji Drift, Clanker Matrix Rain, Clanker Emoji "
                    "Rain, Clanker LCARS Status Sweep, Dots, Synapse, Rain, "
                    "Constellations, Perlin Flow, Petals, Sparkles or Embers — with "
                    "effect colour, intensity and size where the effect supports "
                    "them. Typography offers font and density (comfortable, compact, "
                    "spacious) plus a UI text-size scale. Frosted glass is a toggle."
                ),
                (
                    "Appearance is not decoration for its own sake — it is how the "
                    "house stays readable for your eyes. Effects are optional and "
                    "heavy ones can be turned off without losing the rest of the "
                    "theme."
                ),
                (
                    "Open [Settings → Appearance](clank://settings/appearance). "
                    "Choose a theme and a background effect. Adjust intensity or "
                    "size if the effect supports them, and set the UI text-size "
                    "scale where you need it. Reload.\n\n"
                    "**Observable result:** your owner-scoped theme preference is "
                    "confirmed and the same values rehydrate after a fresh settings "
                    "load."
                ),
                practice={
                    "title": "Appearance preference",
                    "seed": "Pick a theme and one background effect; note the intensity and size.",
                    "cleanup": "Change the values back if you prefer; nothing destructive is involved.",
                    "expectedEvidence": "The same theme and effect values rehydrate after a new settings load.",
                },
                achievement_links=("oc.house-colours",),
            ),
            _lesson(
                "house-stewardship.app-links",
                "App links across Editor and TreeHouse",
                "Two shared app links that open their destinations without disturbing your work.",
                "verified",
                "treehouse",
                (
                    "Rich content can link to app screens with `clank://` "
                    "destinations. A link in [Editor](clank://editor) and a link in "
                    "TreeHouse use the same resolver: the destination opens and "
                    "focuses its screen, chat identity is preserved, and unsaved "
                    "drafts stay in their buffers. A link never toggles an "
                    "already-open window closed and never starts an unrelated chat."
                ),
                (
                    "Instructions that take you to the screen they describe are "
                    "instructions you can follow. One resolver means one behaviour: "
                    "the link in a lesson and the link in a note work the same way."
                ),
                (
                    "Follow one shared app link from content in "
                    "[Editor](clank://editor) and one from TreeHouse content — for "
                    "example [Settings](clank://settings) and "
                    "[Graph](clank://graph). Confirm each resolves to an existing "
                    "app destination.\n\n"
                    "**Observable result:** two successful shared app-link "
                    "resolutions, one from Editor content and one from TreeHouse "
                    "content, each landing on a real destination."
                ),
                practice={
                    "title": "App-link practice pair",
                    "seed": "Place one clank:// link in a practice document and follow one in this lesson.",
                    "cleanup": "Delete the practice document when finished.",
                    "expectedEvidence": "Both app links resolve to existing destinations without disturbing open drafts.",
                },
                achievement_links=("oc.shortcuts-grow",),
            ),
            _lesson(
                "house-stewardship.email-calendar",
                "Local email and calendar drafts, and webhook status",
                "A local draft and a calendar placeholder that never leave the machine until you send.",
                "self-check",
                "settings/integrations",
                (
                    "Email and calendar examples in the Field Guide use **local "
                    "drafts**. Nothing is sent externally by opening a lesson or "
                    "writing a draft. Webhooks are an integration: their "
                    "configuration and delivery status are inspectable in "
                    "[Settings → Integrations](clank://settings/integrations), and "
                    "an unconfigured webhook is visibly disabled rather than "
                    "silently broken."
                ),
                (
                    "Drafting locally is how you practise safely. And knowing "
                    "whether a webhook is actually configured is how you avoid "
                    "believing an alert went out when nothing did."
                ),
                (
                    "Write a local email draft and create a matching calendar "
                    "placeholder. Then open "
                    "[Settings → Integrations](clank://settings/integrations) and "
                    "inspect webhook status.\n\n"
                    "**What is distinctive:** drafts and placeholders stay local and "
                    "inspectable; webhook status is visible before you rely on it. "
                    "Opening this lesson never sends anything externally."
                ),
                practice={
                    "title": "Local communication draft",
                    "seed": "One local email draft and one calendar placeholder; no external send.",
                    "cleanup": "Delete the draft and placeholder when finished.",
                    "expectedEvidence": "The draft and placeholder remain local and inspectable.",
                },
                achievement_links=(),
            ),
            _lesson(
                "house-stewardship.teach-a-class",
                "Making and sharing a Class",
                "A Class of your own, previewed as a learner and then published.",
                "verified",
                "treehouse",
                (
                    "You can author a Class in TreeHouse. Write at least one "
                    "nonempty lesson, preview it as a learner to see what they will "
                    "see, then publish. Sharing gives another account access; "
                    "prerequisites and assessment are features of authored Classes, "
                    "even though the built-in Field Guide itself is free exploration."
                ),
                (
                    "Teaching is how you hand over what you learned. The learner "
                    "preview is the check that the lesson reads the way you meant it "
                    "to — before anyone depends on it."
                ),
                (
                    "In [TreeHouse](clank://treehouse), create a Class with at least "
                    "one nonempty lesson. Preview it as a learner, then publish it. "
                    "Optionally share it with another account.\n\n"
                    "**Observable result:** your authored Class is previewed as a "
                    "learner and then explicitly published with at least one "
                    "nonempty lesson. Seeded official Classes are excluded — this is "
                    "your own work."
                ),
                practice={
                    "title": "Practice Class draft",
                    "seed": "One authored Class with a single nonempty lesson.",
                    "cleanup": "Delete or archive the practice Class when finished.",
                    "expectedEvidence": "The authored Class previews as learner and publishes with its nonempty lesson.",
                },
                capabilities=("treehouse.author",),
                achievement_links=("oc.trail-maker",),
            ),
            _lesson(
                "house-stewardship.progress-secrets",
                "Reading progress, mystery achievements and alpha spoilers",
                "An honest reading of what you have completed and what is still hidden.",
                "self-check",
                "treehouse",
                (
                    "Progress in the built-in Field Guide is free exploration: any "
                    "lesson, any order. A lesson marked **self-check** is reading "
                    "plus your own check; a lesson marked **verified** has an "
                    "observable persisted result. Completing a lesson is not "
                    "automatic evidence for an unrelated action award. In the "
                    "achievements view, mystery entries show `???` until earned and "
                    "count toward the normal total from the beginning; ultra rares "
                    "stay absent until earned and appear as a separate `+U` "
                    "suffix. Alpha admin/edit mode may reveal spoilers."
                ),
                (
                    "Honest labelling is what keeps achievements meaningful. A "
                    "self-check is a self-check; a verified practice is something "
                    "that actually happened. And the secret rules are there so a "
                    "surprise stays surprising until you earn it."
                ),
                (
                    "Open [TreeHouse](clank://treehouse) and read the achievements "
                    "view. Note which of your completions were self-checks and which "
                    "were verified. Mystery entries show `???`; unearned ultra rares "
                    "are not listed at all. In alpha admin/edit mode, spoilers are "
                    "visible.\n\n"
                    "Your account-wide achievements are lifetime records: resetting "
                    "Class progress does not erase them, and changing workspace does "
                    "not re-award them."
                ),
                practice=None,
                achievement_links=(),
            ),
        ),
    },
)


def _achievement_hints(achievement_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    """Resolve lesson achievement links to secrecy-aware render hints.

    Learner mode must not name an unearned ultra rare and must show mystery
    entries as `???` until earned.  Ultra rares are omitted from the hint list
    entirely so the renderer never has to draw an empty secret container.
    A hint is presentation only: completing a lesson is never evidence for an
    unrelated action award.
    """
    hints: list[dict[str, Any]] = []
    for achievement_id in achievement_ids:
        rarity = ACHIEVEMENT_RARITY.get(achievement_id)
        if rarity is None:
            continue
        if rarity == "ultra":
            # Never hinted from a lesson.  Earning one reveals it in the
            # achievements view, not through a lesson spoiler.
            continue
        hints.append({
            "id": achievement_id,
            "rarity": rarity,
            "secret": rarity == "mystery",
        })
    return hints


def field_guide_manifest() -> dict[str, Any]:
    """Return an immutable-by-convention publication manifest."""
    courses = []
    lessons = []
    for position, spec in enumerate(_CLASSES):
        class_key = spec["key"]
        lesson_specs = spec["lessons"]
        course_id_suffix = class_key
        course_lessons = []
        for lesson in lesson_specs:
            destination_key = lesson["destination"]
            destination = APP_DESTINATIONS[destination_key]
            entry = {
                "key": lesson["key"],
                "title": lesson["title"],
                "classKey": class_key,
                "result": lesson["result"],
                "completion": lesson["completion"],
                "explanation": lesson["explanation"],
                "whyThisHelps": lesson["whyThisHelps"],
                "body": lesson["body"],
                "destination": destination_key,
                "surface": {
                    "key": destination_key,
                    "label": destination["label"],
                    "href": destination["href"],
                    "locator": destination["locator"],
                    "appLink": f"clank://{destination_key}",
                },
                "practice": copy.deepcopy(lesson["practice"]) if lesson["practice"] else None,
                "practiceFixture": f"field-guide/{lesson['key']}" if lesson["practice"] else None,
                "capabilityRequirements": list(lesson["capabilityRequirements"]),
                "achievementLinks": list(lesson["achievementLinks"]),
                "achievementHints": _achievement_hints(lesson["achievementLinks"]),
                "suggestedPosition": len(course_lessons) + 1,
            }
            course_lessons.append(entry)
            lessons.append(entry)
        courses.append({
            "key": class_key,
            "title": spec["title"],
            "summary": spec["summary"],
            "description": spec["description"],
            "suggestedOrder": position + 1,
            "freeExploration": True,
            "prerequisites": [],
            "lessons": course_lessons,
        })
    return {
        "templateKey": FIELD_GUIDE_TEMPLATE_KEY,
        "templateVersion": FIELD_GUIDE_TEMPLATE_VERSION,
        "title": "Open Clank Field Guide",
        "practiceProject": "Field Guide Practice",
        "audience": "Assumes basic ChatGPT familiarity; teaches what is distinctive about Open Clank.",
        "courses": courses,
        "lessons": lessons,
        "sections": copy.deepcopy(SECTION_SPECS),
        "suggestedOrder": list(SUGGESTED_ORDER),
        "legacyLessonMap": dict(LEGACY_LESSON_MAP),
        "legacyTemplateVersions": list(LEGACY_TEMPLATE_VERSIONS),
        "classKeys": list(CLASS_KEYS),
        "lessonKeys": [lesson["key"] for lesson in lessons],
        "coverage": {
            lesson["key"]: {
                "classKey": lesson["classKey"],
                "completion": lesson["completion"],
                "destination": lesson["destination"],
                "hasPractice": bool(lesson["practice"]),
            }
            for lesson in lessons
        },
        "coverageContract": {
            "version": FIELD_GUIDE_COVERAGE_VERSION,
            "kind": "disposable-mounted",
            "classCount": len(courses),
            "lessonCount": len(lessons),
            "browserCommand": "node tests/treehouse_field_guide_browser_acceptance.mjs",
            "scope": "owner-account-and-workspace",
            "assertions": [
                "published-class-count",
                "unique-lesson-keys",
                "destination-app-links",
                "no-built-in-prerequisite-locks",
                "practice-seed",
                "cleanup-boundary",
            ],
        },
    }


def validate_field_guide_manifest(manifest: dict[str, Any] | None = None) -> None:
    """Reject publication metadata that cannot drive a disposable mounted exercise.

    Deliberately updated from the one-lesson-only rule: the S30 catalogue is
    five Classes of six lessons each.  Built-in Classes carry no prerequisite
    locks; suggested order is metadata only.
    """
    value = manifest or field_guide_manifest()
    expected_classes = set(CLASS_KEYS)
    courses = value.get("courses") or []
    if len(courses) != len(expected_classes):
        raise ValueError(f"Field Guide manifest must publish {len(expected_classes)} classes")
    if {str(course.get("key") or "") for course in courses} != expected_classes:
        raise ValueError("Field Guide manifest class keys do not match the canonical catalogue")

    contract = value.get("coverageContract") or {}
    if contract.get("version") != FIELD_GUIDE_COVERAGE_VERSION or contract.get("kind") != "disposable-mounted":
        raise ValueError("Field Guide manifest has no current disposable-mounted coverage contract")
    if contract.get("classCount") != len(expected_classes):
        raise ValueError("Field Guide coverage contract has the wrong class count")

    lesson_ids: set[str] = set()
    total = 0
    for course in courses:
        key = str(course.get("key") or "")
        if course.get("freeExploration") is not True:
            raise ValueError(f"Field Guide class {key} must declare freeExploration")
        if course.get("prerequisites"):
            raise ValueError(f"Built-in Field Guide class {key} must not carry prerequisite locks")
        lessons = course.get("lessons") or []
        if len(lessons) != 6:
            raise ValueError(f"Field Guide class {key} must publish exactly six lessons, got {len(lessons)}")
        for lesson in lessons:
            lesson_key = str(lesson.get("key") or "")
            if not lesson_key or lesson_key in lesson_ids:
                raise ValueError(f"Field Guide lesson key is missing or duplicated: {lesson_key}")
            lesson_ids.add(lesson_key)
            total += 1
            if lesson.get("completion") not in {"self-check", "verified"}:
                raise ValueError(f"Field Guide lesson {lesson_key} has no honest completion kind")
            surface = lesson.get("surface") or {}
            app_link = str(surface.get("appLink") or "")
            if not app_link.startswith("clank://"):
                raise ValueError(f"Field Guide lesson {lesson_key} has no shared app-link destination")
            destination = str(lesson.get("destination") or "")
            if destination not in APP_DESTINATIONS:
                raise ValueError(f"Field Guide lesson {lesson_key} has an unregistered destination: {destination}")
            if app_link != f"clank://{destination}":
                raise ValueError(f"Field Guide lesson {lesson_key} surface/appLink disagree")
            if not lesson.get("body") or not lesson.get("explanation") or not lesson.get("whyThisHelps") or not lesson.get("result"):
                raise ValueError(f"Field Guide lesson {lesson_key} is missing learner content")
            legacy_route = "/" + "copal" + "/"
            if legacy_route in str(lesson.get("body") or "") or legacy_route in str(lesson.get("explanation") or ""):
                raise ValueError(f"Field Guide lesson {lesson_key} teaches a legacy /copal/* route")
            practice = lesson.get("practice")
            if practice is not None:
                if not practice.get("title") or not practice.get("seed") or not practice.get("cleanup") or not practice.get("expectedEvidence"):
                    raise ValueError(f"Field Guide lesson {lesson_key} has an incomplete practice fixture")
    if total != 30:
        raise ValueError(f"Field Guide manifest must publish 30 lessons, got {total}")
    if contract.get("lessonCount") != total:
        raise ValueError("Field Guide coverage contract has the wrong lesson count")
    declared = set(str(item) for item in (value.get("lessonKeys") or []))
    if declared != lesson_ids:
        raise ValueError("Field Guide manifest lessonKeys do not match the published lessons")
    sections = value.get("sections") or []
    if not any(section.get("hidden") and section.get("system") for section in sections):
        raise ValueError("Field Guide manifest must keep a hidden system section")


def _lesson_fingerprint(lesson: dict[str, Any]) -> str:
    payload = json.dumps({
        "title": lesson.get("title"),
        "content": lesson.get("content"),
        "steps": lesson.get("steps"),
        "practice": lesson.get("practice"),
        "destination": lesson.get("destination"),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _install_courses(result: dict[str, Any], owner_id: str, manifest: dict[str, Any], *, version_suffix: str) -> dict[str, Any]:
    """Build the published Class/lesson graph through the domain state machine."""
    from src.openclank.copal_treehouse import apply_treehouse_command

    owner_suffix = hashlib.sha256(str(owner_id).encode("utf-8")).hexdigest()[:12]

    profile = result.setdefault("profiles", {}).get(owner_id)
    if profile is None:
        source = result["profiles"].get("owner", {})
        result["profiles"][owner_id] = {
            "id": owner_id,
            "displayName": owner_id,
            "roles": ["admin", "instructor", "learner"],
            "active": True,
            "createdAt": source.get("createdAt"),
        }

    def run(kind: str, payload: dict[str, Any], suffix: str) -> None:
        nonlocal result
        result, _, _ = apply_treehouse_command(
            result,
            {"type": kind, "payload": payload},
            actor_id=owner_id,
            command_id=f"field-guide:{version_suffix}:{suffix}",
            expected_revision=result["revision"],
        )

    for course_spec in manifest["courses"]:
        course_key = course_spec["key"]
        course_id = f"course:{course_key}:{owner_suffix}"
        module_id = f"module:{course_key}:{owner_suffix}"
        run("course.create", {
            "id": course_id,
            "title": course_spec["title"],
            "description": course_spec["summary"],
            "tags": ["field-guide", course_key],
        }, f"course:{course_key}")
        result["courses"][course_id].update({
            "fieldGuideKey": course_key,
            "freeExploration": True,
            "suggestedOrder": course_spec["suggestedOrder"],
            "sectionKey": "house-explore",
        })
        # Built-in exploration never locks.  Prerequisites stay empty; the
        # domain still supports them for user-authored Classes.
        result["courses"][course_id]["prerequisites"] = []
        run("module.create", {
            "id": module_id,
            "courseId": course_id,
            "title": "Lessons",
            "description": course_spec["summary"],
        }, f"module:{course_key}")
        for lesson in course_spec["lessons"]:
            activity_id = f"activity:{lesson['key']}:{owner_suffix}"
            body = (
                f"{lesson['explanation']}\n\n"
                f"{lesson['body']}\n\n"
                f"**Why this helps.** {lesson['whyThisHelps']}\n\n"
                f"**Observable result.** {lesson['result']}"
            )
            run("activity.create", {
                "id": activity_id,
                "moduleId": module_id,
                "title": lesson["title"],
                "activityType": "lesson",
                "content": body,
                "points": 10,
                "skillIds": [],
            }, f"lesson:{lesson['key']}")
            run("activity.update", {"activityId": activity_id, "status": "published"}, f"publish-lesson:{lesson['key']}")
            result["activities"][activity_id].update({
                "fieldGuideKey": lesson["key"],
                "classKey": lesson["classKey"],
                "destination": lesson["destination"],
                "surface": copy.deepcopy(lesson["surface"]),
                "practice": copy.deepcopy(lesson["practice"]) if lesson["practice"] else {},
                "practiceFixture": lesson["practiceFixture"],
                "result": lesson["result"],
                "completion": lesson["completion"],
                "selfCheck": lesson["completion"] == "self-check",
                "capabilityRequirements": list(lesson["capabilityRequirements"]),
                "achievementLinks": list(lesson["achievementLinks"]),
                "achievementHints": copy.deepcopy(lesson.get("achievementHints") or []),
                "suggestedPosition": lesson["suggestedPosition"],
                "freeExploration": True,
            })
        run("course.publish", {"courseId": course_id}, f"publish-course:{course_key}")
    return result


def instantiate_field_guide(state: dict[str, Any], owner_id: str) -> dict[str, Any]:
    """Install or upgrade the template into an owner state.

    The previous same-key early return froze installed content forever.  This
    entry point now installs on first use and upgrades older published
    revisions in place, preserving edited/user-authored lessons and drafts and
    recording the change for recovery.  Learner progress is never reset and
    old self-check completions never become new feats.
    """
    result = copy.deepcopy(state)
    manifest = field_guide_manifest()
    validate_field_guide_manifest(manifest)

    installed = result.get("fieldGuide") or {}
    if installed.get("templateKey") == FIELD_GUIDE_TEMPLATE_KEY and installed.get("templateVersion") == FIELD_GUIDE_TEMPLATE_VERSION:
        return result

    owner_suffix = hashlib.sha256(str(owner_id).encode("utf-8")).hexdigest()[:12]
    upgrade_report: dict[str, Any] = {
        "from": installed.get("templateVersion") or None,
        "to": FIELD_GUIDE_TEMPLATE_VERSION,
        "preservedEdited": [],
        "replacedUnmodified": [],
        "legacyProgress": [],
        "legacyIdMap": dict(LEGACY_LESSON_MAP),
    }

    if installed.get("templateKey") == FIELD_GUIDE_TEMPLATE_KEY and installed.get("templateVersion") in LEGACY_TEMPLATE_VERSIONS:
        # Versioned upgrade from the 17-Class chain.  Template-owned (unmodified)
        # lessons are replaced; anything the learner or an author edited is kept
        # as user content and is never overwritten.
        for activity_id, activity in list(result.get("activities", {}).items()):
            legacy_key = str(activity.get("fieldGuideKey") or "")
            if not legacy_key or legacy_key not in LEGACY_LESSON_FINGERPRINTS:
                continue
            if _lesson_fingerprint(activity) == LEGACY_LESSON_FINGERPRINTS[legacy_key]:
                upgrade_report["replacedUnmodified"].append({"lessonKey": legacy_key, "activityId": activity_id})
            else:
                upgrade_report["preservedEdited"].append({"lessonKey": legacy_key, "activityId": activity_id})
        for event in result.get("events", []):
            if event.get("type") != "activity.completed":
                continue
            data = event.get("data") or {}
            legacy_key = str(data.get("fieldGuideKey") or "")
            if legacy_key in LEGACY_LESSON_MAP:
                upgrade_report["legacyProgress"].append({
                    "oldLessonKey": legacy_key,
                    "newLessonKey": LEGACY_LESSON_MAP[legacy_key],
                    "at": event.get("at"),
                    "countsTowardNewGuide": False,
                })
        # Drop only the unmodified template-owned courses/activities/badges.
        # Edited field-guide records and every user-authored Class remain.
        replaced_ids = {item["activityId"] for item in upgrade_report["replacedUnmodified"]}
        replaced_courses = {
            str(activity.get("courseId"))
            for activity in result.get("activities", {}).values()
            if activity.get("id") in replaced_ids
        }
        for activity_id in replaced_ids:
            result.get("activities", {}).pop(activity_id, None)
        for course_id in replaced_courses:
            course = result.get("courses", {}).pop(course_id, None)
            if not course:
                continue
            for module_id in list(course.get("moduleIds") or []):
                result.get("modules", {}).pop(module_id, None)
            for badge_id, badge in list(result.get("badges", {}).items()):
                if badge.get("courseId") == course_id and str(badge.get("fieldGuideKey") or "").startswith("fg-"):
                    result["badges"].pop(badge_id, None)
        # Learner progress rows that pointed at removed template activities are
        # left intact as history; projections ignore missing activities.  We
        # deliberately do not delete enrolments or completion events.

    result = _install_courses(result, owner_id, manifest, version_suffix=FIELD_GUIDE_TEMPLATE_VERSION)

    # Keep the old-to-new mapping and upgrade report for historical display.
    result["fieldGuide"] = {
        "templateKey": manifest["templateKey"],
        "templateVersion": manifest["templateVersion"],
        "coverageContract": manifest["coverageContract"],
        "suggestedOrder": manifest["suggestedOrder"],
        "legacyLessonMap": dict(LEGACY_LESSON_MAP),
        "ownerAccountId": owner_id,
        "freeExploration": True,
    }
    result.setdefault("extensions", {})["fieldGuideManifest"] = manifest
    result["extensions"]["fieldGuideUpgrade"] = upgrade_report

    # Capture the change in the domain event log so recovery can see it.
    # This is the TreeHouse-side Lore record; host-level Lore capture of the
    # same change is a separate integration boundary (see receipt).
    result.setdefault("events", []).append({
        "id": f"event:field-guide-upgrade:{FIELD_GUIDE_TEMPLATE_VERSION}",
        "type": "fieldGuide.upgraded",
        "subjectId": owner_id,
        "entityType": "fieldGuide",
        "entityId": FIELD_GUIDE_TEMPLATE_KEY,
        "data": {
            "from": upgrade_report["from"],
            "to": upgrade_report["to"],
            "replacedUnmodified": len(upgrade_report["replacedUnmodified"]),
            "preservedEdited": len(upgrade_report["preservedEdited"]),
            "legacyProgressEntries": len(upgrade_report["legacyProgress"]),
            "lessonCount": 30,
        },
        "at": _now_iso(),
    })
    return result


def _now_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def upgrade_field_guide(state: dict[str, Any], owner_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Explicit upgrade entry point.  Returns (state, upgrade report)."""
    before = copy.deepcopy(state.get("extensions", {}).get("fieldGuideUpgrade") or {})
    result = instantiate_field_guide(state, owner_id)
    report = result.get("extensions", {}).get("fieldGuideUpgrade") or before
    return result, report


__all__ = [
    "FIELD_GUIDE_TEMPLATE_KEY",
    "FIELD_GUIDE_TEMPLATE_VERSION",
    "LEGACY_TEMPLATE_VERSIONS",
    "LEGACY_LESSON_MAP",
    "CLASS_KEYS",
    "APP_DESTINATIONS",
    "field_guide_manifest",
    "validate_field_guide_manifest",
    "instantiate_field_guide",
    "upgrade_field_guide",
]
