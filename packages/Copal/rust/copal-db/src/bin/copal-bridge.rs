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

const OPENCLANK_KNOWLEDGE_VERSION: u64 = 3;
const OPENCLANK_KNOWLEDGE: &[KnowledgeSeed] = &[
    KnowledgeSeed {
        slug: "start",
        name: "OpenClank/Start Here",
        source_path: "README.md",
        body: r##"# Open Clank

Open Clank is a self-hosted place to talk with models, let agents use tools, and keep the work beside the conversation.

## Start here
- [[OpenClank/Chat, Agent, and Identity]] — chats, agents, and model choice
- [[OpenClank/Copal Notes and Timeline]] — notes, history, planning, and imports
- [[OpenClank/Memory and Knowledge]] — what lasts and who can see it
- [[OpenClank/Architecture and Data]] — where the app and its data live

Your account owns its model catalogue, settings, and private Copal workspace. The notes in this folder ship with the app, are shared read-only, and update without replacing your own notes.

#openclank #builtin"##,
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

**Chat** is the plain conversation lane. **Agent** is the working lane: it can use the tools offered by the selected model and endpoint. Compare is for looking at more than one model answer side by side.

The model picker changes the engine, not the person you are talking to. Models and endpoints belong to the signed-in account, so a new account starts with its own empty catalogue instead of broken copies of somebody else's connections.

If a model cannot do the tool work a turn needs, Open Clank should say so plainly. It must not quietly borrow another user's endpoint or pretend a text-only model used tools.

Return to [[OpenClank/Start Here]].

#openclank #agent #identity"##,
        links: &["OpenClank/Start Here"],
    },
    KnowledgeSeed {
        slug: "memory",
        name: "OpenClank/Memory and Knowledge",
        source_path: "docs/memory-architecture.md",
        body: r##"# Memory and Knowledge

Conversation history keeps a chat understandable. Copal keeps durable notes you can inspect, edit, export, restore, or trash. They are related, but one does not silently become the other.

Private notes are keyed to both account and workspace. Knowing another document ID is not enough to read or change it. Built-in Open Clank notes are the exception: everyone can read them, nobody edits them in place, and a same-named personal note stays yours.

Imported vaults follow the same owner boundary. The Brain Vault corpus in this installation is private runtime data: it is visible only to its owner, not built into Open Clank, and not checked into Git.

Return to [[OpenClank/Start Here]].

#openclank #memory #knowledge"##,
        links: &["OpenClank/Start Here"],
    },
    KnowledgeSeed {
        slug: "copal",
        name: "OpenClank/Copal Notes and Timeline",
        source_path: "routes/copal_routes.py",
        body: r##"# Copal Notes and Timeline

Copal is Open Clank's notes workspace. A note keeps the same ID and remembers its edits, links, properties, trash, and recovery. If an old browser tab tries to overwrite newer work, Copal refuses the stale save.

Timeline uses the same owner-scoped Copal records as Notes. Canonical event files live under `.events/`; that folder is hidden in the normal file view unless you ask to show dot-folders. Wiki records use `.wik/` and follow the same default.

Obsidian-style ZIP import and export are account-scoped. Compatibility files and attachments are kept as data; importing them does not run scripts or install plugins.

Return to [[OpenClank/Start Here]].

#openclank #copal #notes #timeline"##,
        links: &["OpenClank/Start Here"],
    },
    KnowledgeSeed {
        slug: "architecture",
        name: "OpenClank/Architecture and Data",
        source_path: "src/openclank/copal_bridge.py",
        body: r##"# Architecture and Data

The browser talks to the Python application. Copal note work goes through a small Rust helper into Redb. The main app database keeps accounts, sessions, model endpoints, and settings; Copal keeps notes, their edit history, and attachments.

In this checkout, Copal's debug data lives under `packages/Copal/db/`. That directory is runtime state, not source material. Back it up before migrations and never publish a personal vault, endpoint secret, or session token.

If you are looking through the source: `routes/copal_routes.py` checks the signed-in user, `src/openclank/copal_bridge.py` runs the helper, `packages/Copal/rust/copal-db/` stores notes, and `static/` holds the browser code.

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
        name: ".wik/What Is Wiki",
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
        name: ".wik/Creating and Linking Memes",
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
        name: ".wik/Story Navigation",
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
        name: ".wik/Fields and Properties",
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
        name: ".wik/Wiki vs Notes",
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

struct LegacyWikiSeed {
    name: &'static str,
    target: &'static str,
    blob: &'static str,
}

// Exact fingerprints from the bundled pre-v2 Wiki seeds. Name alone is not
// enough to claim a shared document: an unrelated same-name page stays data.
const LEGACY_WIKI_SEEDS: &[LegacyWikiSeed] = &[
    LegacyWikiSeed {
        name: "Wiki/Creating and Linking Tiddlers",
        target: ".wik/Creating and Linking Memes",
        blob: "032b23b359978236a0f75228a9e3c30df6fce1f5012f3b7f9f685bb81841bc6a",
    },
    LegacyWikiSeed {
        name: "Wiki/Fields and Properties",
        target: ".wik/Fields and Properties",
        blob: "79a26b736bbc6fc613e9e350202c123baed1c028206451a14ed20d1fbdfd1bc0",
    },
    LegacyWikiSeed {
        name: "Wiki/Story Navigation",
        target: ".wik/Story Navigation",
        blob: "eb4d7413bd0afd5e4f7f6b9f1414400eec909134f4565157ede23e685adda6a8",
    },
    LegacyWikiSeed {
        name: "Wiki/What Is Wiki",
        target: ".wik/What Is Wiki",
        blob: "93d36071f83856a09881a35d18b3bfb0031075a3ee2263ba2c9fb91ab7164481",
    },
    LegacyWikiSeed {
        name: "Wiki/Wiki vs Notes",
        target: ".wik/Wiki vs Notes",
        blob: "b3ada6681af29a23d943f63421da091157ccae20d6dadea3e677cf631813b91d",
    },
];

fn legacy_wiki_seed(doc: &DocView) -> Option<&'static LegacyWikiSeed> {
    if doc.kind != "wiki" || doc.owner != "shared" || doc.workspace_id != "global" {
        return None;
    }
    let Content::Blob { hash } = &doc.content else {
        return None;
    };
    LEGACY_WIKI_SEEDS
        .iter()
        .find(|seed| doc.name == seed.name && hash == seed.blob)
}

fn legacy_event_tail(name: &str) -> Option<&str> {
    let prefix = name.get(..7)?;
    if !prefix.eq_ignore_ascii_case("events/") {
        return None;
    }
    name.get(7..).filter(|tail| !tail.is_empty())
}

fn has_event_frontmatter(text: Option<&str>) -> bool {
    let mut lines = text.unwrap_or_default().lines();
    if lines.next().map(str::trim) != Some("---") {
        return false;
    }
    let mut event = false;
    for line in lines {
        let line = line.trim();
        if line == "---" {
            return event;
        }
        let Some((key, value)) = line.split_once(':') else {
            continue;
        };
        if key.trim() == "copal_type"
            && value
                .trim()
                .trim_matches(|character| character == '\"' || character == '\'')
                == "event"
        {
            event = true;
        }
    }
    false
}

fn hidden_namespace_target(doc: &DocView) -> Option<String> {
    if let Some(seed) = legacy_wiki_seed(doc) {
        return Some(seed.target.to_string());
    }
    if doc.kind == "copal-event"
        || (matches!(doc.kind.as_str(), "markdown" | "note")
            && has_event_frontmatter(doc.text.as_deref()))
    {
        return legacy_event_tail(&doc.name).map(|tail| format!(".events/{tail}"));
    }
    if doc.kind == "wiki" {
        return doc
            .name
            .strip_prefix("Wiki/")
            .filter(|tail| !tail.is_empty())
            .map(|tail| format!(".wik/{tail}"));
    }
    None
}

/// Move only Copal's known internal namespaces. Preflight all names first;
/// interrupted runs are safe because each rename is versioned and idempotent.
fn migrate_hidden_namespaces(db: &Db) -> Result<usize, String> {
    let docs = db.list_docs().map_err(|error| error.to_string())?;
    let legacy_ids = docs
        .iter()
        .filter(|doc| !doc.builtin && legacy_wiki_seed(doc).is_some())
        .map(|doc| doc.id.clone())
        .collect::<Vec<_>>();
    let moves = docs
        .iter()
        .filter_map(|doc| hidden_namespace_target(doc).map(|name| (doc, name)))
        .collect::<Vec<_>>();

    for (doc, target) in &moves {
        if docs.iter().any(|other| {
            other.id != doc.id
                && other.owner == doc.owner
                && other.workspace_id == doc.workspace_id
                && other.corpus == doc.corpus
                && other.name == *target
        }) {
            return Err(format!(
                "cannot migrate {} to {target}: target already exists in this scope",
                doc.name
            ));
        }
    }

    for id in legacy_ids {
        db.claim_builtin_seed_doc(&id)
            .map_err(|error| error.to_string())?;
    }
    for (doc, target) in &moves {
        db.rename_doc(&doc.id, target)
            .map_err(|error| error.to_string())?;
    }
    Ok(moves.len())
}

fn seed_wiki_pages(db: &Db) -> Result<usize, String> {
    let mut existing = db
        .list_docs()
        .map_err(|error| error.to_string())?
        .into_iter()
        .filter(|doc| doc.owner == "shared" && doc.workspace_id == "global" && doc.kind == "wiki")
        .collect::<Vec<_>>();
    let mut changed = 0;
    for seed in WIKI_SEEDS {
        if let Some(doc) = existing
            .iter()
            .find(|doc| doc.builtin && doc.name == seed.name)
            .cloned()
        {
            if doc.text.as_deref() == Some(seed.body) {
                continue;
            }
            // Explicitly marked seed: update it in place (idempotent upgrade).
            match db
                .write_doc(&doc.id, seed.body, Some(&doc.head))
                .map_err(|error| error.to_string())?
            {
                WriteOutcome::Committed { .. } => changed += 1,
                WriteOutcome::Unchanged { .. } | WriteOutcome::Stale { .. } => {}
            }
            continue;
        }

        if let Some(doc) = existing
            .iter()
            .find(|doc| doc.name == seed.name && doc.text.as_deref() == Some(seed.body))
            .cloned()
        {
            // Exact legacy bundle content is the only safe unmarked record to claim.
            let promoted = db
                .claim_builtin_seed_doc(&doc.id)
                .map_err(|error| error.to_string())?;
            if let Some(current) = existing.iter_mut().find(|item| item.id == doc.id) {
                *current = promoted;
            }
            changed += 1;
            continue;
        }

        // An unrelated shared same-name record is not a seed. Preserve it and
        // create a separately marked bundled copy; scoped listing will expose
        // the builtin deterministically.
        let doc = db
            .create_builtin_seed_doc(
                "wiki",
                seed.name,
                seed.body,
                Some("built-in Wiki how-to seed"),
            )
            .map_err(|error| error.to_string())?;
        existing.push(doc);
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
        .filter(|doc| doc.owner == "shared" && doc.workspace_id == "global" && doc.kind == "note")
        .collect::<Vec<_>>();
    let mut changed = 0;
    for seed in OPENCLANK_KNOWLEDGE {
        let expected = knowledge_note(seed);
        if let Some(doc) = existing
            .iter()
            .find(|doc| doc.builtin && doc.name == seed.name)
            .cloned()
        {
            if doc.text.as_deref() == Some(expected.as_str()) {
                continue;
            }
            if doc
                .text
                .as_deref()
                .and_then(knowledge_seed_version)
                .is_some_and(|version| version > OPENCLANK_KNOWLEDGE_VERSION)
            {
                continue;
            }
            match db
                .write_doc(&doc.id, &expected, Some(&doc.head))
                .map_err(|error| error.to_string())?
            {
                WriteOutcome::Committed { .. } => changed += 1,
                WriteOutcome::Unchanged { .. } | WriteOutcome::Stale { .. } => {}
            }
            continue;
        }

        if let Some(doc) = existing
            .iter()
            .find(|doc| {
                doc.name == seed.name
                    && (doc.text.as_deref() == Some(expected.as_str())
                        || doc
                            .text
                            .as_deref()
                            .and_then(knowledge_seed_version)
                            .is_some())
            })
            .cloned()
        {
            // Exact generated content or the embedded OpenClank seed metadata
            // identifies a legacy bundled record. Claim before any update.
            let promoted = db
                .claim_builtin_seed_doc(&doc.id)
                .map_err(|error| error.to_string())?;
            let future = promoted
                .text
                .as_deref()
                .and_then(knowledge_seed_version)
                .is_some_and(|version| version > OPENCLANK_KNOWLEDGE_VERSION);
            let mut final_doc = promoted.clone();
            if promoted.text.as_deref() != Some(expected.as_str()) && !future {
                if let WriteOutcome::Committed { view, .. } = db
                    .write_doc(&promoted.id, &expected, Some(&promoted.head))
                    .map_err(|error| error.to_string())?
                {
                    final_doc = view;
                }
            }
            if let Some(current) = existing.iter_mut().find(|item| item.id == doc.id) {
                *current = final_doc;
            }
            changed += 1;
            continue;
        }

        let doc = db
            .create_builtin_seed_doc(
                "note",
                seed.name,
                &expected,
                Some("built-in OpenClank knowledge v1"),
            )
            .map_err(|error| error.to_string())?;
        existing.push(doc);
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
        "builtin": doc.builtin,
        "readOnly": doc.builtin,
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
            "builtin": doc.builtin, "readOnly": doc.builtin,
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
        "builtin": doc.builtin, "readOnly": doc.builtin,
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
        "status" | "scoped_status" => {
            let requested_scope = if op == "scoped_status" {
                Some(scope(args)?)
            } else {
                None
            };
            let docs = match requested_scope {
                Some((owner, workspace_id)) => db.list_docs_scoped(owner, workspace_id),
                None => db.list_docs(),
            }
            .map_err(|error| error.to_string())?;
            let wiki_docs = wiki_db
                .map(|wiki| {
                    match requested_scope {
                        Some((owner, workspace_id)) => wiki.list_docs_scoped(owner, workspace_id),
                        None => wiki.list_docs(),
                    }
                    .map_err(|error| error.to_string())
                })
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
        "rename_owner" => {
            let old_owner = required(args, "old_owner")?;
            let new_owner = required(args, "new_owner")?;
            db.preflight_rename_owner(old_owner, new_owner)
                .map_err(|error| error.to_string())?;
            if let Some(wiki) = wiki_db {
                wiki.preflight_rename_owner(old_owner, new_owner)
                    .map_err(|error| error.to_string())?;
            }
            let notes = db
                .rename_owner(old_owner, new_owner)
                .map_err(|error| error.to_string())?;
            let wiki = if let Some(wiki) = wiki_db {
                match wiki.rename_owner(old_owner, new_owner) {
                    Ok(count) => count,
                    Err(error) => {
                        if let Err(rollback) = db.rename_owner(new_owner, old_owner) {
                            return Err(format!(
                                "wiki owner rename failed: {error}; notes rollback failed: {rollback}"
                            ));
                        }
                        return Err(error.to_string());
                    }
                }
            } else {
                0
            };
            Ok(json!({"notes": notes, "wiki": wiki, "documents": notes + wiki}))
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

            if op == "export_snapshot" {
                docs.retain(|doc| !doc.builtin);
            }

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
            match target
                .get_doc_scoped(id, owner, workspace_id)
                .map_err(|error| error.to_string())?
            {
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
                .diff_scoped(
                    required(args, "id")?,
                    required(args, "from")?,
                    required(args, "to")?,
                    owner,
                    workspace_id,
                )
                .map(|diff| json!({ "diff": diff }))
                .map_err(|error| error.to_string())
        }
        "ops" => {
            let (owner, workspace_id) = scope(args)?;
            db.ops_scoped(
                args.get("limit")
                    .and_then(Value::as_u64)
                    .unwrap_or(50)
                    .min(500) as usize,
                optional(args, "before"),
                owner,
                workspace_id,
            )
            .map_err(|error| error.to_string())
        }
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
            builtin: false,
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
            .all(|doc| doc.kind == "note" && doc.owner == "shared" && doc.builtin));
        let start = seeded
            .iter()
            .find(|doc| doc.name.ends_with("Start Here"))
            .unwrap();
        let indexed_view = indexed(start);
        assert_eq!(indexed_view["storage"], "database");
        assert_eq!(indexed_view["builtin"], true);
        assert_eq!(indexed_view["readOnly"], true);
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
        assert_eq!(
            indexed(&upgraded)["properties"]["seedVersion"],
            OPENCLANK_KNOWLEDGE_VERSION
        );
        assert_eq!(seed_openclank_knowledge(&db).unwrap(), 0);
    }

    #[test]
    fn hidden_namespace_migration_is_scoped_versioned_and_idempotent() {
        let root = std::env::temp_dir().join(format!("copal-hidden-names-{}", ulid::Ulid::new()));
        let db = Db::open(&root).unwrap();
        let event = db
            .create_doc_scoped(
                "alice",
                "home",
                "copal-event",
                "events/Launch.md",
                "{}",
                None,
            )
            .unwrap();
        let markdown_event = db
            .create_doc_scoped(
                "alice",
                "home",
                "markdown",
                "Events/Imported.md",
                "---\ncopal_type: \"event\"\n---\nImported event.\n",
                None,
            )
            .unwrap();
        let wiki = db
            .create_doc_scoped("alice", "home", "wiki", "Wiki/Launch", "# Launch", None)
            .unwrap();
        db.create_doc_scoped(
            "bob",
            "home",
            "markdown",
            "events/Personal.md",
            "leave this alone",
            None,
        )
        .unwrap();

        assert_eq!(migrate_hidden_namespaces(&db).unwrap(), 3);
        assert_eq!(migrate_hidden_namespaces(&db).unwrap(), 0);

        let migrated_event = db.get_doc(&event.id).unwrap().unwrap();
        let migrated_markdown_event = db.get_doc(&markdown_event.id).unwrap().unwrap();
        let migrated_wiki = db.get_doc(&wiki.id).unwrap().unwrap();
        assert_eq!(migrated_event.name, ".events/Launch.md");
        assert_eq!(migrated_markdown_event.name, ".events/Imported.md");
        assert_eq!(migrated_markdown_event.kind, "markdown");
        assert_eq!(migrated_wiki.name, ".wik/Launch");
        assert_ne!(migrated_event.head, event.head);
        assert_ne!(migrated_wiki.head, wiki.head);
        assert_eq!(
            db.list_docs_scoped("bob", "home")
                .unwrap()
                .into_iter()
                .find(|doc| doc.kind == "markdown")
                .unwrap()
                .name,
            "events/Personal.md"
        );
    }

    #[test]
    fn legacy_wiki_seed_alias_requires_the_exact_bundled_blob() {
        let mut wiki = note_view("");
        wiki.kind = "wiki".to_string();
        wiki.corpus = "wiki".to_string();
        wiki.owner = "shared".to_string();
        wiki.workspace_id = "global".to_string();
        wiki.name = "Wiki/Creating and Linking Tiddlers".to_string();
        wiki.content = Content::Blob {
            hash: LEGACY_WIKI_SEEDS[0].blob.to_string(),
        };
        assert_eq!(
            hidden_namespace_target(&wiki).as_deref(),
            Some(".wik/Creating and Linking Memes")
        );

        wiki.content = Content::Blob {
            hash: "unrelated".to_string(),
        };
        assert_eq!(
            hidden_namespace_target(&wiki).as_deref(),
            Some(".wik/Creating and Linking Tiddlers")
        );
    }

    #[test]
    fn hidden_namespace_migration_refuses_collisions_before_writing() {
        let root =
            std::env::temp_dir().join(format!("copal-hidden-collision-{}", ulid::Ulid::new()));
        let db = Db::open(&root).unwrap();
        let legacy = db
            .create_doc_scoped("alice", "home", "wiki", "Wiki/Same", "old", None)
            .unwrap();
        db.create_doc_scoped("alice", "home", "wiki", ".wik/Same", "new", None)
            .unwrap();

        assert!(migrate_hidden_namespaces(&db)
            .unwrap_err()
            .contains("target already exists"));
        assert_eq!(db.get_doc(&legacy.id).unwrap().unwrap().name, "Wiki/Same");
    }

    #[test]
    fn seeders_claim_exact_legacy_content_and_preserve_unrelated_same_names() {
        let root = std::env::temp_dir().join(format!("copal-seed-claim-{}", ulid::Ulid::new()));
        let notes = Db::open(&root.join("notes")).unwrap();
        let wiki = Db::open(&root.join("wiki")).unwrap();

        let exact_note = knowledge_note(&OPENCLANK_KNOWLEDGE[0]);
        let legacy_note = notes
            .create_doc("note", OPENCLANK_KNOWLEDGE[0].name, &exact_note, None)
            .unwrap();
        let unrelated_note = notes
            .create_doc(
                "note",
                OPENCLANK_KNOWLEDGE[1].name,
                "unrelated shared note",
                None,
            )
            .unwrap();
        let legacy_wiki = wiki
            .create_doc("wiki", WIKI_SEEDS[0].name, WIKI_SEEDS[0].body, None)
            .unwrap();
        let unrelated_wiki = wiki
            .create_doc("wiki", WIKI_SEEDS[1].name, "unrelated shared wiki", None)
            .unwrap();

        assert_eq!(
            seed_openclank_knowledge(&notes).unwrap(),
            OPENCLANK_KNOWLEDGE.len()
        );
        assert_eq!(seed_wiki_pages(&wiki).unwrap(), WIKI_SEEDS.len());

        let claimed_note = notes.get_doc(&legacy_note.id).unwrap().unwrap();
        assert!(claimed_note.builtin);
        assert_eq!(claimed_note.record_schema_version, 2);
        assert_eq!(claimed_note.head, legacy_note.head);
        let claimed_wiki = wiki.get_doc(&legacy_wiki.id).unwrap().unwrap();
        assert!(claimed_wiki.builtin);
        assert_eq!(claimed_wiki.head, legacy_wiki.head);

        assert_eq!(
            notes
                .list_docs()
                .unwrap()
                .into_iter()
                .filter(|doc| doc.name == OPENCLANK_KNOWLEDGE[1].name)
                .count(),
            2
        );
        assert_eq!(
            notes
                .get_doc(&unrelated_note.id)
                .unwrap()
                .unwrap()
                .text
                .as_deref(),
            Some("unrelated shared note")
        );
        assert_eq!(
            wiki.list_docs()
                .unwrap()
                .into_iter()
                .filter(|doc| doc.name == WIKI_SEEDS[1].name)
                .count(),
            2
        );
        assert_eq!(
            wiki.get_doc(&unrelated_wiki.id)
                .unwrap()
                .unwrap()
                .text
                .as_deref(),
            Some("unrelated shared wiki")
        );

        assert_eq!(seed_openclank_knowledge(&notes).unwrap(), 0);
        assert_eq!(seed_wiki_pages(&wiki).unwrap(), 0);
    }

    #[test]
    fn builtin_seeds_are_read_only_shadowable_and_excluded_from_export() {
        let root = std::env::temp_dir().join(format!("copal-seed-export-{}", ulid::Ulid::new()));
        let notes = Db::open(&root.join("notes")).unwrap();
        let wiki = Db::open(&root.join("wiki")).unwrap();
        seed_openclank_knowledge(&notes).unwrap();
        seed_wiki_pages(&wiki).unwrap();
        notes
            .create_doc_scoped("alice", "home", "markdown", "Alice.md", "private", None)
            .unwrap();
        let shadow = notes
            .create_doc_scoped(
                "alice",
                "home",
                "note",
                OPENCLANK_KNOWLEDGE[0].name,
                r#"{"schemaVersion":1,"body":{"type":"doc","blocks":[]},"properties":[],"relations":[]}"#,
                None,
            )
            .unwrap();

        let index = execute(
            &notes,
            Some(&wiki),
            "index",
            &json!({"owner": "alice", "workspace_id": "home", "corpus": "all"}),
        )
        .unwrap();
        let indexed_docs = index["docs"].as_array().unwrap();
        assert!(indexed_docs.iter().any(|doc| {
            doc["id"] == shadow.id && doc["builtin"] == false && doc["readOnly"] == false
        }));
        assert!(!indexed_docs
            .iter()
            .any(|doc| doc["builtin"] == true && doc["name"] == OPENCLANK_KNOWLEDGE[0].name));
        assert!(indexed_docs
            .iter()
            .any(|doc| doc["builtin"] == true && doc["readOnly"] == true));

        let export = execute(
            &notes,
            Some(&wiki),
            "export_snapshot",
            &json!({"owner": "alice", "workspace_id": "home", "corpus": "all"}),
        )
        .unwrap();
        let exported_docs = export["docs"].as_array().unwrap();
        assert_eq!(exported_docs.len(), 2);
        assert!(exported_docs
            .iter()
            .all(|doc| doc["builtin"] == false && doc["readOnly"] == false));
    }

    #[test]
    fn scoped_status_does_not_count_another_owners_documents() {
        let root = std::env::temp_dir().join(format!("copal-scoped-status-{}", ulid::Ulid::new()));
        let notes = Db::open(&root.join("notes")).unwrap();
        let wiki = Db::open(&root.join("wiki")).unwrap();
        notes
            .create_doc_scoped("alice", "home", "markdown", "Alice.md", "Alice", None)
            .unwrap();
        notes
            .create_doc_scoped("bob", "home", "planning", "Bob.json", "{}", None)
            .unwrap();
        wiki.create_doc_scoped("alice", "home", "wiki", "Alice Wiki.md", "Wiki", None)
            .unwrap();

        let alice = execute(
            &notes,
            Some(&wiki),
            "scoped_status",
            &json!({"owner": "alice", "workspace_id": "home"}),
        )
        .unwrap();
        let bob = execute(
            &notes,
            Some(&wiki),
            "scoped_status",
            &json!({"owner": "bob", "workspace_id": "home"}),
        )
        .unwrap();

        assert_eq!(alice["documents"], 2);
        assert_eq!(alice["kinds"], json!({"markdown": 1, "wiki": 1}));
        assert_eq!(bob["documents"], 1);
        assert_eq!(bob["kinds"], json!({"planning": 1}));
    }

    #[test]
    fn operation_log_hides_other_owner_document_names() {
        let root = std::env::temp_dir().join(format!("copal-scoped-ops-{}", ulid::Ulid::new()));
        let notes = Db::open(&root.join("notes")).unwrap();
        let alice = notes
            .create_doc_scoped(
                "alice",
                "home",
                "markdown",
                "Alice Private Plan.md",
                "first",
                None,
            )
            .unwrap();
        notes
            .create_doc_scoped(
                "bob",
                "home",
                "markdown",
                "Bob Secret Acquisition.md",
                "secret",
                None,
            )
            .unwrap();
        notes
            .write_doc_scoped(&alice.id, "second", Some(&alice.head), "alice", "home")
            .unwrap();

        let operations = execute(
            &notes,
            None,
            "ops",
            &json!({"owner": "alice", "workspace_id": "home", "limit": 50}),
        )
        .unwrap();
        let descriptions = operations["ops"]
            .as_array()
            .unwrap()
            .iter()
            .map(|operation| operation["description"].as_str().unwrap())
            .collect::<Vec<_>>()
            .join("\n");

        assert!(descriptions.contains("Alice Private Plan.md"));
        assert!(!descriptions.contains("Bob Secret Acquisition.md"));
        assert!(operations["ops"]
            .as_array()
            .unwrap()
            .iter()
            .all(|operation| operation["docs"] == 1));
        assert!(execute(&notes, None, "ops", &json!({"limit": 50})).is_err());
    }

    #[test]
    fn owner_rename_preflights_both_stores() {
        let root =
            std::env::temp_dir().join(format!("copal-owner-lifecycle-{}", ulid::Ulid::new()));
        let notes = Db::open(&root.join("notes")).unwrap();
        let wiki = Db::open(&root.join("wiki")).unwrap();
        let note = notes
            .create_doc_scoped("alice", "home", "markdown", "Alice.md", "old", None)
            .unwrap();
        let wiki_page = wiki
            .create_doc_scoped("alice", "home", "wiki", "Alice Wiki.md", "old", None)
            .unwrap();
        wiki.create_doc_scoped("taken", "home", "wiki", "Taken.md", "taken", None)
            .unwrap();

        assert!(execute(
            &notes,
            Some(&wiki),
            "rename_owner",
            &json!({"old_owner": "alice", "new_owner": "taken"}),
        )
        .is_err());
        assert!(notes
            .get_doc_scoped(&note.id, "alice", "home")
            .unwrap()
            .is_some());

        let renamed = execute(
            &notes,
            Some(&wiki),
            "rename_owner",
            &json!({"old_owner": "alice", "new_owner": "alice2"}),
        )
        .unwrap();
        assert_eq!(renamed["documents"], 2);
        assert!(notes
            .get_doc_scoped(&note.id, "alice2", "home")
            .unwrap()
            .is_some());
        assert!(wiki
            .get_doc_scoped(&wiki_page.id, "alice2", "home")
            .unwrap()
            .is_some());
        assert!(execute(
            &notes,
            Some(&wiki),
            "rename_owner",
            &json!({"old_owner": "shared", "new_owner": "somebody"}),
        )
        .is_err());
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let data_dir = std::env::var_os("COPAL_DATA_DIR")
        .map(PathBuf::from)
        .or_else(|| std::env::args_os().nth(1).map(PathBuf::from))
        .ok_or("COPAL_DATA_DIR is required")?;
    let db = Db::open(&data_dir).map_err(|error| io::Error::other(error.to_string()))?;
    migrate_hidden_namespaces(&db).map_err(io::Error::other)?;
    // Optional second store for wiki corpus (separate Redb file).
    let wiki_db = std::env::var_os("COPAL_WIKI_DATA_DIR")
        .map(PathBuf::from)
        .and_then(|dir| {
            Db::open_with_name(&dir, "copal-wiki")
                .map_err(|error| io::Error::other(error.to_string()))
                .ok()
        });
    if let Some(ref wiki) = wiki_db {
        migrate_hidden_namespaces(wiki).map_err(io::Error::other)?;
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
