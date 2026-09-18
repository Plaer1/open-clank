use std::collections::{BTreeMap, BTreeSet};
use std::io::{self, BufRead, Write};
use std::path::PathBuf;

use base64::Engine;
use copal_db::{
    Content, Db, DocView, GuardedRequest, ImportIdentity, OwnerInventory, WriteOutcome,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const COPAL_PROTOCOL_VERSION: u64 = 1;
const COPAL_CAPABILITIES: &[&str] = &[
    "scoped-storage",
    "native-notes",
    "task-index",
    "guarded-commit",
    "wiki-seeds",
];
const COPAL_SOURCE_IDENTITY: &str = match option_env!("COPAL_SOURCE_IDENTITY") {
    Some(identity) => identity,
    None => "unpackaged",
};

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

Timeline uses the same owner-scoped Copal records as Notes. Canonical event files live under `.events/`; that folder is hidden in the normal file view unless you ask to show dot-folders. Wiki records use `.memes/` and follow the same default.

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
        name: ".memes/What Is Wiki",
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
        name: ".memes/Creating and Linking Memes",
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
        name: ".memes/Story Navigation",
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
        name: ".memes/Fields and Properties",
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
        name: ".memes/Wiki vs Notes",
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
- Linked-page navigation with open stories
- Compact, scannable pages with footer properties

## They work together
Notes and Wiki are separate stores but you can link between them. A Wiki meme can link to a Notes document and vice versa. Each store has its own search, history, and trash.

#wiki #howto #seed"##,
    },
    WikiSeed {
        name: ".memes/How Wiki Works",
        body: r##"# How Wiki Works
Wiki is Copal's meme garden: small, interlinked pages in its own store (`copal-wiki.redb`), separate from Notes. Its home is the hidden `.memes/` folder — invisible in the normal file tree, visible through Wiki mode.

## The loop
- **Create:** "+ Meme" in the Wiki sidebar → name → write.
- **Link:** `[[Page Name]]` anywhere. Existing target = clickable; missing = greyed "broken" link you can click to create.
- **Backlinks:** opening a page lists every other page that links to it.
- **Tags / search:** `#tag` organizes; the sidebar search covers titles, bodies, tags.
- **Edit / save:** Edit toggle, type, Save. A stale tab that overwrites newer work is refused — reopen and merge.
- **Recover:** deleted pages live in Wiki trash; restore from there.

See a complete working example at [[Meme-sized Page]].

#wiki #howto #guide"##,
    },
    WikiSeed {
        name: ".memes/Meme-sized Page",
        body: r##"# Meme-sized Page
One idea per page: this whole meme is the example.

It links back to [[How Wiki Works]] (which links here — open it to see the backlink).

#wiki #example #meme"##,
    },
];

const WIKI_SEED_VERSION: u64 = 1;

fn wiki_seed_slug(name: &str) -> String {
    let mut slug = String::new();
    for character in name.chars() {
        if character.is_ascii_alphanumeric() {
            slug.push(character.to_ascii_lowercase());
        } else if !slug.ends_with('-') {
            slug.push('-');
        }
    }
    slug.trim_matches('-').to_string()
}

fn wiki_note(seed: &WikiSeed) -> String {
    let slug = wiki_seed_slug(seed.name);
    let lines = seed.body.split('\n').collect::<Vec<_>>();
    let relations = links(seed.body)
        .iter()
        .enumerate()
        .map(|(index, target)| {
            let source_block = lines
                .iter()
                .position(|line| line.contains(&format!("[[{target}")))
                .map(|line| format!("blk_wiki_{slug}_{}", line + 1));
            json!({
                "id": format!("rel_wiki_{slug}_{}", index + 1),
                "kind": "link", "origin": "body", "sourceBlockId": source_block,
                "target": target, "targetDocumentId": Value::Null, "targetBlockId": Value::Null,
            })
        })
        .collect::<Vec<_>>();
    let blocks = lines
        .iter()
        .enumerate()
        .map(|(index, line)| {
            let id = format!("blk_wiki_{slug}_{}", index + 1);
            let trimmed = line.trim_start();
            let indent = line.len() - trimmed.len();
            let heading = trimmed.chars().take_while(|character| *character == '#').count();
            let block = if heading > 0 && heading <= 6 && trimmed.as_bytes().get(heading) == Some(&b' ') {
                json!({"id": id, "type": "heading", "level": heading, "text": &trimmed[heading + 1..], "source": line})
            } else if let Some(text) = trimmed.strip_prefix("- [ ] ") {
                json!({"id": id, "type": "task", "checked": false, "text": text, "indent": indent, "source": line})
            } else if let Some(text) = trimmed.strip_prefix("- [x] ").or_else(|| trimmed.strip_prefix("- [X] ")) {
                json!({"id": id, "type": "task", "checked": true, "text": text, "indent": indent, "source": line})
            } else if let Some(text) = trimmed.strip_prefix("- ") {
                json!({"id": id, "type": "bullet", "indent": indent, "text": text, "source": line})
            } else if line.is_empty() {
                json!({"id": id, "type": "blank", "text": "", "source": line})
            } else {
                json!({"id": id, "type": "paragraph", "text": line, "source": line})
            };
            let relation_ids = relations
                .iter()
                .filter(|relation| relation.get("sourceBlockId").and_then(Value::as_str) == Some(&id))
                .filter_map(|relation| relation.get("id").cloned())
                .collect::<Vec<_>>();
            let mut block = block;
            if !relation_ids.is_empty() {
                block["relationIds"] = Value::Array(relation_ids);
            }
            block
        })
        .collect::<Vec<_>>();
    json!({
        "schemaVersion": 1,
        "body": {"type": "doc", "blocks": blocks},
        "properties": [
            {"id": format!("prop_wiki_{slug}_type"), "key": "type", "type": "text", "value": "wiki"},
            {"id": format!("prop_wiki_{slug}_version"), "key": "seedVersion", "type": "number", "value": WIKI_SEED_VERSION},
            {"id": format!("prop_wiki_{slug}_source"), "key": "sourcePath", "type": "text", "value": seed.name},
            {"id": format!("prop_wiki_{slug}_builtin"), "key": "builtin", "type": "checkbox", "value": true},
            {"id": format!("prop_wiki_{slug}_tags"), "key": "tags", "type": "tags", "value": ["wiki", "builtin"]}
        ],
        "relations": relations,
        "tags": ["wiki", "builtin"],
        "extensions": {"interchange": {"source": seed.body, "modified": false}, "seed": {"version": WIKI_SEED_VERSION, "name": seed.name}}
    }).to_string()
}

struct LegacyWikiSeed {
    // Base64 of the exact pre-v2 name. Keeping the compatibility fingerprint
    // encoded prevents retired product wording from entering fresh source or UI.
    name_fingerprint: &'static str,
    target: &'static str,
    blob: &'static str,
    preserve_name: bool,
}

// Compatibility-only fingerprints from the bundled pre-v2 Wiki seeds. The
// encoded name and bundled blob hash recognize existing records for migration;
// fresh seeds above use the `.memes/` vocabulary. A name fingerprint alone is
// not enough to claim a shared document: an unrelated page stays data.
const LEGACY_WIKI_SEEDS: &[LegacyWikiSeed] = &[
    LegacyWikiSeed {
        // The installed schema-3 Wiki store contains this exact historical
        // alias alongside the canonical Meme-sized Page. Recognize it for
        // provenance, but preserve the alias when the canonical target exists.
        name_fingerprint: "Lm1lbWVzL01lbWUtc2l6ZWQgVGlkZGxlcg==",
        target: ".memes/Meme-sized Page",
        blob: "1eafefa210565ab12a8a2d44b4389202713290a93529ce1667ad30ca533d8048",
        preserve_name: true,
    },
    LegacyWikiSeed {
        name_fingerprint: "V2lraS9DcmVhdGluZyBhbmQgTGlua2luZyBUaWRkbGVycw==",
        target: ".memes/Creating and Linking Memes",
        blob: "032b23b359978236a0f75228a9e3c30df6fce1f5012f3b7f9f685bb81841bc6a",
        preserve_name: false,
    },
    LegacyWikiSeed {
        name_fingerprint: "V2lraS9GaWVsZHMgYW5kIFByb3BlcnRpZXM=",
        target: ".memes/Fields and Properties",
        blob: "79a26b736bbc6fc613e9e350202c123baed1c028206451a14ed20d1fbdfd1bc0",
        preserve_name: false,
    },
    LegacyWikiSeed {
        name_fingerprint: "V2lraS9TdG9yeSBOYXZpZ2F0aW9u",
        target: ".memes/Story Navigation",
        blob: "eb4d7413bd0afd5e4f7f6b9f1414400eec909134f4565157ede23e685adda6a8",
        preserve_name: false,
    },
    LegacyWikiSeed {
        name_fingerprint: "V2lraS9XaGF0IElzIFdpa2k=",
        target: ".memes/What Is Wiki",
        blob: "93d36071f83856a09881a35d18b3bfb0031075a3ee2263ba2c9fb91ab7164481",
        preserve_name: false,
    },
    LegacyWikiSeed {
        name_fingerprint: "V2lraS9XaWtpIHZzIE5vdGVz",
        target: ".memes/Wiki vs Notes",
        blob: "b3ada6681af29a23d943f63421da091157ccae20d6dadea3e677cf631813b91d",
        preserve_name: false,
    },
];

fn legacy_wiki_seed(doc: &DocView) -> Option<&'static LegacyWikiSeed> {
    if doc.kind != "wiki" || doc.owner != "shared" || doc.workspace_id != "global" {
        return None;
    }
    let Content::Blob { hash } = &doc.content else {
        return None;
    };
    let name_fingerprint = base64::engine::general_purpose::STANDARD.encode(doc.name.as_bytes());
    LEGACY_WIKI_SEEDS
        .iter()
        .find(|seed| name_fingerprint == seed.name_fingerprint && hash == seed.blob)
}

fn known_legacy_wiki_seed(doc: &DocView, seed: &WikiSeed) -> bool {
    let Content::Blob { hash } = &doc.content else { return false };
    LEGACY_WIKI_SEEDS
        .iter()
        .any(|legacy| legacy.target == seed.name && legacy.blob == hash)
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
        if seed.preserve_name {
            return None;
        }
        return Some(seed.target.to_string());
    }
    if doc.kind == "copal-event"
        || (matches!(doc.kind.as_str(), "markdown" | "note")
            && has_event_frontmatter(doc.text.as_deref()))
    {
        return legacy_event_tail(&doc.name).map(|tail| format!(".events/{tail}"));
    }
    if doc.kind == "wiki" {
        // One-time namespace repair: early builds used `.wik/`, `.wiki/`, or
        // `Wiki/`. Fold those records into the canonical `.memes/` home so
        // seeded builtins rename in place instead of orphaning duplicates.
        if let Some(tail) = doc
            .name
            .strip_prefix(".wik/")
            .filter(|tail| !tail.is_empty())
        {
            return Some(format!(".memes/{tail}"));
        }
        return doc
            .name
            .strip_prefix(".wiki/")
            .filter(|tail| !tail.is_empty())
            .map(|tail| format!(".memes/{tail}"))
            .or_else(|| {
                doc.name
                    .strip_prefix("Wiki/")
                    .filter(|tail| !tail.is_empty())
                    .map(|tail| format!(".memes/{tail}"))
            });
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

    // Two legacy sources (e.g. `Wiki/Foo` and `.wik/Foo`) must not collapse into
    // one canonical name. Refuse before any rename so the operator can reconcile.
    let mut seen_targets = std::collections::HashSet::new();
    for (doc, target) in &moves {
        let key = (
            doc.owner.as_str(),
            doc.workspace_id.as_str(),
            doc.corpus.as_str(),
            target.as_str(),
        );
        if !seen_targets.insert(key) {
            return Err(format!(
                "cannot migrate {}: multiple sources collapse to {target}",
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
        let expected = wiki_note(seed);
        if let Some(doc) = existing
            .iter()
            .find(|doc| doc.builtin && doc.name == seed.name)
            .cloned()
        {
            if doc.text.as_deref() == Some(expected.as_str()) {
                continue;
            }
            // Upgrade only an exact historical seed. An edited builtin remains
            // untouched so startup cannot erase provenance or user evidence.
            if doc.text.as_deref() == Some(seed.body) || known_legacy_wiki_seed(&doc, seed) {
                if matches!(db.write_doc(&doc.id, &expected, Some(&doc.head)).map_err(|error| error.to_string())?, WriteOutcome::Committed { .. }) {
                    changed += 1;
                }
            }
            continue;
        }

        if let Some(doc) = existing
            .iter()
            .find(|doc| doc.name == seed.name && (doc.text.as_deref() == Some(seed.body) || known_legacy_wiki_seed(doc, seed)))
            .cloned()
        {
            // Exact legacy bundle content is the only safe unmarked record to claim.
            let promoted = db
                .claim_builtin_seed_doc(&doc.id)
                .map_err(|error| error.to_string())?;
            if let Some(current) = existing.iter_mut().find(|item| item.id == doc.id) {
                *current = match db.write_doc(&promoted.id, &expected, Some(&promoted.head)).map_err(|error| error.to_string())? {
                    WriteOutcome::Committed { view, .. } => view,
                    WriteOutcome::Unchanged { view, .. } | WriteOutcome::Stale { view, .. } => view,
                };
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
                &expected,
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
    let raw_source = |raw: &str| {
        let digest = Sha256::digest(raw.as_bytes());
        json!({
            "encoding": "utf-8",
            "base64": base64::engine::general_purpose::STANDARD.encode(raw.as_bytes()),
            "sha256": format!("{digest:x}"),
        })
    };
    let fail_with = |message: String, recovery_state: &str| {
        let raw = doc.text.as_deref().unwrap_or("");
        json!({
            "id": doc.id, "recordSchemaVersion": doc.record_schema_version,
            "corpus": doc.corpus, "kind": doc.kind,
            "owner": doc.owner, "workspace_id": doc.workspace_id,
            "builtin": doc.builtin, "readOnly": doc.builtin || doc.kind == "wiki",
            "name": doc.name, "head": doc.head, "ts": doc.ts,
            "hidden": doc.hidden, "deleted": doc.deleted, "content": doc.content,
            "text": "", "properties": {}, "propertyDefinitions": [], "frontmatter": {},
            "relations": [], "links": [], "tags": [], "blocks": [], "tasks": [], "treehouse": null,
            "format": "copal-note-v1", "storage": "database",
            "extensions": {},
            "rawPreserved": true, "note_error": message,
            "rawSource": raw_source(raw), "recoveryState": recovery_state,
        })
    };
    let fail = |message: String| fail_with(message, "malformed-preserved");
    let raw = doc.text.as_deref().unwrap_or("");
    // A Wiki can contain an imported Markdown source from before native Wiki
    // records existed. Keep it inert and byte-recoverable, while exposing a
    // deliberate conversion state to the route/UI.
    let trimmed = raw.trim_start();
    let looks_like_json_value = trimmed.starts_with('[') || trimmed.starts_with('"')
        || matches!(trimmed.chars().next(), Some('0'..='9' | 't' | 'f' | 'n'))
        || (trimmed.starts_with('-') && trimmed.chars().nth(1).is_some_and(|ch| ch.is_ascii_digit()));
    if !trimmed.starts_with('{') && !looks_like_json_value {
        return json!({
            "id": doc.id, "recordSchemaVersion": doc.record_schema_version,
            "corpus": doc.corpus, "kind": doc.kind,
            "owner": doc.owner, "workspace_id": doc.workspace_id,
            "builtin": doc.builtin, "readOnly": doc.builtin || doc.kind == "wiki",
            "name": doc.name, "head": doc.head, "ts": doc.ts,
            "hidden": doc.hidden, "deleted": doc.deleted, "content": doc.content,
            "text": raw, "properties": {}, "propertyDefinitions": [], "frontmatter": {},
            "relations": [], "links": [], "tags": [], "blocks": [], "tasks": [], "treehouse": null,
            "format": "copal-note-v1", "storage": "database", "extensions": {},
            "rawPreserved": true, "formatNotice": "legacy-markdown",
            "rawSource": raw_source(raw), "recoveryState": "legacy-import", "sourceFormat": "markdown",
        });
    }
    let record = match serde_json::from_str::<Value>(raw) {
        Ok(Value::Object(record)) => record,
        Ok(_) => return fail("database note root is not an object".to_string()),
        Err(error) => return fail(error.to_string()),
    };
    if let Some(version) = record.get("schemaVersion").and_then(Value::as_u64) {
        if version > 1 {
            return fail_with("This Wiki source uses a newer native schema".to_string(), "unsupported-future");
        }
    }
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
        "extensions": extensions, "recoveryState": "supported",
    })
}

fn indexed(doc: &DocView) -> Value {
    if matches!(doc.kind.as_str(), "note" | "wiki") {
        indexed_note(doc)
    } else {
        indexed_legacy_markdown(doc)
    }
}

fn metadata_size(doc: &DocView) -> u64 {
    match &doc.content {
        Content::Asset { size, .. } => *size,
        Content::Blob { .. } => doc.text.as_ref().map(|text| text.len() as u64).unwrap_or(0),
        Content::Conflict { .. } | Content::Tombstone => 0,
    }
}

fn metadata(doc: &DocView) -> Value {
    json!({
        "id": doc.id,
        "recordSchemaVersion": doc.record_schema_version,
        "corpus": doc.corpus,
        "kind": doc.kind,
        "builtin": doc.builtin,
        "readOnly": doc.builtin,
        "name": doc.name,
        "head": doc.head,
        "ts": doc.ts,
        "hidden": doc.hidden,
        "deleted": doc.deleted,
        "size": metadata_size(doc),
        "storage": "database"
    })
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

fn owner_lifecycle_manifest(
    notes: &Db,
    wiki: Option<&Db>,
    old_owner: &str,
    new_owner: &str,
) -> Result<Value, String> {
    let (notes_source, notes_target) = notes
        .preflight_rename_owner(old_owner, new_owner)
        .map_err(|error| error.to_string())?;
    let wiki_pair = wiki
        .map(|db| db.preflight_rename_owner(old_owner, new_owner))
        .transpose()
        .map_err(|error| error.to_string())?;
    let (wiki_source, wiki_target) = wiki_pair.unwrap_or_else(|| {
        (
            OwnerInventory::empty(old_owner),
            OwnerInventory::empty(new_owner),
        )
    });
    let source_documents = notes_source.documents + wiki_source.documents;
    let target_documents = notes_target.documents + wiki_target.documents;
    if source_documents > 0 && target_documents > 0 {
        return Err("destination owner already has Copal documents".to_string());
    }
    Ok(json!({
        "schema_version": 1,
        "source": {
            "owner": old_owner,
            "notes": notes_source,
            "wiki": wiki_source,
            "documents": source_documents,
            "content_included": false,
        },
        "target": {
            "owner": new_owner,
            "notes": notes_target,
            "wiki": wiki_target,
            "documents": target_documents,
            "content_included": false,
        },
        "content_included": false,
    }))
}

fn lifecycle_inventory(
    manifest: &Value,
    side: &str,
    store: &str,
) -> Result<OwnerInventory, String> {
    serde_json::from_value(
        manifest
            .get(side)
            .and_then(|value| value.get(store))
            .cloned()
            .ok_or_else(|| format!("Copal lifecycle manifest is missing {side}.{store}"))?,
    )
    .map_err(|error| format!("invalid Copal lifecycle inventory: {error}"))
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
                "protocol_version": COPAL_PROTOCOL_VERSION,
                "source_identity": COPAL_SOURCE_IDENTITY,
                "capabilities": COPAL_CAPABILITIES,
                "schema_version": schema_version,
                "documents": docs.len() + wiki_docs.len(),
                "kinds": kinds,
                "integrity_ok": true,
            }))
        }
        "preflight_rename_owner" => {
            let old_owner = required(args, "old_owner")?;
            let new_owner = required(args, "new_owner")?;
            if let Some(manifest) = db
                .load_owner_lifecycle_manifest(old_owner, new_owner)
                .map_err(|error| error.to_string())?
            {
                Ok(manifest)
            } else {
                owner_lifecycle_manifest(db, wiki_db, old_owner, new_owner)
            }
        }
        "owner_inventory" => {
            let owner = required(args, "owner")?;
            let empty_target = format!("__copal_inventory_target_{}", ulid::Ulid::new());
            owner_lifecycle_manifest(db, wiki_db, owner, &empty_target)
                .map(|manifest| manifest["source"].clone())
        }
        "rename_owner" | "reconcile_owner" => {
            let old_owner = required(args, "old_owner")?;
            let new_owner = required(args, "new_owner")?;
            let proposed = if let Some(value) = args.get("manifest") {
                value.clone()
            } else if let Some(value) = db
                .load_owner_lifecycle_manifest(old_owner, new_owner)
                .map_err(|error| error.to_string())?
            {
                value
            } else {
                owner_lifecycle_manifest(db, wiki_db, old_owner, new_owner)?
            };
            let manifest = db
                .freeze_owner_lifecycle_manifest(old_owner, new_owner, &proposed)
                .map_err(|error| error.to_string())?;
            let notes_source = lifecycle_inventory(&manifest, "source", "notes")?;
            let notes_target = lifecycle_inventory(&manifest, "target", "notes")?;
            let wiki_source = lifecycle_inventory(&manifest, "source", "wiki")?;
            let wiki_target = lifecycle_inventory(&manifest, "target", "wiki")?;
            let notes = db
                .reconcile_owner_rename(old_owner, new_owner, &notes_source, &notes_target)
                .map_err(|error| error.to_string())?;
            let wiki = wiki_db
                .map(|wiki| {
                    wiki.reconcile_owner_rename(old_owner, new_owner, &wiki_source, &wiki_target)
                })
                .transpose()
                .map_err(|error| error.to_string())?
                .unwrap_or_else(|| json!({"state": "empty", "documents": 0}));
            db.mark_owner_lifecycle(old_owner, new_owner, "complete")
                .map_err(|error| error.to_string())?;
            db.prune_owner_lifecycles(old_owner, Some(old_owner), Some(new_owner))
                .map_err(|error| error.to_string())?;
            db.prune_owner_lifecycles(new_owner, Some(old_owner), Some(new_owner))
                .map_err(|error| error.to_string())?;
            Ok(json!({
                "schema_version": 1,
                "state": if notes["state"] == "applied" || wiki["state"] == "applied" {
                    "applied"
                } else {
                    "already_applied"
                },
                "notes": notes,
                "wiki": wiki,
                "documents": notes_source.documents + wiki_source.documents,
                "content_included": false,
            }))
        }
        "compensate_owner_rename" => {
            let old_owner = required(args, "old_owner")?;
            let new_owner = required(args, "new_owner")?;
            let manifest = args
                .get("manifest")
                .ok_or_else(|| "Copal compensation requires a lifecycle manifest".to_string())?;
            let manifest = db
                .freeze_owner_lifecycle_manifest(old_owner, new_owner, manifest)
                .map_err(|error| error.to_string())?;
            let notes_source = lifecycle_inventory(&manifest, "source", "notes")?;
            let notes_target = lifecycle_inventory(&manifest, "target", "notes")?;
            let wiki_source = lifecycle_inventory(&manifest, "source", "wiki")?;
            let wiki_target = lifecycle_inventory(&manifest, "target", "wiki")?;
            let notes = db
                .compensate_owner_rename(old_owner, new_owner, &notes_source, &notes_target)
                .map_err(|error| error.to_string())?;
            let wiki = wiki_db
                .map(|wiki| {
                    wiki.compensate_owner_rename(old_owner, new_owner, &wiki_source, &wiki_target)
                })
                .transpose()
                .map_err(|error| error.to_string())?
                .unwrap_or_else(|| json!({"state": "empty", "documents": 0}));
            db.mark_owner_lifecycle(old_owner, new_owner, "compensated")
                .map_err(|error| error.to_string())?;
            db.prune_owner_lifecycles(old_owner, Some(old_owner), Some(new_owner))
                .map_err(|error| error.to_string())?;
            db.prune_owner_lifecycles(new_owner, Some(old_owner), Some(new_owner))
                .map_err(|error| error.to_string())?;
            Ok(json!({
                "schema_version": 1,
                "state": "compensated",
                "notes": notes,
                "wiki": wiki,
                "documents": notes_source.documents + wiki_source.documents,
                "content_included": false,
            }))
        }
        "purge_owner" => {
            let owner = required(args, "owner")?;
            let expected_manifest = if let Some(value) = args.get("expected") {
                value.clone()
            } else {
                let empty_target = format!("__copal_purge_target_{}", ulid::Ulid::new());
                owner_lifecycle_manifest(db, wiki_db, owner, &empty_target)?["source"].clone()
            };
            let notes_expected: OwnerInventory = serde_json::from_value(
                expected_manifest
                    .get("notes")
                    .cloned()
                    .ok_or_else(|| "Copal purge inventory is missing notes".to_string())?,
            )
            .map_err(|error| format!("invalid Copal purge inventory: {error}"))?;
            let wiki_expected: OwnerInventory = serde_json::from_value(
                expected_manifest
                    .get("wiki")
                    .cloned()
                    .ok_or_else(|| "Copal purge inventory is missing wiki".to_string())?,
            )
            .map_err(|error| format!("invalid Copal purge inventory: {error}"))?;
            let notes = db
                .purge_owner_deferred_assets(owner, Some(&notes_expected))
                .map_err(|error| error.to_string())?;
            let wiki = wiki_db
                .map(|wiki| wiki.purge_owner_deferred_assets(owner, Some(&wiki_expected)))
                .transpose()
                .map_err(|error| error.to_string())?
                .unwrap_or_else(|| json!({"state": "empty", "removed_documents": 0}));
            let removed_assets = if let Some(wiki_db) = wiki_db {
                if db.assets_dir() == wiki_db.assets_dir() {
                    let wiki_references = wiki_db
                        .asset_references()
                        .map_err(|error| error.to_string())?;
                    db.compact_orphan_assets(&wiki_references)
                        .map_err(|error| error.to_string())?
                } else {
                    let notes_removed = db
                        .compact_orphan_assets(&BTreeSet::new())
                        .map_err(|error| error.to_string())?;
                    let wiki_removed = wiki_db
                        .compact_orphan_assets(&BTreeSet::new())
                        .map_err(|error| error.to_string())?;
                    notes_removed + wiki_removed
                }
            } else {
                db.compact_orphan_assets(&BTreeSet::new())
                    .map_err(|error| error.to_string())?
            };
            db.prune_owner_lifecycles(owner, None, None)
                .map_err(|error| error.to_string())?;
            Ok(json!({
                "schema_version": 1,
                "state": "applied",
                "notes": notes,
                "wiki": wiki,
                "removed_assets": removed_assets,
                "documents": notes_expected.documents + wiki_expected.documents,
                "content_included": false,
                "physical_compaction": true,
                "history_retained": false,
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
            let expected_heads = args
                .get("expected_heads")
                .cloned()
                .map(serde_json::from_value::<BTreeMap<String, String>>)
                .transpose()
                .map_err(|error| format!("invalid expected restore heads: {error}"))?
                .unwrap_or_default();
            target
                .import_vault_scoped_as_with_ids_and_heads(
                    &vault,
                    planning.as_deref(),
                    owner,
                    workspace_id,
                    note_kind,
                    &restore_ids,
                    &expected_heads,
                )
                .map(|stats| json!(stats))
                .map_err(|error| error.to_string())
        }
        "metadata_page" | "metadata_get" => {
            let (owner, workspace_id) = scope(args)?;
            let state = optional(args, "state").unwrap_or("active");
            if !matches!(state, "active" | "trash") {
                return Err("unsupported metadata state".to_string());
            }
            let hidden = optional(args, "hidden").unwrap_or("exclude");
            if !matches!(hidden, "exclude" | "include" | "only") {
                return Err("unsupported hidden filter".to_string());
            }
            let corpus = optional(args, "corpus").unwrap_or("all");
            let collect = |target: &Db| {
                if state == "trash" {
                    target.list_deleted_docs_scoped(owner, workspace_id)
                } else {
                    target.list_docs_scoped(owner, workspace_id)
                }
            };
            let mut docs = if corpus == "all" {
                let mut rows = collect(db).map_err(|error| error.to_string())?;
                if let Some(wiki) = wiki_db {
                    rows.extend(collect(wiki).map_err(|error| error.to_string())?);
                }
                rows
            } else {
                collect(pick_db(db, wiki_db, args)?).map_err(|error| error.to_string())?
            };
            docs.retain(|doc| match hidden {
                "include" => true,
                "only" => doc.hidden,
                _ => !doc.hidden,
            });
            if op == "metadata_get" {
                let id = required(args, "id")?;
                return docs
                    .iter()
                    .find(|doc| doc.id == id)
                    .map(metadata)
                    .ok_or_else(|| "document not found in this scope".to_string());
            }
            let query = optional(args, "query").unwrap_or("").to_lowercase();
            if !query.is_empty() {
                docs.retain(|doc| doc.name.to_lowercase().contains(&query));
            }
            let sort_key = optional(args, "sort_key").unwrap_or("name");
            let descending = optional(args, "sort_direction").unwrap_or("asc") == "desc";
            if !matches!(sort_key, "name" | "kind" | "size" | "modified") {
                return Err("unsupported metadata sort".to_string());
            }
            docs.sort_by(|left, right| {
                let order = match sort_key {
                    "kind" => (&left.kind, &left.name, &left.id).cmp(&(
                        &right.kind,
                        &right.name,
                        &right.id,
                    )),
                    "size" => {
                        let left_size = metadata_size(left);
                        let right_size = metadata_size(right);
                        (left_size, &left.name, &left.id).cmp(&(right_size, &right.name, &right.id))
                    }
                    "modified" => {
                        (left.ts, &left.name, &left.id).cmp(&(right.ts, &right.name, &right.id))
                    }
                    _ => (&left.name, &left.kind, &left.id).cmp(&(
                        &right.name,
                        &right.kind,
                        &right.id,
                    )),
                };
                if descending {
                    order.reverse()
                } else {
                    order
                }
            });
            let metadata_rows = docs.iter().map(metadata).collect::<Vec<_>>();
            let encoded = serde_json::to_vec(&metadata_rows).map_err(|error| error.to_string())?;
            let snapshot = blake3::hash(&encoded).to_hex().to_string();
            if let Some(expected) = optional(args, "snapshot") {
                if expected != snapshot {
                    return Err("stale_cursor".to_string());
                }
            }
            let offset = args
                .get("cursor")
                .and_then(Value::as_str)
                .unwrap_or("0")
                .parse::<usize>()
                .map_err(|_| "invalid metadata cursor".to_string())?;
            let limit = args
                .get("limit")
                .and_then(Value::as_u64)
                .unwrap_or(100)
                .clamp(1, 200) as usize;
            let total = metadata_rows.len();
            let page = metadata_rows
                .into_iter()
                .skip(offset)
                .take(limit)
                .collect::<Vec<_>>();
            let next_cursor =
                (offset + page.len() < total).then(|| (offset + page.len()).to_string());
            Ok(json!({
                "docs": page,
                "total": total,
                "next_cursor": next_cursor,
                "snapshot": snapshot,
                "storage": "database"
            }))
        }
        "find_by_name" => {
            let (owner, workspace_id) = scope(args)?;
            let name = required(args, "name")?;
            let corpus = optional(args, "corpus").unwrap_or("all");
            let target = if corpus == "all" { None } else { Some(pick_db(db, wiki_db, args)?) };
            let mut docs = if let Some(target) = target {
                target
                    .list_docs_scoped(owner, workspace_id)
                    .map_err(|error| error.to_string())?
            } else {
                let mut rows = db
                    .list_docs_scoped(owner, workspace_id)
                    .map_err(|error| error.to_string())?;
                if let Some(wiki) = wiki_db {
                    rows.extend(
                        wiki.list_docs_scoped(owner, workspace_id)
                            .map_err(|error| error.to_string())?,
                    );
                }
                rows
            };
            docs.retain(|doc| doc.name == name && (corpus == "all" || doc.corpus == corpus));
            Ok(docs.first().map(indexed).unwrap_or(Value::Null))
        }
        "task_index_get" => {
            let (owner, workspace_id) = scope(args)?;
            let ids = args
                .get("ids")
                .cloned()
                .map(serde_json::from_value::<Vec<String>>)
                .transpose()
                .map_err(|error| format!("invalid task index ids: {error}"))?;
            db.task_index_get(owner, workspace_id, ids.as_deref())
                .map_err(|error| error.to_string())
        }
        "task_index_generation" => {
            let (owner, workspace_id) = scope(args)?;
            db.task_index_generation(owner, workspace_id)
                .map_err(|error| error.to_string())
        }
        "task_index_resolve" => {
            let (owner, workspace_id) = scope(args)?;
            db.task_index_resolve(owner, workspace_id, required(args, "resourceId")?)
                .map_err(|error| error.to_string())
        }
        "task_index_page" => {
            let (owner, workspace_id) = scope(args)?;
            let completed = args.get("completed").and_then(Value::as_bool);
            let source = optional(args, "source").unwrap_or("all");
            let query = optional(args, "query").unwrap_or("");
            let cursor = args.get("cursor").and_then(Value::as_str);
            let limit = args.get("limit").and_then(Value::as_u64).unwrap_or(100).clamp(1, 500) as usize;
            let generation = optional(args, "generation").unwrap_or("");
            db.task_index_page(owner, workspace_id, query, completed, source, cursor, limit, generation)
                .map_err(|error| error.to_string())
        }
        "task_index_update" => {
            let (owner, workspace_id) = scope(args)?;
            let removed = args
                .get("removed")
                .cloned()
                .map(serde_json::from_value::<Vec<String>>)
                .transpose()
                .map_err(|error| format!("invalid task index removals: {error}"))?
                .unwrap_or_default();
            db.task_index_update(
                owner,
                workspace_id,
                optional(args, "generation").unwrap_or(""),
                args.get("records").unwrap_or(&Value::Object(Default::default())),
                &removed,
                args.get("rebuild").and_then(Value::as_bool).unwrap_or(false),
                args.get("sourceReads").and_then(Value::as_u64).unwrap_or(0) as usize,
            )
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
        "commit_guarded" => {
            let (owner, workspace_id) = scope(args)?;
            let target = pick_db(db, wiki_db, args)?;
            let request: GuardedRequest = serde_json::from_value(args.clone())
                .map_err(|error| format!("invalid guarded request: {error}"))?;
            target
                .commit_guarded(&request, owner, workspace_id)
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
        "put_asset_scoped" => {
            let (owner, workspace_id) = scope(args)?;
            let target = pick_db(db, wiki_db, args)?;
            let encoded = required(args, "base64")?;
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(encoded)
                .map_err(|error| format!("asset base64 is invalid: {error}"))?;
            let doc = target
                .put_asset_scoped(
                    owner,
                    workspace_id,
                    required(args, "name")?,
                    optional(args, "ext").unwrap_or("bin"),
                    &bytes,
                )
                .map_err(|error| error.to_string())?;
            Ok(json!({ "doc": doc }))
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
    fn metadata_pages_are_body_free_scoped_and_snapshot_bound() {
        let root = std::env::temp_dir().join(format!("copal-metadata-page-{}", ulid::Ulid::new()));
        let db = Db::open(&root).unwrap();
        db.create_doc_scoped("alice", "home", "note", "Alpha.md", "SECRET BODY", None)
            .unwrap();
        db.create_doc_scoped("bob", "home", "note", "Private.md", "BOB SECRET", None)
            .unwrap();
        let first = execute(
            &db,
            None,
            "metadata_page",
            &json!({
                "owner": "alice", "workspace_id": "home", "corpus": "all",
                "hidden": "exclude", "state": "active", "limit": 1,
                "sort_key": "name", "sort_direction": "asc"
            }),
        )
        .unwrap();
        let rows = first["docs"].as_array().unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["name"], "Alpha.md");
        assert!(rows[0].get("text").is_none());
        assert!(rows[0].get("content").is_none());
        let snapshot = first["snapshot"].as_str().unwrap();

        db.create_doc_scoped("alice", "home", "note", "Beta.md", "body", None)
            .unwrap();
        let stale = execute(
            &db,
            None,
            "metadata_page",
            &json!({
                "owner": "alice", "workspace_id": "home", "corpus": "all",
                "hidden": "exclude", "state": "active", "cursor": "1",
                "snapshot": snapshot, "limit": 1,
                "sort_key": "name", "sort_direction": "asc"
            }),
        )
        .unwrap_err();
        assert_eq!(stale, "stale_cursor");
    }

    #[test]
    fn metadata_pages_sort_assets_and_text_by_honest_byte_size() {
        let root = std::env::temp_dir().join(format!("copal-metadata-size-{}", ulid::Ulid::new()));
        let vault = root.join("vault");
        std::fs::create_dir_all(&vault).unwrap();
        std::fs::write(vault.join("Z-small.md"), "é").unwrap();
        std::fs::write(vault.join("A-large.bin"), b"123456").unwrap();
        let db = Db::open(&root.join("database")).unwrap();
        db.import_vault_scoped(&vault, None, "alice", "home")
            .unwrap();

        let result = execute(
            &db,
            None,
            "metadata_page",
            &json!({
                "owner": "alice", "workspace_id": "home", "corpus": "all",
                "hidden": "exclude", "state": "active", "limit": 10,
                "sort_key": "size", "sort_direction": "asc"
            }),
        )
        .unwrap();
        let rows = result["docs"].as_array().unwrap();

        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["name"], "Z-small.md");
        assert_eq!(rows[0]["size"], 2);
        assert_eq!(rows[1]["name"], "A-large.bin");
        assert_eq!(rows[1]["size"], 6);

        drop(db);
        std::fs::remove_dir_all(root).unwrap();
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
    fn legacy_wiki_markdown_is_readable_without_a_decode_error() {
        let mut wiki = note_view("# Heading\n\nUnicode — readable\n");
        wiki.kind = "wiki".to_string();
        wiki.corpus = "wiki".to_string();
        let indexed = indexed(&wiki);
        assert_eq!(indexed["text"], "# Heading\n\nUnicode — readable\n");
        assert!(indexed.get("note_error").is_none());
        assert_eq!(indexed["recoveryState"], "legacy-import");
        assert_eq!(indexed["formatNotice"], "legacy-markdown");
        assert_eq!(indexed["readOnly"], true);
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
        let legacy_wik = db
            .create_doc_scoped("alice", "home", "wiki", ".wik/Legacy", "# Legacy", None)
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

        assert_eq!(migrate_hidden_namespaces(&db).unwrap(), 4);
        assert_eq!(migrate_hidden_namespaces(&db).unwrap(), 0);

        let migrated_event = db.get_doc(&event.id).unwrap().unwrap();
        let migrated_markdown_event = db.get_doc(&markdown_event.id).unwrap().unwrap();
        let migrated_wiki = db.get_doc(&wiki.id).unwrap().unwrap();
        let migrated_legacy_wik = db.get_doc(&legacy_wik.id).unwrap().unwrap();
        assert_eq!(migrated_event.name, ".events/Launch.md");
        assert_eq!(migrated_markdown_event.name, ".events/Imported.md");
        assert_eq!(migrated_markdown_event.kind, "markdown");
        assert_eq!(migrated_wiki.name, ".memes/Launch");
        assert_eq!(migrated_legacy_wik.name, ".memes/Legacy");
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
        let legacy = LEGACY_WIKI_SEEDS
            .iter()
            .find(|seed| seed.target == ".memes/Creating and Linking Memes")
            .unwrap();
        let mut wiki = note_view("");
        wiki.kind = "wiki".to_string();
        wiki.corpus = "wiki".to_string();
        wiki.owner = "shared".to_string();
        wiki.workspace_id = "global".to_string();
        // Compatibility regression: this exact pre-v2 identifier must remain
        // readable for old records even though fresh seeds use new wording.
        wiki.name = String::from_utf8(
            base64::engine::general_purpose::STANDARD
                .decode(legacy.name_fingerprint)
                .unwrap(),
        )
        .unwrap();
        wiki.content = Content::Blob {
            hash: legacy.blob.to_string(),
        };
        // An unrelated blob keeps its legacy tail while moving namespaces;
        // this protects user data from an over-broad seed alias.
        assert_eq!(
            hidden_namespace_target(&wiki).as_deref(),
            Some(".memes/Creating and Linking Memes")
        );

        wiki.content = Content::Blob {
            hash: "unrelated".to_string(),
        };
        assert_eq!(
            hidden_namespace_target(&wiki),
            Some(format!(
                ".memes/{}",
                wiki.name.strip_prefix("Wiki/").unwrap()
            ))
        );
    }

    #[test]
    fn eighth_historical_alias_shape_is_exactly_recognized_and_preserved() {
        let legacy = LEGACY_WIKI_SEEDS
            .iter()
            .find(|seed| seed.preserve_name)
            .unwrap();
        let mut wiki = note_view("# historical body\n\nUnicode — preserved\n");
        wiki.kind = "wiki".to_string();
        wiki.corpus = "wiki".to_string();
        wiki.owner = "shared".to_string();
        wiki.workspace_id = "global".to_string();
        wiki.name = String::from_utf8(
            base64::engine::general_purpose::STANDARD
                .decode(legacy.name_fingerprint)
                .unwrap(),
        )
        .unwrap();
        wiki.content = Content::Blob {
            hash: legacy.blob.to_string(),
        };
        assert_eq!(hidden_namespace_target(&wiki), None);
        let indexed = indexed(&wiki);
        assert_eq!(indexed["text"], "# historical body\n\nUnicode — preserved\n");
        assert!(indexed.get("note_error").is_none());
        assert_eq!(indexed["recoveryState"], "legacy-import");
        assert_eq!(indexed["readOnly"], true);
    }

    #[test]
    fn hidden_namespace_migration_refuses_collisions_before_writing() {
        let root =
            std::env::temp_dir().join(format!("copal-hidden-collision-{}", ulid::Ulid::new()));
        let db = Db::open(&root).unwrap();
        let legacy = db
            .create_doc_scoped("alice", "home", "wiki", "Wiki/Same", "old", None)
            .unwrap();
        db.create_doc_scoped("alice", "home", "wiki", ".memes/Same", "new", None)
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
        assert_ne!(claimed_wiki.head, legacy_wiki.head);
        assert_eq!(claimed_wiki.text.as_deref(), Some(wiki_note(&WIKI_SEEDS[0]).as_str()));

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
    fn wiki_seeds_are_native_readable_and_idempotent() {
        let root = std::env::temp_dir().join(format!("copal-wiki-native-{}", ulid::Ulid::new()));
        let wiki = Db::open(&root).unwrap();
        assert_eq!(seed_wiki_pages(&wiki).unwrap(), WIKI_SEEDS.len());
        let first = wiki
            .list_docs()
            .unwrap()
            .into_iter()
            .filter(|doc| doc.builtin && doc.kind == "wiki")
            .map(|doc| (doc.name.clone(), doc.id.clone(), doc.head.clone(), indexed(&doc)))
            .collect::<Vec<_>>();
        assert_eq!(first.len(), WIKI_SEEDS.len());
        for (name, _id, _head, view) in &first {
            assert!(view["text"].as_str().is_some_and(|text| !text.trim().is_empty()), "{name}");
            assert!(view.get("note_error").is_none(), "{name} unexpectedly failed to decode");
            assert_eq!(view["properties"]["seedVersion"], WIKI_SEED_VERSION);
            assert_eq!(view["properties"]["type"], "wiki");
            assert!(view["text"].as_str().unwrap().starts_with('#'));
        }
        assert_eq!(seed_wiki_pages(&wiki).unwrap(), 0);
        let second = wiki
            .list_docs()
            .unwrap()
            .into_iter()
            .filter(|doc| doc.builtin && doc.kind == "wiki")
            .map(|doc| (doc.name, doc.id, doc.head))
            .collect::<Vec<_>>();
        assert_eq!(first.iter().map(|(name, id, head, _)| (name.clone(), id.clone(), head.clone())).collect::<Vec<_>>(), second);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn edited_builtin_wiki_is_preserved_during_seeding() {
        let root = std::env::temp_dir().join(format!("copal-wiki-edited-{}", ulid::Ulid::new()));
        let wiki = Db::open(&root).unwrap();
        let edited = wiki
            .create_builtin_seed_doc("wiki", WIKI_SEEDS[0].name, "# edited by an operator", None)
            .unwrap();
        assert_eq!(seed_wiki_pages(&wiki).unwrap(), WIKI_SEEDS.len() - 1);
        let current = wiki.get_doc(&edited.id).unwrap().unwrap();
        assert_eq!(current.head, edited.head);
        assert_eq!(current.text.as_deref(), Some("# edited by an operator"));
        std::fs::remove_dir_all(root).unwrap();
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

    #[test]
    fn owner_lifecycle_recovers_cross_store_partial_move_compensates_and_purges() {
        let root = std::env::temp_dir().join(format!("copal-owner-saga-{}", ulid::Ulid::new()));
        let notes = Db::open(&root).unwrap();
        let wiki = Db::open_with_name(&root, "copal-wiki").unwrap();
        notes
            .create_doc_scoped(
                "alice",
                "home",
                "markdown",
                "Private Note.md",
                "ALICE NOTE SECRET",
                None,
            )
            .unwrap();
        wiki.create_doc_scoped(
            "alice",
            "home",
            "wiki",
            "Private Wiki.md",
            "ALICE WIKI SECRET",
            None,
        )
        .unwrap();
        notes
            .create_doc_scoped("bob", "home", "markdown", "Bob.md", "BOB", None)
            .unwrap();
        let bob_before = notes.owner_inventory("bob").unwrap();
        let manifest = execute(
            &notes,
            Some(&wiki),
            "preflight_rename_owner",
            &json!({"old_owner": "alice", "new_owner": "deleted:stable"}),
        )
        .unwrap();
        assert_eq!(manifest["source"]["documents"], 2);
        assert!(!serde_json::to_string(&manifest)
            .unwrap()
            .contains("ALICE NOTE SECRET"));

        // Simulate a process death after the notes Redb transaction commits
        // but before the separate wiki transaction begins.
        let notes_source = lifecycle_inventory(&manifest, "source", "notes").unwrap();
        let notes_target = lifecycle_inventory(&manifest, "target", "notes").unwrap();
        notes
            .reconcile_owner_rename("alice", "deleted:stable", &notes_source, &notes_target)
            .unwrap();
        assert_eq!(notes.owner_inventory("alice").unwrap().documents, 0);
        assert_eq!(wiki.owner_inventory("alice").unwrap().documents, 1);

        let converged = execute(
            &notes,
            Some(&wiki),
            "rename_owner",
            &json!({
                "old_owner": "alice",
                "new_owner": "deleted:stable",
                "manifest": manifest,
            }),
        )
        .unwrap();
        assert_eq!(converged["state"], "applied");
        let replay = execute(
            &notes,
            Some(&wiki),
            "rename_owner",
            &json!({
                "old_owner": "alice",
                "new_owner": "deleted:stable",
                "manifest": manifest,
            }),
        )
        .unwrap();
        assert_eq!(replay["state"], "already_applied");

        let compensated = execute(
            &notes,
            Some(&wiki),
            "compensate_owner_rename",
            &json!({
                "old_owner": "alice",
                "new_owner": "deleted:stable",
                "manifest": manifest,
            }),
        )
        .unwrap();
        assert_eq!(compensated["state"], "compensated");
        assert_eq!(notes.owner_inventory("alice").unwrap().documents, 1);
        assert_eq!(wiki.owner_inventory("alice").unwrap().documents, 1);

        execute(
            &notes,
            Some(&wiki),
            "rename_owner",
            &json!({
                "old_owner": "alice",
                "new_owner": "deleted:stable",
                "manifest": manifest,
            }),
        )
        .unwrap();
        let expected = execute(
            &notes,
            Some(&wiki),
            "owner_inventory",
            &json!({"owner": "deleted:stable"}),
        )
        .unwrap();
        let purged = execute(
            &notes,
            Some(&wiki),
            "purge_owner",
            &json!({"owner": "deleted:stable", "expected": expected}),
        )
        .unwrap();
        assert_eq!(purged["physical_compaction"], true);
        assert_eq!(
            notes.owner_inventory("deleted:stable").unwrap().documents,
            0
        );
        assert_eq!(wiki.owner_inventory("deleted:stable").unwrap().documents, 0);
        assert_eq!(
            notes.owner_inventory("bob").unwrap().fingerprint,
            bob_before.fingerprint
        );
        let purge_replay = execute(
            &notes,
            Some(&wiki),
            "purge_owner",
            &json!({"owner": "deleted:stable", "expected": expected}),
        )
        .unwrap();
        assert_eq!(purge_replay["documents"], 2);
        assert!(!serde_json::to_string(&purged)
            .unwrap()
            .contains("ALICE WIKI SECRET"));
        drop(wiki);
        drop(notes);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn owner_purge_compacts_shared_asset_directory_without_touching_other_store_refs() {
        let root = std::env::temp_dir().join(format!("copal-owner-assets-{}", ulid::Ulid::new()));
        let notes = Db::open(&root).unwrap();
        let wiki = Db::open_with_name(&root, "copal-wiki").unwrap();
        let alice_vault = root.join("alice-vault");
        let bob_vault = root.join("bob-vault");
        std::fs::create_dir_all(&alice_vault).unwrap();
        std::fs::create_dir_all(&bob_vault).unwrap();
        std::fs::write(alice_vault.join("alice.bin"), b"ALICE ASSET").unwrap();
        std::fs::write(bob_vault.join("bob.bin"), b"BOB ASSET").unwrap();
        notes
            .import_vault_scoped(&alice_vault, None, "alice", "home")
            .unwrap();
        wiki.import_vault_scoped(&bob_vault, None, "bob", "home")
            .unwrap();
        let alice = notes.list_docs_scoped("alice", "home").unwrap().remove(0);
        let bob = wiki.list_docs_scoped("bob", "home").unwrap().remove(0);
        let Content::Asset {
            hash: alice_hash,
            ext: alice_ext,
            ..
        } = alice.content
        else {
            panic!("alice import was not an asset")
        };
        let Content::Asset {
            hash: bob_hash,
            ext: bob_ext,
            ..
        } = bob.content
        else {
            panic!("bob import was not an asset")
        };
        let alice_path = notes.asset_file(&alice_hash, &alice_ext);
        let bob_path = wiki.asset_file(&bob_hash, &bob_ext);
        assert!(alice_path.exists());
        assert!(bob_path.exists());
        let expected = execute(
            &notes,
            Some(&wiki),
            "owner_inventory",
            &json!({"owner": "alice"}),
        )
        .unwrap();

        execute(
            &notes,
            Some(&wiki),
            "purge_owner",
            &json!({"owner": "alice", "expected": expected}),
        )
        .unwrap();

        assert!(!alice_path.exists());
        assert!(bob_path.exists());
        assert_eq!(wiki.owner_inventory("bob").unwrap().documents, 1);
        drop(wiki);
        drop(notes);
        std::fs::remove_dir_all(root).unwrap();
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
