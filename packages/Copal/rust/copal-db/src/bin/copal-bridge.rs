use std::collections::{BTreeMap, BTreeSet};
use std::io::{self, BufRead, Write};
use std::path::PathBuf;

use copal_db::{Content, Db, DocView, ImportIdentity, WriteOutcome};
use serde_json::{json, Value};

fn required<'a>(args: &'a Value, key: &str) -> Result<&'a str, String> {
    args.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("missing {key}"))
}

fn optional<'a>(args: &'a Value, key: &str) -> Option<&'a str> {
    args.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
}

fn scope<'a>(args: &'a Value) -> Result<(&'a str, &'a str), String> {
    Ok((required(args, "owner")?, required(args, "workspace_id")?))
}

struct KnowledgeSeed {
    slug: &'static str,
    name: &'static str,
    source_path: &'static str,
    body: &'static str,
    links: &'static [&'static str],
}

const OPENCLANK_KNOWLEDGE_VERSION: u64 = 2;
const OPENCLANK_KNOWLEDGE: &[KnowledgeSeed] = &[
    KnowledgeSeed {
        slug: "start",
        name: "OpenClank/Start Here",
        source_path: "README.md",
        body: r##"# OpenClank

OpenClank is a self-hosted AI workspace combining chat, agents, research, documents, email, notes, calendar, and local model workflows into one integrated environment.

## Explore the built-in knowledge
- [[OpenClank/Chat, Agent, and Identity]] — how conversations and agents work
- [[OpenClank/Memory and Knowledge]] — memory capture, trust, and recall
- [[OpenClank/Copal Notes and Timeline]] — the database-backed knowledge workspace
- [[OpenClank/Architecture and Data]] — filesystem, database, build, and contributor info

## Core surfaces

### Planning and project views
- **Galaxy** — task hub with node visualization for project overviews
- **Timeline** — horizontal chip-based timeline with tracks, tasks, and date ranges
- **Calendar** — monthly grid showing scheduled tasks and events
- **Tasks** — flat task list with source filtering (vault vs timeline)

### Knowledge and documentation views
- **Notes** — Obsidian-style note browser with live Markdown editor, backlinks, properties, outline, and task extraction. Database-native notes with versioned structured records, stable block IDs, typed properties, explicit relations, history, trash, and recovery.
- **Graph** — wikilink graph visualization showing note connections and backlinks
- **Mind** — outline/mind-map view of note structure
- **Wiki** — meme-style knowledge store in a separate database, with story navigation, cross-corpus links, and footer properties

### Learning and data views
- **TreeHouse** — skill-tree/LMS view for structured learning paths
- **Bases** — database-style views for querying and editing structured data

## Architecture at a glance
- **Frontend**: Next.js 16, React 19, Tailwind CSS 4, served from `out/`
- **Server**: Python stdlib HTTP (`app.py`) for vault/notes API; Rust bridge for database operations
- **Database**: Redb (pure Rust, ACID) with content-addressed commits, operation log, and built-in versioning
- **Desktop**: Servo-based native shell (`servo-shell/`) with embedded Next.js build

## Safety baseline
Keep authentication enabled, keep private data out of Git, and expose only the authenticated application entrypoint through a trusted private network or proxy.

#openclank #knowledge #builtin"##,
        links: &[
            "OpenClank/Chat, Agent, and Identity",
            "OpenClank/Memory and Knowledge",
            "OpenClank/Copal Notes and Timeline",
            "OpenClank/Architecture and Data",
        ],
    },
    KnowledgeSeed {
        slug: "identity",
        name: "OpenClank/Chat, Agent, and Identity",
        source_path: "docs/identity-architecture.md",
        body: r##"# Chat, Agent, and Identity

OpenClank has distinct interaction modes that control what the assistant can do and how it reasons.

## Chat
The neutral conversation lane. Chat handles questions, discussion, brainstorming, and tasks that don't require tool execution. It's conversational — no file editing, no shell access, no multi-step workflows.

## Agent
Owns the Build, Plan, and Compose workflows and the tools needed to carry them out. Agent-mode turns use the negotiated workflow while ordinary Chat stays conversational. Agent can escalate from Chat when a request clearly needs tools.

## Plan
Read-only design mode. Plan agents have a hard permission rule that blocks every write tool except plan files. Used for structured planning before implementation — multi-file work, multiple valid approaches, or anything where wrong design costs more than planning.

## Compare
Runs parallel reasoning candidates and returns the best result. Useful for evaluating multiple approaches simultaneously.

## Temporary Agent
Ephemeral agent sessions spawned for specific tasks. They inherit context from the parent session but operate independently, returning results when complete.

## Model selection
The selected model is a transport and capability choice, not the assistant's identity. Identity is composed by Odysseus where the conversation context lives, then rendered downstream. A model change affects capability (which tools are available) but not the assistant's personality or knowledge.

## Capability certification
Before using tools, the system certifies that the selected model supports explicit tool calling. Models that lack tool capability receive a typed, actionable failure rather than silently falling back to no-tools mode.

Return to [[OpenClank/Start Here]].

#openclank #agent #identity"##,
        links: &["OpenClank/Start Here"],
    },
    KnowledgeSeed {
        slug: "memory",
        name: "OpenClank/Memory and Knowledge",
        source_path: "docs/memory-architecture.md",
        body: r##"# Memory and Knowledge

OpenClank distinguishes between conversational memory (ephemeral, per-session) and durable knowledge (persistent, user-authored).

## Conversational memory
- **Turn capture** is owned by Odysseus so changing model transports does not create a second source of truth
- **Session transcripts** persist on disk as JSONL files, surviving restarts and compaction
- **Memory bank** is owner-scoped — one bank across conversation and agent surfaces
- **Trusted guidance** and **untrusted recalled material** stay visibly separate
- **Search and graph recall** pull deeper context when a task needs it, while bounded summaries keep ordinary turns small

## Copal Notes (durable knowledge)
- Database-native structured records with versioned blocks, typed properties, and explicit relations
- History, diff, restore, and trash are built-in — every mutation is a content-addressed commit
- Notes complement conversational memory; neither silently replaces the other
- Wiki is a separate knowledge store for meme-style pages (see [[OpenClank/Copal Notes and Timeline]])

## Knowledge seeds
OpenClank ships built-in knowledge documents (like this one) as read-only database notes. They upgrade automatically when the app updates and never overwrite user-edited content.

Return to [[OpenClank/Start Here]].

#openclank #memory #knowledge"##,
        links: &["OpenClank/Start Here"],
    },
    KnowledgeSeed {
        slug: "copal",
        name: "OpenClank/Copal Notes and Timeline",
        source_path: "routes/copal_routes.py",
        body: r##"# Copal Notes and Timeline

Copal is the database-backed knowledge workspace inside OpenClank.

## Notes
Notes are stored as versioned structured records (`copal-note-v1` schema) with:
- **Stable block IDs** — each paragraph, heading, and list item has a persistent identifier
- **Typed properties** — key-value metadata (tags, dates, custom fields) stored alongside content
- **Explicit relations** — wikilinks, embeds, and cross-references tracked as first-class links
- **History** — every write is a content-addressed commit; diff, log, and restore are queries
- **Trash and recovery** — deleted notes are tombstoned, not erased; restore is one operation
- **Owner scope** — notes are scoped to owner/workspace; shared notes are read-only to others

The editor is a projection of the database record, not a file editor. Changes go through the write pipeline: amend commit, stale detection, checkpoint boundaries.

## Timeline
The Timeline is a typed Notes view over Copal's canonical planning records. Opening it inside Notes or through the Timeline tab reads and edits the same events and tracks — there is no copied timeline database.

Planning data supports:
- Tracks with start/end dates, tasks, and visual styles
- Tasks with due dates, scheduled dates, completion dates, recurrence, and priority
- Fuzzy date ranges for uncertain timelines
- JSON export for AI calendar tools

## Wiki
Wiki is a separate knowledge store for meme-style pages, stored in its own database file (`copal-wiki.redb`). It supports:
- Small, focused pages (memes) with story-based navigation
- Cross-corpus links between Wiki and Notes
- Footer properties on each meme
- Create, edit, search, and trash flows independent from Notes

## Graph
Wikilink graph visualization showing note connections, backlinks, and document relationships. Nodes represent notes; edges represent wikilinks and other relations.

## Mind
Outline/mind-map view showing the heading hierarchy of notes. Useful for navigating document structure at a glance.

## Bases
Database-style views for querying and editing structured data extracted from notes. Supports table views, filters, and inline editing.

## Core workspace features (no plugins required)
Search, links, backlinks, properties, daily notes, templates, Canvas, and recovery all work out of the box.

Return to [[OpenClank/Start Here]].

#openclank #copal #notes #timeline"##,
        links: &["OpenClank/Start Here"],
    },
    KnowledgeSeed {
        slug: "architecture",
        name: "OpenClank/Architecture and Data",
        source_path: "docs/architecture.md",
        body: r##"# Architecture and Data

OpenClank is a local-first application with a clear separation between frontend, server, database, and desktop shell.

## Filesystem layout
- **`packages/Copal/`** — the Copal knowledge workspace (this app)
  - `out/` — built static site (Next.js build output)
  - `db/copal.redb` — main Redb database (Notes, knowledge seeds, assets index)
  - `db/assets/` — content-addressed binary files (`<blake3>.<ext>`)
  - `sample-vault/` — development vault with example notes
  - `rust/` — Rust crates: `copal-core` (parser), `copal-db` (database), `servo-shell` (desktop)
  - `src/` — Next.js frontend (React components, views, store, hooks)
  - `app.py` — Python stdlib HTTP server for vault/notes API
  - `scripts/` — shell scripts for smoke tests and parity checks
  - `copal.toml` — configuration (`debug = true` uses repo-local `db/`)

## Data directories
Resolution order for the database path:
1. `COPAL_DB=/path` environment variable (highest priority)
2. `COPAL_DEBUG=0|1` environment variable
3. `copal.toml` `debug = true/false`
4. Default: `$XDG_DATA_HOME/copal` or `~/.local/share/copal`

With `debug = true` (current default), data lives in `packages/Copal/db/`.

## Database (Redb)
- **Engine**: Redb — pure Rust, ACID, single-file embedded database
- **Tables**: `DOCS` (documents), `COMMITS` (content-addressed commits), `BLOBS` (raw bytes), `OPS` (operation log), `META` (metadata)
- **Schema version**: 3 (auto-migrates on open)
- **Versioning model**: Every accepted write is an amend commit. No staging area — the doc's live state is exactly its head commit. Change identity (`doc_id`, ULID) is stable; commits are content-addressed (blake3) and never rewritten.
- **Checkpoint boundary**: A new change identity opens when the head commit is older than 30 minutes at write time.
- **Undo/restore**: New operation with an older view. Tombstones for deletion.

## Wiki database
Separate Redb file opened via `COPAL_WIKI_DATA_DIR` env var. Uses the same `Store` implementation with `copal-wiki` as the database name. Same code, different file, zero forked behavior.

## Build system
- **Package manager**: bun
- **Frontend**: `bun run dev` (Next.js dev on port 3000), `bun run build` (static export to `out/`)
- **Server**: `bun run start` (launches `app.py` on port 8765)
- **Rust**: `bun run servo:*` commands for Cargo builds; `servo:native-release` for full desktop build
- **Desktop**: Servo shell embeds the Next.js build via `include_dir!` and serves it from a local HTTP server

## API routes

### Python server (`app.py`, port 8765)
- `GET /api/data` — read planning data (move-data.json)
- `POST /api/data` — write planning data
- `GET /api/vault` — vault path info
- `GET /api/ui-state` — read UI state (pinned items, wiki block order)
- `POST /api/ui-state` — write UI state
- `GET /api/notes` — list vault notes
- `GET /api/note?path=` — read a note
- `POST /api/note` — create/update a note
- `POST /api/note/rename` — rename a note
- `POST /api/note/delete` — delete a note
- `POST /api/mkdir` — create directory
- `GET /api/search?q=` — search notes
- `GET /api/backlinks?path=` — get backlinks for a note
- `GET /api/graph` — graph data (nodes + edges)
- `GET /api/tasks` — derived tasks from notes
- `GET /api/index` — build vault index
- `GET /api/vault-asset?path=` — serve binary asset
- `GET /api/export/ai` — AI export format (`copal.ai-export.v0`)
- `GET /api/export/okf` — OKF export format (`copal.okf-inspired.v0`)
- `GET /api/export/doclang` — DocLang XML export
- `GET /api/export/markdown-bundle` — ZIP of markdown files

### Rust bridge (stdin/stdout JSON-RPC)
Operations: `status`, `import_vault`, `list`, `index`, `search`, `export_snapshot`, `get`, `trash`, `create`, `write`, `history`, `checkpoint`, `rename`, `delete`, `restore`, `restore_deleted`, `diff`, `ops`, `asset_path`

## Contributor architecture
- **Tests**: Rust unit tests in `copal-core`, `copal-db`, and `copal-bridge`; shell smoke tests in `scripts/`
- **Clean-room rules**: Proprietary Obsidian code/assets/strings are behavioral evidence only; Copal must not become a hidden fork
- **Reference imports**: External projects are references only; code must not drift into runtime source
- **Packaging**: `servo-shell/Cargo.toml` builds the desktop binary with embedded assets

Return to [[OpenClank/Start Here]].

#openclank #architecture #data #builtin"##,
        links: &["OpenClank/Start Here"],
    },
];

// ── Wiki how-to seeds ────────────────────────────────────────────────────

struct WikiSeed {
    name: &'static str,
    body: &'static str,
}

const WIKI_SEEDS: &[WikiSeed] = &[
    WikiSeed {
        name: "Wiki/What Is Wiki",
        body: r##"# What Is Wiki
Wiki is a separate knowledge store inside Copal, dedicated to meme-style pages. It lives in its own database file (`copal-wiki.redb`) and is fully isolated from Notes.

## Key differences from Notes
- Wiki stores memes (small, focused pages) while Notes stores longer documents
- Wiki has its own create, edit, search, and trash flows
- Wiki pages use `[[wikilinks]]` for cross-referencing
- Properties appear as a compact footer strip on each meme

## Getting started
Create a new meme with the "+ Meme" button in the Wiki sidebar. Give it a name and start writing.

#wiki #howto #seed"##,
    },
    WikiSeed {
        name: "Wiki/Creating and Linking Memes",
        body: r##"# Creating and Linking Memes
## Creating a meme
Click "+ Meme" in the Wiki sidebar. Enter a name. The new meme opens in edit mode — start writing.

## Linking between memes
Use double-bracket wikilinks anywhere in your meme text:
```
See [[Other Meme]] for more details.
```
When rendered, wikilinks become clickable buttons. If the target exists, clicking it opens the meme. If not, the link appears grayed out (broken link state).

## Cross-corpus links
You can link from a Wiki meme to a Notes document and vice versa. The link shows the source and target corpus, so you always know where a link points.

#wiki #howto #seed"##,
    },
    WikiSeed {
        name: "Wiki/Story Navigation",
        body: r##"# Story Navigation
Wiki uses a "story" model — multiple memes can be open side by side.

## Opening memes
Click any meme name in the left sidebar to add it to your story. The story shows up to 3 memes by default.

## Rearranging
Use the arrow buttons (← →) on each meme header to move it left or right in the story.

## Pinning
Click "Pin" to keep a meme in your story even when you close others. Pinned memes stay until you unpin them.

## Closing
Click × to remove a meme from the story (unless it's pinned).

#wiki #howto #seed"##,
    },
    WikiSeed {
        name: "Wiki/Fields and Properties",
        body: r##"# Fields and Properties
Every Wiki meme can have properties (also called fields). These appear as a compact footer strip below the meme content.

## Viewing properties
Look at the bottom of any open meme. Properties like `type`, `status`, or `tags` show as small chips.

## Editing properties
When editing a meme, you can add or modify properties. Properties are stored as key-value pairs in the meme's metadata.

## Common properties
- `type` — categorize the meme (e.g., "reference", "guide", "recipe")
- `tags` — organize memes by topic
- `created` — when the meme was first created

#wiki #howto #seed"##,
    },
    WikiSeed {
        name: "Wiki/Wiki vs Notes",
        body: r##"# Wiki vs Notes
Copal has two knowledge stores: **Notes** and **Wiki**. Here's when to use each.

## Use Notes for
- Long-form documents and journals
- Daily notes and templates
- Structured records with typed properties and relations
- Planning and calendar integration
- Bases and Canvas views

## Use Wiki for
- Quick reference memes
- Interlinked knowledge pages
- TiddlyWiki-style navigation with open stories
- Compact, scannable pages with footer properties

## They work together
Notes and Wiki are separate stores but you can link between them. A Wiki meme can link to a Notes document and vice versa. Each store has its own search, history, and trash.

#wiki #howto #seed"##,
    },
];

fn seed_wiki_pages(db: &Db) -> Result<usize, String> {
    let mut existing = db
        .list_docs()
        .map_err(|error| error.to_string())?
        .into_iter()
        .filter(|doc| doc.owner == "shared" && doc.workspace_id == "global")
        .map(|doc| (doc.name.clone(), doc))
        .collect::<BTreeMap<_, _>>();
    let mut changed = 0;
    for seed in WIKI_SEEDS {
        if let Some(doc) = existing.get(seed.name) {
            if doc.text.as_deref() == Some(seed.body) {
                continue;
            }
            // Update if content changed (idempotent upgrade).
            match db
                .write_doc_scoped(&doc.id, seed.body, Some(&doc.head), "shared", "global")
                .map_err(|error| error.to_string())?
            {
                WriteOutcome::Committed { .. } => changed += 1,
                WriteOutcome::Unchanged { .. } | WriteOutcome::Stale { .. } => {}
            }
            continue;
        }
        let doc = db
            .create_doc("wiki", seed.name, seed.body, Some("built-in Wiki how-to seed"))
            .map_err(|error| error.to_string())?;
        existing.insert(seed.name.to_string(), doc);
        changed += 1;
    }
    Ok(changed)
}

fn knowledge_note(seed: &KnowledgeSeed) -> String {
    let lines = seed.body.split('\n').collect::<Vec<_>>();
    let relations = seed
        .links
        .iter()
        .enumerate()
        .map(|(index, target)| {
            let source_index = lines
                .iter()
                .position(|line| line.contains(&format!("[[{target}]]")));
            json!({
                "id": format!("rel_kb_{}_{}", seed.slug, index + 1),
                "kind": "link",
                "origin": "body",
                "sourceBlockId": source_index.map(|line| format!("blk_kb_{}_{}", seed.slug, line + 1)),
                "target": target,
                "targetDocumentId": Value::Null,
                "targetBlockId": Value::Null,
            })
        })
        .collect::<Vec<_>>();
    let blocks = lines
        .iter()
        .enumerate()
        .map(|(index, line)| {
            let id = format!("blk_kb_{}_{}", seed.slug, index + 1);
            let relation_ids = relations
                .iter()
                .filter(|relation| relation.get("sourceBlockId").and_then(Value::as_str) == Some(&id))
                .filter_map(|relation| relation.get("id").cloned())
                .collect::<Vec<_>>();
            let trimmed = line.trim_start();
            let heading = trimmed.chars().take_while(|character| *character == '#').count();
            let mut block = if heading > 0 && heading <= 6 && trimmed.as_bytes().get(heading) == Some(&b' ') {
                json!({ "id": id, "type": "heading", "level": heading, "text": &trimmed[heading + 1..], "source": line })
            } else if let Some(text) = trimmed.strip_prefix("- ") {
                json!({ "id": id, "type": "bullet", "indent": line.len() - trimmed.len(), "text": text, "source": line })
            } else if line.is_empty() {
                json!({ "id": id, "type": "blank", "text": "", "source": line })
            } else {
                json!({ "id": id, "type": "paragraph", "text": line, "source": line })
            };
            if !relation_ids.is_empty() {
                block["relationIds"] = Value::Array(relation_ids);
            }
            block
        })
        .collect::<Vec<_>>();
    json!({
        "schemaVersion": 1,
        "body": { "type": "doc", "blocks": blocks },
        "properties": [
            { "id": format!("prop_kb_{}_type", seed.slug), "key": "type", "type": "text", "value": "knowledge" },
            { "id": format!("prop_kb_{}_product", seed.slug), "key": "product", "type": "text", "value": "open-clank" },
            { "id": format!("prop_kb_{}_version", seed.slug), "key": "seedVersion", "type": "number", "value": OPENCLANK_KNOWLEDGE_VERSION },
            { "id": format!("prop_kb_{}_source", seed.slug), "key": "sourcePath", "type": "text", "value": seed.source_path },
            { "id": format!("prop_kb_{}_builtin", seed.slug), "key": "builtin", "type": "checkbox", "value": true },
            { "id": format!("prop_kb_{}_tags", seed.slug), "key": "tags", "type": "tags", "value": ["openclank", "knowledge", "builtin"] }
        ],
        "relations": relations,
        "tags": ["builtin", "knowledge", "openclank"]
    })
    .to_string()
}

fn knowledge_seed_version(text: &str) -> Option<u64> {
    let record = serde_json::from_str::<Value>(text).ok()?;
    let properties = record.get("properties")?.as_array()?;
    let value = |key: &str| {
        properties.iter().find_map(|property| {
            (property.get("key").and_then(Value::as_str) == Some(key))
                .then(|| property.get("value"))
                .flatten()
        })
    };
    if value("builtin").and_then(Value::as_bool) != Some(true)
        || value("product").and_then(Value::as_str) != Some("open-clank")
    {
        return None;
    }
    Some(value("seedVersion").and_then(Value::as_u64).unwrap_or(0))
}

fn seed_openclank_knowledge(db: &Db) -> Result<usize, String> {
    let mut existing = db
        .list_docs()
        .map_err(|error| error.to_string())?
        .into_iter()
        .filter(|doc| doc.owner == "shared" && doc.workspace_id == "global")
        .map(|doc| (doc.name.clone(), doc))
        .collect::<BTreeMap<_, _>>();
    let mut changed = 0;
    for seed in OPENCLANK_KNOWLEDGE {
        let expected = knowledge_note(seed);
        if let Some(doc) = existing.get(seed.name) {
            if doc.text.as_deref() == Some(expected.as_str()) {
                continue;
            }
            let Some(version) = doc.text.as_deref().and_then(knowledge_seed_version) else {
                continue; // Preserve unrelated shared content with the same name.
            };
            if version > OPENCLANK_KNOWLEDGE_VERSION {
                continue;
            }
            match db
                .write_doc_scoped(&doc.id, &expected, Some(&doc.head), "shared", "global")
                .map_err(|error| error.to_string())?
            {
                WriteOutcome::Committed { .. } => changed += 1,
                WriteOutcome::Unchanged { .. } | WriteOutcome::Stale { .. } => {}
            }
            continue;
        }
        let doc = db
            .create_doc(
                "note",
                seed.name,
                &expected,
                Some("built-in OpenClank knowledge v1"),
            )
            .map_err(|error| error.to_string())?;
        existing.insert(seed.name.to_string(), doc);
        changed += 1;
    }
    Ok(changed)
}

fn links(text: &str) -> Vec<String> {
    let mut found = BTreeSet::new();
    let mut rest = text;
    while let Some(start) = rest.find("[[") {
        rest = &rest[start + 2..];
        let Some(end) = rest.find("]]") else { break };
        let target = rest[..end].split(['|', '#']).next().unwrap_or("").trim();
        if !target.is_empty() {
            found.insert(target.to_string());
        }
        rest = &rest[end + 2..];
    }
    found.into_iter().collect()
}

fn tags(text: &str) -> Vec<String> {
    let mut found = BTreeSet::new();
    for word in text.split_whitespace() {
        let tag = word
            .strip_prefix('#')
            .unwrap_or("")
            .trim_matches(|ch: char| !ch.is_alphanumeric() && ch != '-' && ch != '_' && ch != '/');
        if !tag.is_empty() && !tag.chars().all(|ch| ch.is_ascii_digit()) {
            found.insert(tag.to_string());
        }
    }
    found.into_iter().collect()
}

fn frontmatter(text: &str) -> BTreeMap<String, String> {
    let mut values = BTreeMap::new();
    let Some(body) = text.strip_prefix("---\n") else {
        return values;
    };
    let Some(end) = body.find("\n---") else {
        return values;
    };
    for line in body[..end].lines() {
        if let Some((key, value)) = line.split_once(':') {
            values.insert(
                key.trim().to_string(),
                value.trim().trim_matches(['\'', '"']).to_string(),
            );
        }
    }
    values
}

fn tasks(text: &str, document_id: &str) -> Vec<Value> {
    text.lines()
        .enumerate()
        .filter_map(|(index, line)| {
            let trimmed = line.trim_start();
            let (done, value) = if let Some(value) = trimmed.strip_prefix("- [ ] ") {
                (false, value)
            } else if let Some(value) = trimmed
                .strip_prefix("- [x] ")
                .or_else(|| trimmed.strip_prefix("- [X] "))
            {
                (true, value)
            } else {
                return None;
            };
            Some(json!({
                "id": format!("{document_id}:{}", index + 1),
                "line": index + 1,
                "done": done,
                "text": value,
            }))
        })
        .collect()
}

fn indexed_legacy_markdown(doc: &DocView) -> Value {
    let text = doc.text.as_deref().unwrap_or("");
    let frontmatter = frontmatter(text);
    let tasks = tasks(text, &doc.id);
    let course = frontmatter.get("course").cloned();
    let skill = frontmatter.get("skill").cloned();
    let treehouse = if course.is_some() || skill.is_some() || doc.kind.starts_with("treehouse-") {
        Some(json!({
            "id": doc.id,
            "course": course,
            "skill": skill,
            "prerequisite": frontmatter.get("depends_on"),
            "evidence_task_ids": tasks.iter().filter_map(|task| task.get("id")).collect::<Vec<_>>(),
            "source_document_id": doc.id,
            "source_head": doc.head,
        }))
    } else {
        None
    };
    json!({
        "id": doc.id,
        "recordSchemaVersion": doc.record_schema_version,
        "corpus": doc.corpus,
        "kind": doc.kind,
        "owner": doc.owner,
        "workspace_id": doc.workspace_id,
        "readOnly": doc.owner == "shared" && doc.workspace_id == "global",
        "name": doc.name,
        "head": doc.head,
        "ts": doc.ts,
        "hidden": doc.hidden,
        "deleted": doc.deleted,
        "text": doc.text,
        "content": doc.content,
        "links": links(text),
        "tags": tags(text),
        "frontmatter": frontmatter,
        "tasks": tasks,
        "treehouse": treehouse,
    })
}

fn block_line(block: &Value) -> String {
    if let Some(source) = block.get("source").and_then(Value::as_str) {
        return source.to_string();
    }
    let text = block.get("text").and_then(Value::as_str).unwrap_or("");
    let indent = " ".repeat(block.get("indent").and_then(Value::as_u64).unwrap_or(0) as usize);
    match block
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or("paragraph")
    {
        "heading" => format!(
            "{} {text}",
            "#".repeat(
                block
                    .get("level")
                    .and_then(Value::as_u64)
                    .unwrap_or(1)
                    .clamp(1, 6) as usize
            )
        ),
        "task" => format!(
            "{indent}- [{}] {text}",
            if block
                .get("checked")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                "x"
            } else {
                " "
            }
        ),
        "bullet" => format!("{indent}- {text}"),
        "ordered" => format!(
            "{indent}{}. {text}",
            block
                .get("number")
                .and_then(Value::as_u64)
                .unwrap_or(1)
                .max(1)
        ),
        "quote" => format!("> {text}"),
        "code-fence" => format!("```{text}"),
        "divider" => "---".to_string(),
        _ => text.to_string(),
    }
}

fn indexed_note(doc: &DocView) -> Value {
    let fail = |message: String| {
        json!({
            "id": doc.id, "recordSchemaVersion": doc.record_schema_version,
            "corpus": doc.corpus, "kind": doc.kind,
            "owner": doc.owner, "workspace_id": doc.workspace_id,
            "readOnly": doc.owner == "shared" && doc.workspace_id == "global",
            "name": doc.name, "head": doc.head, "ts": doc.ts,
            "hidden": doc.hidden, "deleted": doc.deleted, "content": doc.content,
            "text": "", "properties": {}, "propertyDefinitions": [], "frontmatter": {},
            "relations": [], "links": [], "tags": [], "blocks": [], "tasks": [], "treehouse": null,
            "format": "copal-note-v1", "storage": "database",
            "extensions": {},
            "rawPreserved": true, "note_error": message,
        })
    };
    let raw = doc.text.as_deref().unwrap_or("");
    let record = match serde_json::from_str::<Value>(raw) {
        Ok(Value::Object(record)) => record,
        Ok(_) => return fail("database note root is not an object".to_string()),
        Err(error) => return fail(error.to_string()),
    };
    if record.get("schemaVersion").and_then(Value::as_u64) != Some(1) {
        return fail("unsupported database note schema".to_string());
    }
    let Some(body) = record.get("body").and_then(Value::as_object) else {
        return fail("database note body is not a document tree".to_string());
    };
    if body.get("type").and_then(Value::as_str) != Some("doc") {
        return fail("database note body is not a document tree".to_string());
    }
    let Some(blocks) = body.get("blocks").and_then(Value::as_array) else {
        return fail("database note blocks are missing".to_string());
    };
    let text = blocks.iter().map(block_line).collect::<Vec<_>>().join("\n");
    let mut properties = serde_json::Map::new();
    let definitions = record
        .get("properties")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    for property in &definitions {
        if let Some(key) = property.get("key").and_then(Value::as_str) {
            properties.insert(
                key.to_string(),
                property.get("value").cloned().unwrap_or(Value::Null),
            );
        }
    }
    let relations = record
        .get("relations")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let links = relations
        .iter()
        .filter_map(|relation| {
            matches!(
                relation.get("kind").and_then(Value::as_str),
                Some("link" | "embed")
            )
            .then(|| {
                relation
                    .get("target")
                    .and_then(Value::as_str)
                    .map(ToString::to_string)
            })
            .flatten()
        })
        .collect::<BTreeSet<_>>();
    let tags = record
        .get("tags")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let extensions = record
        .get("extensions")
        .filter(|value| value.is_object())
        .cloned()
        .unwrap_or_else(|| json!({}));
    let task_values = blocks
        .iter()
        .enumerate()
        .filter_map(|(index, block)| {
            if block.get("type").and_then(Value::as_str) != Some("task") {
                return None;
            }
            let block_id = block.get("id").and_then(Value::as_str)?;
            Some(json!({
                "id": format!("{}:{block_id}", doc.id), "blockId": block_id, "line": index + 1,
                "done": block.get("checked").and_then(Value::as_bool).unwrap_or(false),
                "text": block.get("text").and_then(Value::as_str).unwrap_or(""),
            }))
        })
        .collect::<Vec<_>>();
    let course = properties.get("course").cloned();
    let skill = properties.get("skill").cloned();
    let treehouse = if course.is_some() || skill.is_some() {
        Some(json!({
            "id": doc.id, "course": course, "skill": skill, "prerequisite": properties.get("depends_on"),
            "evidence_task_ids": task_values.iter().filter_map(|task| task.get("id")).collect::<Vec<_>>(),
            "source_document_id": doc.id, "source_head": doc.head,
        }))
    } else {
        None
    };
    json!({
        "id": doc.id, "recordSchemaVersion": doc.record_schema_version,
        "corpus": doc.corpus, "kind": doc.kind,
        "owner": doc.owner, "workspace_id": doc.workspace_id,
        "readOnly": doc.owner == "shared" && doc.workspace_id == "global",
        "name": doc.name, "head": doc.head, "ts": doc.ts,
        "hidden": doc.hidden, "deleted": doc.deleted, "content": doc.content,
        "text": text, "properties": properties, "propertyDefinitions": definitions, "frontmatter": properties,
        "relations": relations, "links": links, "tags": tags, "blocks": blocks, "tasks": task_values,
        "treehouse": treehouse, "format": "copal-note-v1", "storage": "database",
        "extensions": extensions,
    })
}

fn indexed(doc: &DocView) -> Value {
    if matches!(doc.kind.as_str(), "note" | "wiki") {
        indexed_note(doc)
    } else {
        indexed_legacy_markdown(doc)
    }
}

fn outcome(value: WriteOutcome) -> Value {
    match value {
        WriteOutcome::Committed { view, new_change } => {
            json!({ "outcome": "committed", "new_change": new_change, "doc": view })
        }
        WriteOutcome::Unchanged { view } => json!({ "outcome": "unchanged", "doc": view }),
        WriteOutcome::Stale { view } => json!({ "outcome": "stale", "doc": view }),
    }
}

/// Pick the right database for the corpus declared in args.
fn pick_db<'a>(notes: &'a Db, wiki: Option<&'a Db>, args: &Value) -> Result<&'a Db, String> {
    let corpus = args
        .get("corpus")
        .and_then(Value::as_str)
        .unwrap_or("notes");
    match corpus {
        "wiki" => wiki.ok_or_else(|| "wiki corpus requested but no wiki store opened".to_string()),
        _ => Ok(notes),
    }
}

fn execute(db: &Db, wiki_db: Option<&Db>, op: &str, args: &Value) -> Result<Value, String> {
    match op {
        "status" => {
            let docs = db.list_docs().map_err(|error| error.to_string())?;
            let wiki_docs = wiki_db
                .map(|w| w.list_docs().map_err(|e| e.to_string()))
                .transpose()?
                .unwrap_or_default();
            let mut kinds = BTreeMap::<String, usize>::new();
            for doc in docs.iter().chain(wiki_docs.iter()) {
                *kinds.entry(doc.kind.clone()).or_default() += 1;
            }
            let schema_version = db.schema_version().map_err(|error| error.to_string())?;
            Ok(json!({
                "schema_version": schema_version,
                "documents": docs.len() + wiki_docs.len(),
                "kinds": kinds,
                "integrity_ok": true,
            }))
        }
        "import_vault" => {
            let (owner, workspace_id) = scope(args)?;
            let target = pick_db(db, wiki_db, args)?;
            let vault = PathBuf::from(required(args, "path")?);
            let planning = optional(args, "planning_path").map(PathBuf::from);
            let note_kind = optional(args, "note_kind").unwrap_or("markdown");
            let restore_ids = args
                .get("restore_ids")
                .cloned()
                .map(serde_json::from_value::<BTreeMap<String, ImportIdentity>>)
                .transpose()
                .map_err(|error| format!("invalid restore identity map: {error}"))?
                .unwrap_or_default();
            target
                .import_vault_scoped_as_with_ids(
                    &vault,
                    planning.as_deref(),
                    owner,
                    workspace_id,
                    note_kind,
                    &restore_ids,
                )
                .map(|stats| json!(stats))
                .map_err(|error| error.to_string())
        }
        "list" | "index" | "search" | "export_snapshot" => {
            let (owner, workspace_id) = scope(args)?;
            let query = optional(args, "query").unwrap_or("").to_lowercase();
            let kind = optional(args, "kind");
            let corpus = args
                .get("corpus")
                .and_then(Value::as_str)
                .unwrap_or("notes");

            // Collect docs from the appropriate store(s).
            let mut docs: Vec<_> = if corpus == "all" {
                let mut d = db
                    .list_docs_scoped(owner, workspace_id)
                    .map_err(|error| error.to_string())?;
                if let Some(wiki) = wiki_db {
                    d.extend(
                        wiki.list_docs_scoped(owner, workspace_id)
                            .map_err(|error| error.to_string())?,
                    );
                }
                d
            } else {
                let target = pick_db(db, wiki_db, args)?;
                target
                    .list_docs_scoped(owner, workspace_id)
                    .map_err(|error| error.to_string())?
            };

            docs.retain(|doc| kind.is_none_or(|value| doc.kind == value));

            if op == "list" {
                return Ok(json!({ "docs": docs }));
            }
            let docs = docs
                .iter()
                .map(indexed)
                .filter(|doc| {
                    query.is_empty()
                        || doc
                            .get("name")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_lowercase()
                            .contains(&query)
                        || doc
                            .get("text")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_lowercase()
                            .contains(&query)
                        || doc
                            .get("properties")
                            .unwrap_or(&Value::Null)
                            .to_string()
                            .to_lowercase()
                            .contains(&query)
                        || doc
                            .get("tags")
                            .unwrap_or(&Value::Null)
                            .to_string()
                            .to_lowercase()
                            .contains(&query)
                        || doc
                            .get("relations")
                            .unwrap_or(&Value::Null)
                            .to_string()
                            .to_lowercase()
                            .contains(&query)
                })
                .collect::<Vec<_>>();
            Ok(json!({ "docs": docs }))
        }
        "get" => {
            let (owner, workspace_id) = scope(args)?;
            let id = required(args, "id")?;
            // Try the requested store first; if corpus is unspecified, fall
            // through to the other store so cross-store lookups work.
            let target = pick_db(db, wiki_db, args)?;
            match target.get_doc_scoped(id, owner, workspace_id).map_err(|error| error.to_string())? {
                Some(doc) => Ok(indexed(&doc)),
                None => {
                    // Fallback: try the other store when corpus was explicit.
                    let fallback = if args.get("corpus").and_then(Value::as_str) == Some("wiki") {
                        db.get_doc_scoped(id, owner, workspace_id)
                    } else if let Some(wiki) = wiki_db {
                        wiki.get_doc_scoped(id, owner, workspace_id)
                    } else {
                        Ok(None)
                    };
                    fallback
                        .map_err(|error| error.to_string())?
                        .map(|doc| indexed(&doc))
                        .ok_or_else(|| "doc not found".to_string())
                }
            }
        }
        "trash" => {
            let (owner, workspace_id) = scope(args)?;
            let target = pick_db(db, wiki_db, args)?;
            let docs = target
                .list_deleted_docs_scoped(owner, workspace_id)
                .map_err(|error| error.to_string())?;
            Ok(json!({ "docs": docs }))
        }
        "create" => {
            let (owner, workspace_id) = scope(args)?;
            let target = pick_db(db, wiki_db, args)?;
            let doc = target
                .create_doc_scoped(
                    owner,
                    workspace_id,
                    optional(args, "kind").unwrap_or("markdown"),
                    required(args, "name")?,
                    args.get("content").and_then(Value::as_str).unwrap_or(""),
                    optional(args, "message"),
                )
                .map_err(|error| error.to_string())?;
            Ok(json!({ "outcome": "created", "doc": doc }))
        }
        "write" => {
            let (owner, workspace_id) = scope(args)?;
            let target = pick_db(db, wiki_db, args)?;
            target
                .write_doc_scoped(
                    required(args, "id")?,
                    args.get("content").and_then(Value::as_str).unwrap_or(""),
                    optional(args, "base"),
                    owner,
                    workspace_id,
                )
                .map(outcome)
                .map_err(|error| error.to_string())
        }
        "history" => {
            let (owner, workspace_id) = scope(args)?;
            let target = pick_db(db, wiki_db, args)?;
            target
                .history_scoped(required(args, "id")?, owner, workspace_id)
                .map_err(|error| error.to_string())
        }
        "checkpoint" => {
            let (owner, workspace_id) = scope(args)?;
            let target = pick_db(db, wiki_db, args)?;
            target
                .checkpoint_scoped(
                    required(args, "id")?,
                    optional(args, "message"),
                    owner,
                    workspace_id,
                )
                .map(|doc| json!({ "doc": doc }))
                .map_err(|error| error.to_string())
        }
        "rename" => {
            let (owner, workspace_id) = scope(args)?;
            let target = pick_db(db, wiki_db, args)?;
            target
                .rename_doc_scoped(
                    required(args, "id")?,
                    required(args, "name")?,
                    owner,
                    workspace_id,
                )
                .map(|doc| json!({ "doc": doc }))
                .map_err(|error| error.to_string())
        }
        "delete" => {
            let (owner, workspace_id) = scope(args)?;
            let target = pick_db(db, wiki_db, args)?;
            target
                .delete_doc_scoped(required(args, "id")?, owner, workspace_id)
                .map(|_| json!({ "deleted": true }))
                .map_err(|error| error.to_string())
        }
        "restore" => {
            let (owner, workspace_id) = scope(args)?;
            let target = pick_db(db, wiki_db, args)?;
            target
                .restore_doc_scoped(
                    required(args, "id")?,
                    required(args, "commit")?,
                    owner,
                    workspace_id,
                )
                .map(|doc| json!({ "doc": doc }))
                .map_err(|error| error.to_string())
        }
        "restore_deleted" => {
            let (owner, workspace_id) = scope(args)?;
            let target = pick_db(db, wiki_db, args)?;
            target
                .restore_deleted_doc_scoped(required(args, "id")?, owner, workspace_id)
                .map(|doc| json!({ "doc": doc }))
                .map_err(|error| error.to_string())
        }
        "diff" => {
            let (owner, workspace_id) = scope(args)?;
            let target = pick_db(db, wiki_db, args)?;
            target
                .history_scoped(required(args, "id")?, owner, workspace_id)
                .map_err(|error| error.to_string())?;
            target
                .diff(required(args, "from")?, required(args, "to")?)
                .map(|diff| json!({ "diff": diff }))
                .map_err(|error| error.to_string())
        }
        "ops" => db
            .ops(
                args.get("limit")
                    .and_then(Value::as_u64)
                    .unwrap_or(50)
                    .min(500) as usize,
                optional(args, "before"),
            )
            .map_err(|error| error.to_string()),
        "asset_path" => {
            let (owner, workspace_id) = scope(args)?;
            let target = pick_db(db, wiki_db, args)?;
            let doc = target
                .get_doc_scoped(required(args, "id")?, owner, workspace_id)
                .map_err(|error| error.to_string())?
                .ok_or_else(|| "asset not found".to_string())?;
            let Content::Asset { hash, ext, size } = doc.content else {
                return Err("doc is not an asset".to_string());
            };
            Ok(json!({ "path": target.asset_file(&hash, &ext), "name": doc.name, "size": size }))
        }
        _ => Err(format!("unknown operation: {op}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn note_view(text: &str) -> DocView {
        DocView {
            id: "NOTE1".to_string(),
            record_schema_version: 1,
            corpus: "notes".to_string(),
            kind: "note".to_string(),
            owner: "local".to_string(),
            workspace_id: "default".to_string(),
            name: "Native".to_string(),
            head: "head1".to_string(),
            ts: 1,
            hidden: false,
            deleted: false,
            content: Content::Blob {
                hash: "hash".to_string(),
            },
            text: Some(text.to_string()),
        }
    }

    #[test]
    fn indexes_structured_note_fields_and_tasks() {
        let note = note_view(
            r##"{"schemaVersion":1,"body":{"type":"doc","blocks":[{"id":"blk_1","type":"heading","source":"# Native","text":"Native","level":1},{"id":"blk_2","type":"task","source":"  - [ ] prove it","text":"prove it","indent":2,"checked":false,"relationIds":["rel_1"]}]},"properties":[{"id":"prop_1","key":"status","type":"text","value":"active"},{"id":"prop_2","key":"course","type":"text","value":"OpenClank"}],"relations":[{"id":"rel_1","kind":"link","origin":"body","sourceBlockId":"blk_2","target":"Target","targetDocumentId":"TARGET","targetBlockId":null}],"tags":["native"]}"##,
        );

        let indexed = indexed(&note);

        assert_eq!(indexed["text"], "# Native\n  - [ ] prove it");
        assert_eq!(indexed["properties"]["status"], "active");
        assert_eq!(indexed["links"][0], "Target");
        assert_eq!(indexed["tasks"][0]["id"], "NOTE1:blk_2");
        assert_eq!(indexed["treehouse"]["course"], "OpenClank");
        assert_eq!(indexed["storage"], "database");
    }

    #[test]
    fn indexes_wiki_corpus_and_interchange_extensions() {
        let mut wiki = note_view(
            r##"{"schemaVersion":1,"body":{"type":"doc","blocks":[{"id":"blk_1","type":"paragraph","source":"Wiki","text":"Wiki"}]},"properties":[],"relations":[],"extensions":{"interchange":{"source":"Wiki\n","modified":false}}}"##,
        );
        wiki.kind = "wiki".to_string();
        wiki.corpus = "wiki".to_string();

        let indexed = indexed(&wiki);

        assert_eq!(indexed["corpus"], "wiki");
        assert_eq!(indexed["extensions"]["interchange"]["source"], "Wiki\n");
    }

    #[test]
    fn rejects_malformed_note_without_exposing_raw_json() {
        let indexed = indexed(&note_view("{bad json"));
        assert_eq!(indexed["text"], "");
        assert_eq!(indexed["rawPreserved"], true);
        assert!(indexed["note_error"]
            .as_str()
            .is_some_and(|message| !message.is_empty()));
    }

    #[test]
    fn openclank_knowledge_seed_is_database_native_and_idempotent() {
        let dir = std::env::temp_dir().join(format!("copal-knowledge-seed-{}", ulid::Ulid::new()));
        let db = Db::open(&dir).unwrap();

        assert_eq!(
            seed_openclank_knowledge(&db).unwrap(),
            OPENCLANK_KNOWLEDGE.len()
        );
        assert_eq!(seed_openclank_knowledge(&db).unwrap(), 0);
        let seeded = db
            .list_docs_scoped("alice", "home")
            .unwrap()
            .into_iter()
            .filter(|doc| doc.name.starts_with("OpenClank/"))
            .collect::<Vec<_>>();
        assert_eq!(seeded.len(), OPENCLANK_KNOWLEDGE.len());
        assert!(seeded
            .iter()
            .all(|doc| doc.kind == "note" && doc.owner == "shared"));
        let start = seeded
            .iter()
            .find(|doc| doc.name.ends_with("Start Here"))
            .unwrap();
        let indexed_view = indexed(start);
        assert_eq!(indexed_view["storage"], "database");
        assert_eq!(indexed_view["properties"]["builtin"], true);
        assert!(indexed_view["links"]
            .as_array()
            .is_some_and(|links| links.len() == 4));

        let mut stale = serde_json::from_str::<Value>(start.text.as_deref().unwrap()).unwrap();
        let version = stale["properties"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .find(|property| property["key"] == "seedVersion")
            .unwrap();
        version["value"] = json!(0);
        let outcome = db
            .write_doc_scoped(
                &start.id,
                &stale.to_string(),
                Some(&start.head),
                "shared",
                "global",
            )
            .unwrap();
        assert!(matches!(outcome, WriteOutcome::Committed { .. }));
        assert_eq!(seed_openclank_knowledge(&db).unwrap(), 1);
        let upgraded = db.get_doc(&start.id).unwrap().unwrap();
        assert_eq!(indexed(&upgraded)["properties"]["seedVersion"], 2);
        assert_eq!(seed_openclank_knowledge(&db).unwrap(), 0);
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let data_dir = std::env::var_os("COPAL_DATA_DIR")
        .map(PathBuf::from)
        .or_else(|| std::env::args_os().nth(1).map(PathBuf::from))
        .ok_or("COPAL_DATA_DIR is required")?;
    let db = Db::open(&data_dir).map_err(|error| io::Error::other(error.to_string()))?;
    // Optional second store for wiki corpus (separate Redb file).
    let wiki_db = std::env::var_os("COPAL_WIKI_DATA_DIR")
        .map(PathBuf::from)
        .and_then(|dir| {
            Db::open_with_name(&dir, "copal-wiki")
                .map_err(|error| io::Error::other(error.to_string()))
                .ok()
        });
    if let Some(ref wiki) = wiki_db {
        seed_wiki_pages(wiki).map_err(io::Error::other)?;
        eprintln!("[copal-bridge] wiki store opened alongside notes store");
    }
    seed_openclank_knowledge(&db).map_err(io::Error::other)?;
    let stdin = io::stdin();
    let mut stdout = io::stdout().lock();
    for line in stdin.lock().lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let request = serde_json::from_str::<Value>(&line);
        let id = request
            .as_ref()
            .ok()
            .and_then(|value| value.get("id"))
            .cloned()
            .unwrap_or(Value::Null);
        let response = match request {
            Ok(value) => {
                let op = value.get("op").and_then(Value::as_str).unwrap_or("");
                let args = value.get("args").unwrap_or(&Value::Null);
                match execute(&db, wiki_db.as_ref(), op, args) {
                    Ok(result) => json!({ "id": id, "ok": true, "result": result }),
                    Err(error) => json!({ "id": id, "ok": false, "error": error }),
                }
            }
            Err(error) => {
                json!({ "id": id, "ok": false, "error": format!("invalid request: {error}") })
            }
        };
        serde_json::to_writer(&mut stdout, &response)?;
        stdout.write_all(b"\n")?;
        stdout.flush()?;
    }
    Ok(())
}
