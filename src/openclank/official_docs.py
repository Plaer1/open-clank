"""Maintained official Open Clank documentation — source, manifest, provisioning.

This module owns the first-party official content that is provisioned into a
top-level read-only docs folder in every account's Copal. Content reflects the
approved host/product behavior from the S11–S20 slices: Editor owns Wiki as a
page type, image work uses Files/Imps, Theme lives in Settings, Graph hides
provisioned docs by identity. Repository docs under ``docs/`` remain
learning/reference material and are not relocated by this module.

The manifest is data, not a home-directory side effect. Stable article IDs,
hierarchy, bodies and known aliases live here so provisioning can be
idempotent, Help can address the same content IDs as folder browsing, and the
Graph can recognize provisioned records by real identity metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# Stable content version. Bump when maintained bodies change so idempotent
# provisioning can replace prior official revisions in place.
# v2 — S30 reconciliation against the completed S22–S26 theme/effect UI:
# shipped effect names, typography/accessibility controls, and repaired
# clank://chat / clank://tasks app-link targets.
OFFICIAL_DOCS_SEED_VERSION = 2

# Top-level folder that holds every provisioned official page. Derived identity
# (product/builtin markers) is the real recognition mechanism; this name is the
# presentation root that Graph binds when it holds only provisioned documents.
OFFICIAL_ROOT_FOLDER = "OpenClank"

# Identity markers written into every provisioned note. ``isOfficialDocument``
# and the Graph route recognize these — never a name, dot-prefix or read-only
# status alone.
PRODUCT_MARKER = "open-clank"

# Rejected legacy page-unit vocabulary. Literal and decoded checks confirm this
# terminology is absent from official content; personal history is never
# rewritten to enforce it.
_BANNED_TERMINOLOGY = ("tiddler", "tiddly")

_ARTICLE: dict[str, dict[str, Any]] = {}


def _article(article_id: str, name: str, title: str, body: str, *, aliases: tuple[str, ...] = (), order: int = 0) -> None:
    _ARTICLE[article_id] = {
        "id": article_id,
        "name": name,
        "title": title,
        "aliases": list(aliases),
        "order": order,
        "body": body,
    }


# ── Official articles ────────────────────────────────────────────────────────

_article(
    "openclank-docs-home",
    f"{OFFICIAL_ROOT_FOLDER}/Home",
    "Open Clank Handbook",
    """# Open Clank Handbook

Welcome. This is the maintained handbook that ships with Open Clank. It is
read-only: use **Make editable copy** on any page to keep your own notes beside
it. Updates replace these pages in place and never touch your personal copies.

## Start here

- [[Getting Work Done]] — the shape of a normal session
- [[Editor]] — documents, Wiki pages, templates and media
- [[Files and Imps]] — the file tree and managed image projects
- [[Graph]] — how your documents connect

## Your account and tools

- [[Accounts and Models]] — identity, providers and model choice
- [[Chat and Workspaces]] — conversations, drafts and context
- [[Tasks and Continuations]] — turning notes into follow-through

## The rest of the handbook

- [[Memory and Lore]] — what the assistant remembers and how recovery works
- [[Settings and Themes]] — appearance, panels and preferences
- [[Recovery]] — history, backups and when things go wrong
- [[Limits and Platform Support]] — honest capability limits
- [[Markdown Formatting Demo]] — every rendered element with its source

## Go somewhere in the app

These links open the real screen. They never close a window you already have
open and never start a chat on your behalf.

- Open [Settings](clank://settings) or jump to [appearance](clank://settings/appearance)
- Browse [Files](clank://files) or open the [Graph](clank://graph)
- Continue in [Editor](clank://editor) or the [Wiki library](clank://wiki)
""",
    aliases=(f"{OFFICIAL_ROOT_FOLDER}/Start Here", "OpenClank Handbook"),
    order=0,
)

_article(
    "openclank-docs-getting-work-done",
    f"{OFFICIAL_ROOT_FOLDER}/Getting Work Done",
    "Getting Work Done",
    """# Getting Work Done

Open Clank is a chat-first workspace. You talk with your assistant in the
center, and the Copal suite — Editor, Files, Graph, Timeline, TreeHouse and
Tasks — holds the durable material you produce together.

## A normal session

1. Start or resume a chat. Every conversation keeps its own transcript and its
   own task list.
2. Ask for something concrete. The assistant can read and write your Copal
   documents, run tools, and reference what it did later.
3. Capture the durable parts. Notes, tables, images and tasks live in Copal
   rather than only in the transcript.

Open [Editor](clank://editor) to see the documents, or [Chat](clank://chat) to
return to the conversation.

## Where things live

| You want to | Go to |
| --- | --- |
| Write or read long-form documents | [Editor](clank://editor) |
| Organize linked knowledge pages | [Wiki library](clank://wiki) |
| Manage files and images | [Files](clank://files) |
| See how documents connect | [Graph](clank://graph) |
| Track follow-through | [Tasks](clank://tasks) |
| Change appearance or behavior | [Settings](clank://settings) |

## Two kinds of content

**Personal** notes, copies and annotations belong to you. Rename them, edit
them, delete them — the assistant will not overwrite them just because a name
looks familiar.

**Official** pages like this handbook are read-only and shared by every
account on this installation. They live in the `OpenClank/` folder and update
with the product.

When you want to change an official page, make an editable copy. Your copy is
yours from then on.
""",
    order=1,
)

_article(
    "openclank-docs-accounts-models",
    f"{OFFICIAL_ROOT_FOLDER}/Accounts and Models",
    "Accounts and Models",
    """# Accounts and Models

Open Clank is multi-account. Each account owns its own chats, documents, files
and settings, so two people can share one installation without seeing each
other's work.

## Your account

Your account is chosen at sign-in. Documents, tasks, memory and settings are
scoped to that account. If you sign in as someone else you get their
workspace, not yours — nothing is merged.

Local installs without authentication map to a single local account. The
Copal document store is still scoped per owner internally, so enabling
authentication later does not scramble existing data.

## Choosing a model

Models are configured in [Settings](clank://settings/providers). A provider is
the service that runs a model — a local runtime, or a hosted API you bring a
key for.

Practical guidance:

- Pick a small, fast model for quick edits and summaries.
- Pick a stronger model for long reasoning, code or research.
- Switch mid-conversation if a task grows; the transcript keeps both.

Compare is available for side-by-side answers from two models on the same
prompt. It is useful when you are deciding which model to keep.

## What Open Clank does not do

- It does not choose a model for you or silently fall back to a different
  provider without telling you.
- It does not send your documents to a provider unless a tool call or prompt
  needs them.
- It does not store provider API keys in your documents or in first-party
  source.

See [[Limits and Platform Support]] for what runs where.
""",
    order=2,
)

_article(
    "openclank-docs-chat-workspaces",
    f"{OFFICIAL_ROOT_FOLDER}/Chat and Workspaces",
    "Chat and Workspaces",
    """# Chat and Workspaces

Chat is the conversational surface. Copal is the document surface. They share
one account and one set of resources, but they have different rules.

## Chat

Each conversation is its own transcript with its own history, attachments and
task list. You can keep many conversations open and switch between them from
the sidebar.

Chats can reference Copal documents by name. When the assistant quotes or
links a document, opening that link goes to the document in Editor — it does
not fork the chat or lose your draft.

## Workspaces and drafts

Editor keeps drafts. If you type into a document and navigate away, the draft
is kept and restored when you come back. Saving is guarded: if the stored
version moved underneath you, the save is refused rather than silently
overwriting newer work.

Windows in Copal (Editor, Graph, Timeline and the rest) keep their own
position and selection. Closing a window does not delete anything.

## Identity in links

Document links carry the document identity, not just a title. Renaming a
document keeps its links working. Sharing a link to someone on another account
of the same installation resolves inside their own scope — they see their
copy of the target, not yours.

## Preserving your work

- Drafts survive navigation and window management.
- Answering a task toast resumes in the original task chat, not whichever
  chat happens to be in front.
- Nothing in chat rewrites a Copal document without going through the normal
  guarded save.

Continue with [[Editor]] or [[Tasks and Continuations]].
""",
    order=3,
)

_article(
    "openclank-docs-editor",
    f"{OFFICIAL_ROOT_FOLDER}/Editor",
    "Editor",
    """# Editor

Editor is the shared document workspace. Markdown pages, Wiki pages, typed
notes, Bases, Canvas, tables and media all open here as typed leaves with
tabs and splits.

## Pages and tabs

Open a document from the file tree or a link. Use tabs for parallel reading,
and splits for side-by-side work. Navigation intent is honored: a normal click
opens in the current tab, Command/Control-click opens a new tab, and the
context menu offers split right or split below.

Links between documents navigate the tab you are in. Explicit new-tab
navigation stays explicit — nothing hijacks your current view.

## Wiki pages

Wiki is a page type inside Editor, not a separate application. The Wiki
library lists your knowledge pages and can create, export and import them as
`.memes` files. Opening a page gives you the article view with links,
backlinks and details — the same tabs, splits and save ownership as any other
document.

Wikilinks look like `[[Some Page]]`. Existing targets open on click; missing
targets are shown as broken links you can create. Backlinks on a page list
every page that points at it.

See the [Wiki library](clank://wiki) or open [Editor](clank://editor).

## Templates

Templates live in a folder you choose in
[Settings](clank://settings). Inserting a template copies its assets and
links into the current document and keeps resource identity, so attachments
stay attached.

## Source comments

Rich source comments attach a comment to a range of the document source. They
travel with the document and survive edits outside the range. Comments are
part of the document record, not a separate overlay store.

## Media and attachments

Images, audio, video and PDFs can be attached to a document or referenced by
path. Embedded media renders inline in the Markdown view. Attached files are
owned by the document and move with it.

The [[Markdown Formatting Demo]] shows every formatting element this renderer
supports, with its raw source one click away.
""",
    order=4,
)

_article(
    "openclank-docs-formatting-demo",
    f"{OFFICIAL_ROOT_FOLDER}/Markdown Formatting Demo",
    "Markdown Formatting Demo",
    """# Markdown Formatting Demo

This page is the formatting demonstration. Click any rendered element below to
reveal the exact Markdown source that produced it. The source inspector is
read-only — you can select and copy from it, but it never edits this page and
never starts a save. Use **Back to rendered** to return.

## Headings

### A level-three heading

#### A level-four heading

## Emphasis and marks

Some **bold text**, some *italic text*, some ~~struck-through text~~, and some
==highlighted text==.

Inline `code spans` look like this.

## Lists

- First bullet
- Second bullet
  - A nested bullet
- Third bullet

1. First step
2. Second step
3. Third step

## Task list

- [x] A finished item
- [ ] An open item

## Quote

> A quoted line.
> It can span more than one line.

## Code

```
function hello() {
  return 'world';
}
```

## Rule

---

## Table

| Column | Meaning |
| --- | --- |
| Name | What it is called |
| Value | What it holds |

## Links and references

A [standard link](clank://settings) opens a real app screen.

A wikilink target such as [[Editor]] opens another document in this Editor.

## Footnote-style note

Everything on this page is ordinary Markdown. The only special behavior is
the read-only source reveal above, and it exists for this demonstration alone
— normal documentation pages simply stay rendered.
""",
    aliases=(f"{OFFICIAL_ROOT_FOLDER}/Formatting Demo",),
    order=5,
)

_article(
    "openclank-docs-files-imps",
    f"{OFFICIAL_ROOT_FOLDER}/Files and Imps",
    "Files and Imps",
    """# Files and Imps

Files is the file tree for your account: documents, folders, media and the
managed image projects that Imps produces.

## The file tree

Open [Files](clank://files) to browse. Files and Editor share one resource
identity: a document opened from Files is the same document, with the same
draft and history. Moving a document keeps its links and attachments.

Files exposes capabilities explicitly — read, edit, write — so a read-only
resource says so instead of failing later on save.

## Imps managed projects

Imps is the image editor. Its projects are managed records: every save
captures a recovery preimage first, and the save is refused if that capture
fails. You can undo a save by restoring the captured preimage.

Project endpoints live under `/api/imps/projects/*`. A project binds to a
provider image resource and keeps its own revision counter so concurrent
saves cannot silently clobber each other.

## Gallery

The separate Gallery applet is retired. Provisioned image content now lives
in the **Photos** folder under Files. Legacy `gallery:` links resolve to
Files, and image editing opens the Imps editor. Album management is gone with
the applet; the folder is an ordinary folder you can move and rename.

Open [Files](clank://files) to see Photos.

## Media in documents

Media attached to documents is stored with the document and moves with it.
Embedding an image in Markdown renders it inline; a PDF embed shows a viewer;
audio and video embed their player.

See [[Editor]] for attaching media to a document.
""",
    order=6,
)

_article(
    "openclank-docs-graph",
    f"{OFFICIAL_ROOT_FOLDER}/Graph",
    "Graph",
    """# Graph

Graph shows how your documents connect: wikilinks, embeds, tags and typed
relations all become edges. Galaxy mode arranges documents and calendar
events together; structure mode turns a document's headings and bullets into
a navigable outline.

Open the [Graph](clank://graph) or jump to [Galaxy](clank://galaxy).

## What you see

Nodes are documents. Edges are relationships — a link, an embed, a tag
membership or a typed relation from a note's properties. Clicking a node
opens that document in Editor.

## Filters

The filter panel narrows by kind, folder, tag and property value. Facets come
from real document metadata, so a value that exists only on a document you
have not loaded yet is still discoverable — the facet list pages through the
server until it has seen everything.

Empty selections narrow the result; clearing a filter opens it back up.

## Official documentation in Graph

This handbook is provisioned documentation. Graph hides provisioned documents
by default so your personal notes are what you see first. Turn on **Include
official docs** to add them back, or click the `OpenClank` folder chip to
include just that folder.

Hiding is based on real identity metadata — the shipped product marker — not
on the folder name. Your own notes in any folder, including `OpenClank/`, are
never hidden just because of where they live.

## Structure mode

For a single document, structure mode lists headings and bullets and lets you
rearrange sections. It is the right tool for reshaping a long page; [[Editor]]
is the right tool for rewriting one.
""",
    order=7,
)

_article(
    "openclank-docs-memory-lore",
    f"{OFFICIAL_ROOT_FOLDER}/Memory and Lore",
    "Memory and Lore",
    """# Memory and Lore

Open Clank has two related but separate memory systems. **Memory** (the Brain
surface) is what the assistant recalls between sessions. **Lore** is the
recovery ledger that lets work be undone and reconstructed.

## Memory

Memory capture is optional and per-account. When enabled, the assistant can
save short recollections that later sessions can use. You can review, edit and
delete saved memories from [Settings](clank://settings) under Brain.

Memory never replaces your documents. It is recall, not storage — the
authoritative record of your work is Copal and the chat transcript.

## Lore

Lore is the durable preimage and action ledger behind recoverable operations.
When a managed save happens — an Imps project save, a guarded document
commit — the previous state is captured first. If capture fails, the save is
refused rather than leaving an unrecoverable hole.

You do not interact with Lore directly. You see it in History and in the
undo/restore affordances that depend on it.

## History

Document history lists checkpoints and changes for a single document. From
there you can inspect a prior version and restore it. History is scoped to
the document and the account.

See [Settings](clank://settings/history) for retention budgets.

## What memory is not

- It is not a search index over everything you have ever typed.
- It is not shared between accounts.
- It is not a substitute for saving a document you care about.

[[Recovery]] covers what to do when something goes wrong.
""",
    order=8,
)

_article(
    "openclank-docs-tasks",
    f"{OFFICIAL_ROOT_FOLDER}/Tasks and Continuations",
    "Tasks and Continuations",
    """# Tasks and Continuations

Tasks are durable follow-through items. They are the bridge between "the
assistant said it would do X" and "X actually happened, and here is where."

## Task chats

Each task keeps one durable chat. Opening a task from the toast, the badge or
the task list opens that chat — the original one — not whichever conversation
happens to be in front. Answering a question in the toast resumes the task
chat without hijacking your foreground chat.

Open [Meatbag Tasks](clank://tasks) for the full list, or [the task view](clank://tasks)
inside Copal.

## Sources

Tasks can come from a chat, from a checkbox in a document, or from a
scheduled run. A checkbox task in a document carries the document identity,
so the task and the line it came from stay connected.

## Continuations

A continuation is a resumed run of the same task chat. Context accumulates in
that task's transcript rather than scattering across new conversations. When
a task is done, its chat remains as the record of what happened.

## Practical habits

- Ask the assistant to create a task when work will outlive the current
  conversation.
- Keep one task per outcome; the chat under it holds the detail.
- Use checkboxes in documents for work that belongs to that document.

Next: [[Memory and Lore]] for what survives a restart, or [[Recovery]] for
what to do when a run goes wrong.
""",
    order=9,
)

_article(
    "openclank-docs-settings-themes",
    f"{OFFICIAL_ROOT_FOLDER}/Settings and Themes",
    "Settings and Themes",
    """# Settings and Themes

Settings owns configuration and appearance. Theme lives here — there is no
separate theme applet.

Open [Settings](clank://settings).

## Panels

| Panel | What it holds |
| --- | --- |
| [Appearance](clank://settings/appearance) | Theme, colors, background effects |
| [Providers](clank://settings/providers) | Model providers and keys |
| [History](clank://settings/history) | Memory and retention budgets |
| [File access](clank://settings/file-access) | What the assistant may touch |

Settings panels are reachable by direct navigation. Opening a panel from a
link focuses it; it never closes a window you already had open.

## Themes

Themes change colors, backgrounds and effects across the whole shell. The
theme picker shows live previews. Custom accent colors are available on every
theme, and you can save your own named themes (up to eight) and share them as
JSON.

Background / Effect chooses the decorative background. The shipped effects
are: Solid, Clanker Signal Routes, Shipibo Kene-Inspired Signal Weave,
Clanker LCARS, Clanker Gem Drift, Clanker Emoji Drift, Clanker Matrix Rain,
Clanker Emoji Rain, Clanker LCARS Status Sweep, Dots, Synapse, Rain,
Constellations, Perlin Flow, Petals, Sparkles and Embers. Each effect that
supports it exposes an effect color, an Intensity slider and a Size slider;
effects without those controls hide them.

Background effects are decorative. On machines where an effect is too heavy
it can be turned off without changing the rest of the theme.

## Typography and accessibility

Appearance also controls typography: a font choice, a density setting
(comfortable, compact, spacious) and a UI text-size scale that is independent
of the active theme. A frosted-glass toggle softens panels where the theme
supports it. These are readability controls, not decoration.

## What settings does not do

- It does not migrate your documents.
- It does not change other accounts' settings.
- It does not require a restart for appearance changes.

[[Accounts and Models]] covers provider setup in more detail.
""",
    order=10,
)

_article(
    "openclank-docs-recovery",
    f"{OFFICIAL_ROOT_FOLDER}/Recovery",
    "Recovery",
    """# Recovery

Most of the time nothing goes wrong. When it does, this page is the map.

## A save was refused

Guarded saves refuse to overwrite newer work. Reopen the document and merge —
the version you were editing is still in your draft buffer, and the stored
version is intact.

## A document looks wrong

If a document shows a recovery panel instead of its content, the stored bytes
could not be decoded. The original source is preserved. Use the recovery
panel to inspect the preserved source, or restore from history.

Nothing silently rewrites a document that failed to decode.

## Undo a managed save

Imps project saves capture a preimage first. Restore from the project's
history to replay that preimage and undo the save.

## Restore a document version

Document history lists checkpoints. Pick a version and restore it. The
restore is itself recorded, so you can go forward again.

## Start over on official pages

Official handbook pages like this one are read-only and update with the
product. If you made an editable copy and want a fresh original, delete your
copy and reopen the official page — the original is still there.

## When to ask for help

- A scheduled task ran but produced nothing: its task chat has the transcript.
- Files are missing: check [Files](clank://files) trash before assuming loss.
- Settings look wrong: [Settings](clank://settings) panels are per account.

See [[Memory and Lore]] for the history and retention side.
""",
    order=11,
)

_article(
    "openclank-docs-limits",
    f"{OFFICIAL_ROOT_FOLDER}/Limits and Platform Support",
    "Limits and Platform Support",
    """# Limits and Platform Support

Honest limits are part of the handbook. This page is where they live.

## Data formats

**Strict JSON has no comments.** Anything stored as strict JSON —
configuration, project files, interchange bundles — cannot carry explanatory
comments. Use a Markdown page or a document property if you need to annotate
data.

**Native notes are structured.** A Copal note is a document tree with typed
blocks, properties and relations, not a plain `.md` file. Markdown is the
interchange and the editing projection; the stored record is richer.

## Platform support

Open Clank runs natively on macOS, Windows and Linux. GPU-accelerated local
models depend on the platform:

- macOS: Metal through the local runtime. MLX-only models are not served.
- Windows: native launcher binds loopback by default; LAN exposure is opt-in.
- Linux: native packages; GPU support depends on the installed driver stack.

Containerized deployment is documented for servers but is not the desktop
experience and is not what these desktop docs qualify.

## Renderer scope

The Markdown renderer is not CommonMark-complete. It supports headings,
emphasis, marks, inline code, fenced code, lists, task lists, quotes,
dividers, tables, wikilinks, standard links, app links and embedded media.
Exotic constructs fall through as literal text rather than being mangled.

The formatting source-reveal inspector exists for
[[Markdown Formatting Demo]] only. Normal documentation pages stay rendered.

## What is not claimed here

- No web or public documentation site is launched by this handbook.
- No cross-account sharing of documents.
- No automatic migration of personal files into Copal.

If something on this page is out of date relative to the running product,
the running product is right and this page will be updated with it.
""",
    order=12,
)


# ── Manifest access ──────────────────────────────────────────────────────────

def official_articles() -> list[dict[str, Any]]:
    """Every maintained article, in display order."""
    return sorted(_ARTICLE.values(), key=lambda item: (item["order"], item["name"]))


def official_home() -> dict[str, Any]:
    """The article Help opens first."""
    return _ARTICLE["openclank-docs-home"]


def article_by_id(article_id: str) -> dict[str, Any] | None:
    return _ARTICLE.get(str(article_id or ""))


def known_names() -> list[str]:
    """Canonical names plus historical aliases, for install-fixture checks."""
    names: list[str] = []
    for article in official_articles():
        names.append(article["name"])
        names.extend(article["aliases"])
    return names


# ── Terminology gate ─────────────────────────────────────────────────────────

def terminology_violations(text: str) -> list[str]:
    """Rejected legacy vocabulary found in ``text`` (case-insensitive)."""
    lowered = str(text or "").lower()
    return [term for term in _BANNED_TERMINOLOGY if term in lowered]


def validate_official_terminology(*texts: str) -> list[str]:
    """All rejected terms across the supplied texts, for install-fixture checks."""
    found: list[str] = []
    for text in texts:
        found.extend(terminology_violations(text))
    return found


# ── Native note encoding ─────────────────────────────────────────────────────
#
# Mirrors the record shape produced by routes.copal_routes._encode_note so a
# provisioned official page is a real native note: typed blocks, identity
# properties and a Markdown interchange source that records whether the
# maintained body is still unmodified. Provisioning never needs the route
# layer to build content.

_NOTE_SCHEMA_VERSION = 1

_NOTE_LINK = re.compile(r"(!?)\[\[([^\]\n]+)\]\]")
_NOTE_TAG = re.compile(r"(?<![\w/])#([\w][\w/-]*)", re.UNICODE)


def _stable_id(prefix: str, material: str) -> str:
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _block_from_line(line: str, namespace: str, index: int) -> dict[str, Any]:
    block: dict[str, Any]
    if not line:
        block = {"type": "blank", "text": ""}
    elif match := re.fullmatch(r"(#{1,6})\s+(.*)", line):
        block = {"type": "heading", "level": len(match.group(1)), "text": match.group(2)}
    elif match := re.fullmatch(r"(\s*)([-*+])\s+\[([ xX])\]\s*(.*)", line):
        block = {"type": "task", "indent": len(match.group(1)), "marker": match.group(2), "checked": match.group(3).lower() == "x", "text": match.group(4)}
    elif match := re.fullmatch(r"(\s*)[-*+]\s+(.*)", line):
        block = {"type": "bullet", "indent": len(match.group(1)), "text": match.group(2)}
    elif match := re.fullmatch(r"(\s*)(\d+)\.\s+(.*)", line):
        block = {"type": "ordered", "indent": len(match.group(1)), "number": int(match.group(2)), "text": match.group(3)}
    elif match := re.fullmatch(r"\s*>\s?(.*)", line):
        block = {"type": "quote", "text": match.group(1)}
    elif re.fullmatch(r"\s*```.*", line):
        block = {"type": "code-fence", "text": line.strip()[3:]}
    elif re.fullmatch(r"\s*(?:---+|___+|\*\*\*+)\s*", line):
        block = {"type": "divider", "text": ""}
    elif line.count("|") >= 2:
        block = {"type": "table-row", "text": line}
    else:
        block = {"type": "paragraph", "text": line}
    block["source"] = line
    block["id"] = _stable_id("blk", f"{namespace}\0block\0{index}\0{line}")
    return block


def _property_records(properties: dict[str, Any], namespace: str) -> list[dict[str, Any]]:
    records = []
    for key, value in properties.items():
        if isinstance(value, bool):
            kind = "checkbox"
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            kind = "number"
        elif isinstance(value, (list, dict)):
            kind = "tags" if isinstance(value, list) else "object"
        elif isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            kind = "date"
        else:
            kind = "text"
        records.append({
            "id": _stable_id("prop", f"{namespace}\0property\0{key}"),
            "key": key,
            "type": kind,
            "value": value,
        })
    return records


def encode_official_note(body: str, properties: dict[str, Any]) -> str:
    """Encode one official article body as a native Copal note record.

    ``properties`` must already carry the identity markers (product, builtin,
    docId). The returned JSON is the document content stored by the bridge.
    """
    namespace = str(properties.get("docId") or properties.get("title") or "official")
    lines = str(body or "").split("\n")
    blocks = [_block_from_line(line, namespace, index) for index, line in enumerate(lines)]

    relations: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for match in _NOTE_LINK.finditer(body or ""):
        raw = match.group(2).split("|", 1)[0].strip()
        target, _, fragment = raw.partition("#")
        target = target.strip()
        if not target:
            continue
        key = ("embed" if match.group(1) else "link", target, fragment.strip())
        if key in seen:
            continue
        seen.add(key)
        relations.append({
            "id": _stable_id("rel", f"{namespace}\0relation\0{key[0]}\0{key[1]}\0{key[2]}"),
            "kind": key[0],
            "sourceBlockId": None,
            "target": key[1],
            "targetDocumentId": None,
            "targetBlockId": None,
            "fragment": key[2] or None,
        })
    tags = list(dict.fromkeys(match.group(1) for match in _NOTE_TAG.finditer(body or "")))
    for tag in tags:
        relations.append({
            "id": _stable_id("rel", f"{namespace}\0tag\0{tag}"),
            "kind": "tag",
            "sourceBlockId": None,
            "target": tag,
            "targetDocumentId": None,
            "targetBlockId": None,
        })

    record = {
        "schemaVersion": _NOTE_SCHEMA_VERSION,
        "body": {"type": "doc", "blocks": blocks},
        "properties": _property_records(properties, namespace),
        "relations": relations,
        "tags": tags,
        "extensions": {
            "interchange": {
                "format": "markdown",
                "source": str(body or ""),
                "projectionHash": hashlib.sha256(str(body or "").encode("utf-8")).hexdigest(),
                "modified": False,
            },
            "seed": {"version": OFFICIAL_DOCS_SEED_VERSION, "name": properties.get("docId")},
        },
    }
    return json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def article_payload(article: dict[str, Any]) -> dict[str, Any]:
    """Name, encoded content and identity markers for one manifest article."""
    properties = {
        "product": PRODUCT_MARKER,
        "builtin": True,
        "docId": article["id"],
        "seedVersion": OFFICIAL_DOCS_SEED_VERSION,
        "title": article["title"],
    }
    return {
        "name": article["name"],
        "kind": "wiki",
        "corpus": "wiki",
        "read_only": True,
        "content": encode_official_note(article["body"], properties),
        "aliases": list(article["aliases"]),
        "body": article["body"],
        "title": article["title"],
        "docId": article["id"],
    }


def official_payloads() -> list[dict[str, Any]]:
    """Every article ready for provisioning, in display order."""
    return [article_payload(article) for article in official_articles()]


# ── Provisioning plan ────────────────────────────────────────────────────────

def _existing_properties(document: dict[str, Any]) -> dict[str, Any]:
    properties = document.get("properties")
    return dict(properties) if isinstance(properties, dict) else {}


def _existing_doc_id(document: dict[str, Any]) -> str:
    properties = _existing_properties(document)
    return str(properties.get("docId") or "")


def _is_official_record(document: dict[str, Any]) -> bool:
    """Identity-only recognition, matching Graph's ``isOfficialDocument``."""
    if document.get("builtin") is True:
        return True
    properties = _existing_properties(document)
    product = str(properties.get("product", document.get("product", ""))).strip().lower()
    return product == PRODUCT_MARKER or properties.get("builtin") is True


def _is_user_modified(document: dict[str, Any]) -> bool:
    """True when the maintained body is no longer the shipped one."""
    extensions = document.get("extensions")
    if not isinstance(extensions, dict):
        extensions = {}
    interchange = extensions.get("interchange")
    if isinstance(interchange, dict) and interchange.get("modified") is True:
        return True
    properties = _existing_properties(document)
    stored = properties.get("seedVersion")
    if isinstance(stored, (int, float)) and not isinstance(stored, bool):
        if int(stored) > OFFICIAL_DOCS_SEED_VERSION:
            return True
    return False


def plan_input_from_content(
    document_id: str,
    name: str,
    content: str,
    *,
    read_only: bool = False,
    trashed: bool = False,
) -> dict[str, Any]:
    """Build one ``plan_official_provision`` input row from stored note bytes.

    Loose records keep identity inside the encoded note (product, builtin,
    docId), not on the document record. Decode that identity so the live
    provisioning path can use the same docId/alias matching as the plan helper.
    """
    properties: dict[str, Any] = {}
    extensions: dict[str, Any] = {}
    try:
        record = json.loads(str(content or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        record = None
    if isinstance(record, dict):
        raw_properties = record.get("properties")
        if isinstance(raw_properties, list):
            for prop in raw_properties:
                if isinstance(prop, dict) and isinstance(prop.get("key"), str):
                    properties[prop["key"]] = prop.get("value")
        elif isinstance(raw_properties, dict):
            properties = dict(raw_properties)
        raw_extensions = record.get("extensions")
        if isinstance(raw_extensions, dict):
            extensions = raw_extensions
    return {
        "id": document_id,
        "name": name,
        "trashed": trashed,
        "readOnly": read_only,
        "properties": properties,
        "extensions": extensions,
    }


def plan_official_provision(
    existing: list[dict[str, Any]],
    payloads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Decide create/update/skip for every manifest article.

    Matching is by stable identity (the ``docId`` property) first. A same-named
    page that is not ours is personal data and is never overwritten or
    claimed. Known aliases of previously installed defaults are adopted and
    renamed to the canonical name in place.

    ``payloads`` defaults to the full maintained manifest; callers that
    provision a subset pass that subset so interrupted runs resume cleanly.

    Returns ``{"create": [...], "update": [...], "skip": [...], "conflicts": [...]}``
    where each entry carries the payload and, for updates, the matched
    document id.
    """
    if payloads is None:
        payloads = official_payloads()
    by_doc_id: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    for document in existing or []:
        if not isinstance(document, dict) or document.get("trashed"):
            continue
        doc_id = _existing_doc_id(document)
        if doc_id:
            by_doc_id[doc_id] = document
        name = str(document.get("name") or "")
        if name:
            by_name[name] = document

    plan: dict[str, Any] = {"create": [], "update": [], "skip": [], "conflicts": []}
    for payload in payloads:
        doc_id = payload["docId"]
        name = payload["name"]
        matched = by_doc_id.get(doc_id)
        rename_from: str | None = None
        if matched is None:
            # A previously installed default sitting under a known historical
            # alias is adopted and renamed to the canonical name in place.
            for alias in payload.get("aliases") or ():
                alias_hit = by_name.get(str(alias))
                if alias_hit is not None and _is_official_record(alias_hit) and not _is_user_modified(alias_hit):
                    matched = alias_hit
                    rename_from = str(alias_hit.get("name") or "")
                    break
        if matched is None:
            named = by_name.get(name)
            if named is not None and _is_official_record(named) and not _existing_doc_id(named):
                # Previously installed default without a stable id: adopt it.
                matched = named
        if matched is not None and rename_from is None:
            # Identity matched but the record still sits under a known alias.
            current_name = str(matched.get("name") or "")
            aliases = {str(alias) for alias in payload.get("aliases") or ()}
            if current_name and current_name != name and current_name in aliases:
                rename_from = current_name
        if matched is None:
            collision = by_name.get(name)
            if collision is not None and not _is_official_record(collision):
                plan["conflicts"].append({
                    "docId": doc_id,
                    "name": name,
                    "reason": "personal-page-occupies-name",
                    "existingId": collision.get("id"),
                })
                continue
            plan["create"].append(payload)
            continue
        if _is_user_modified(matched):
            plan["skip"].append({
                "docId": doc_id,
                "name": name,
                "existingId": matched.get("id"),
                "reason": "user-modified",
            })
            continue
        plan["update"].append({
            **payload,
            "existingId": matched.get("id"),
            "renameFrom": rename_from,
        })
    return plan


def plan_summary(plan: dict[str, Any]) -> dict[str, int]:
    return {
        "create": len(plan.get("create") or []),
        "update": len(plan.get("update") or []),
        "skip": len(plan.get("skip") or []),
        "conflicts": len(plan.get("conflicts") or []),
    }


def is_user_modified_content(content: str) -> bool:
    """True when stored note content is no longer an unmodified official revision.

    Used by the loose provisioning path so a personal edit of a provisioned
    page is never replaced by a newer maintained body.
    """
    try:
        record = json.loads(str(content or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        # Undecodable content is treated as user-owned; never clobber it.
        return True
    if not isinstance(record, dict):
        return True
    extensions = record.get("extensions")
    if not isinstance(extensions, dict):
        return False
    interchange = extensions.get("interchange")
    if isinstance(interchange, dict) and interchange.get("modified") is True:
        return True
    seed = extensions.get("seed")
    if isinstance(seed, dict):
        version = seed.get("version")
        if isinstance(version, (int, float)) and not isinstance(version, bool):
            if int(version) > OFFICIAL_DOCS_SEED_VERSION:
                return True
    return False
