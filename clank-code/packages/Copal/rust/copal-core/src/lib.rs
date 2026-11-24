#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedNote {
    pub frontmatter: Vec<(String, String)>,
    pub wikilinks: Vec<String>,
    pub embeds: Vec<String>,
    pub tags: Vec<String>,
    pub tasks: Vec<MarkdownTask>,
    pub fenced_blocks: Vec<FencedBlock>,
    pub plugin_queries: Vec<PluginQueryBlock>,
    pub callouts: Vec<CalloutBlock>,
    pub footnotes: Vec<Footnote>,
    pub math_blocks: Vec<MathBlock>,
    pub outline: Vec<OutlineItem>,
    pub tables: Vec<MarkdownTable>,
    pub has_templater: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MarkdownTask {
    pub line: usize,
    pub indent: usize,
    pub status_symbol: char,
    pub status: TaskStatus,
    pub text: String,
    pub clean_text: String,
    pub due_date: Option<String>,
    pub scheduled_date: Option<String>,
    pub done_date: Option<String>,
    pub recurrence: Option<String>,
    pub priority: Priority,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TaskStatus {
    Pending,
    Done,
    InProgress,
    Cancelled,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Priority {
    Low,
    Medium,
    High,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FencedBlock {
    pub line: usize,
    pub lang: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PluginQueryBlock {
    pub line: usize,
    pub lang: String,
    pub kind: PluginQueryKind,
    pub query: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PluginQueryKind {
    Dataview,
    Tasks,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CalloutBlock {
    pub line: usize,
    pub kind: String,
    pub title: String,
    pub fold: Option<char>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Footnote {
    pub line: usize,
    pub id: String,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MathBlock {
    pub line: usize,
    pub body: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OutlineItem {
    pub line: usize,
    pub level: usize,
    pub text: String,
    pub kind: OutlineKind,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OutlineKind {
    Heading,
    List,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MarkdownTable {
    pub line: usize,
    pub headers: Vec<String>,
    pub rows: Vec<Vec<String>>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BaseSummary {
    pub columns: Vec<String>,
    pub filters: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CanvasSummary {
    pub node_count: usize,
    pub edge_count: usize,
    pub files: Vec<String>,
}

pub fn parse_markdown_note(content: &str) -> ParsedNote {
    let (frontmatter, body) = parse_frontmatter(content);
    let lines: Vec<&str> = body.lines().collect();
    let mut wikilinks = Vec::new();
    let mut embeds = Vec::new();
    let mut tags = Vec::new();
    let mut tasks = Vec::new();
    let mut fenced_blocks = Vec::new();
    let mut callouts = Vec::new();
    let mut footnotes = Vec::new();
    let mut math_blocks = Vec::new();
    let mut outline = Vec::new();
    let mut math_open: Option<(usize, Vec<String>)> = None;

    for (idx, line) in lines.iter().enumerate() {
        let line_no = idx + 1;
        collect_wikilinks(line, &mut wikilinks);
        collect_embeds(line, &mut embeds);
        collect_tags(line, &mut tags);
        if let Some(lang) = fence_lang(line) {
            fenced_blocks.push(FencedBlock { line: line_no, lang });
        }
        if let Some(callout) = parse_callout(line, line_no) {
            callouts.push(callout);
        }
        if let Some(footnote) = parse_footnote(line, line_no) {
            footnotes.push(footnote);
        }
        if line.trim() == "$$" {
            if let Some((start_line, body)) = math_open.take() {
                math_blocks.push(MathBlock { line: start_line, body: body.join("\n") });
            } else {
                math_open = Some((line_no, Vec::new()));
            }
            continue;
        }
        if let Some((_, body)) = math_open.as_mut() {
            body.push((*line).to_string());
            continue;
        }
        if let Some(task) = parse_task(line, line_no) {
            tasks.push(task);
        } else if let Some(item) = parse_outline_item(line, line_no) {
            outline.push(item);
        }
    }

    wikilinks.sort();
    wikilinks.dedup();
    embeds.sort();
    embeds.dedup();
    tags.sort();
    tags.dedup();

    ParsedNote {
        frontmatter,
        wikilinks,
        embeds,
        tags,
        tasks,
        fenced_blocks,
        plugin_queries: parse_plugin_queries(&lines),
        callouts,
        footnotes,
        math_blocks,
        outline,
        tables: parse_tables(&lines),
        has_templater: body.contains("<%") && body.contains("%>"),
    }
}

fn parse_frontmatter(content: &str) -> (Vec<(String, String)>, &str) {
    if !content.starts_with("---\n") {
        return (Vec::new(), content);
    }
    let Some(close) = content[4..].find("\n---") else {
        return (Vec::new(), content);
    };
    let raw = &content[4..4 + close];
    let body_start = 4 + close + 4;
    let body = content[body_start..].strip_prefix('\n').unwrap_or(&content[body_start..]);
    let fields = raw
        .lines()
        .filter_map(|line| {
            let (key, value) = line.split_once(':')?;
            Some((key.trim().to_string(), value.trim().to_string()))
        })
        .collect();
    (fields, body)
}

fn collect_wikilinks(line: &str, out: &mut Vec<String>) {
    let mut rest = line;
    while let Some(start) = rest.find("[[") {
        rest = &rest[start + 2..];
        let Some(end) = rest.find("]]") else { break };
        let target = rest[..end]
            .split(['|', '#'])
            .next()
            .unwrap_or("")
            .trim();
        if !target.is_empty() {
            out.push(target.to_string());
        }
        rest = &rest[end + 2..];
    }
}

fn collect_embeds(line: &str, out: &mut Vec<String>) {
    let mut rest = line;
    while let Some(start) = rest.find("![[") {
        rest = &rest[start + 3..];
        let Some(end) = rest.find("]]") else { break };
        let target = rest[..end].split(['|', '#']).next().unwrap_or("").trim();
        if !target.is_empty() {
            out.push(target.to_string());
        }
        rest = &rest[end + 2..];
    }
    rest = line;
    while let Some(start) = rest.find("](") {
        let before = &rest[..start];
        if !before.ends_with('!') && !before.contains("![") {
            rest = &rest[start + 2..];
            continue;
        }
        rest = &rest[start + 2..];
        let Some(end) = rest.find(')') else { break };
        let target = rest[..end].trim();
        if !target.is_empty() {
            out.push(target.to_string());
        }
        rest = &rest[end + 1..];
    }
}

fn collect_tags(line: &str, out: &mut Vec<String>) {
    for word in line.split_whitespace() {
        let clean = word.trim_matches(|c: char| c == ',' || c == '.' || c == ')' || c == '(');
        if let Some(tag) = clean.strip_prefix('#') {
            if !tag.is_empty()
                && tag.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '/' || c == '-')
            {
                out.push(tag.to_string());
            }
        }
    }
}

fn fence_lang(line: &str) -> Option<String> {
    let trimmed = line.trim();
    let lang = trimmed.strip_prefix("```")?;
    if lang.is_empty() {
        return None;
    }
    Some(lang.trim().to_ascii_lowercase())
}

fn parse_callout(line: &str, line_no: usize) -> Option<CalloutBlock> {
    let trimmed = line.trim_start();
    let rest = trimmed.strip_prefix("> [!")?;
    let end = rest.find(']')?;
    let kind = rest[..end].trim().to_ascii_lowercase();
    let after = rest[end + 1..].trim();
    let (fold, title) = match after.chars().next() {
        Some('+') | Some('-') => (after.chars().next(), after[1..].trim()),
        _ => (None, after),
    };
    Some(CalloutBlock { line: line_no, kind, title: title.to_string(), fold })
}

fn parse_footnote(line: &str, line_no: usize) -> Option<Footnote> {
    let trimmed = line.trim_start();
    let rest = trimmed.strip_prefix("[^")?;
    let end = rest.find("]:")?;
    let id = rest[..end].trim();
    if id.is_empty() {
        return None;
    }
    Some(Footnote {
        line: line_no,
        id: id.to_string(),
        text: rest[end + 2..].trim().to_string(),
    })
}

fn parse_plugin_queries(lines: &[&str]) -> Vec<PluginQueryBlock> {
    let mut blocks = Vec::new();
    let mut open: Option<(String, usize, Vec<String>)> = None;
    for (idx, line) in lines.iter().enumerate() {
        let trimmed = line.trim();
        if let Some((lang, start_line, body)) = open.as_mut() {
            if trimmed == "```" {
                let kind = match lang.as_str() {
                    "dataview" | "dataviewjs" => PluginQueryKind::Dataview,
                    "tasks" => PluginQueryKind::Tasks,
                    _ => PluginQueryKind::Unknown,
                };
                if kind != PluginQueryKind::Unknown {
                    blocks.push(PluginQueryBlock {
                        line: *start_line,
                        lang: lang.clone(),
                        kind,
                        query: body.join("\n"),
                    });
                }
                open = None;
            } else {
                body.push((*line).to_string());
            }
            continue;
        }
        if let Some(lang) = fence_lang(line) {
            open = Some((lang, idx + 1, Vec::new()));
        }
    }
    blocks
}

fn parse_task(line: &str, line_no: usize) -> Option<MarkdownTask> {
    let indent = line.chars().take_while(|c| c.is_whitespace()).count();
    let trimmed = line.trim_start();
    let rest = trimmed.strip_prefix("- [").or_else(|| trimmed.strip_prefix("* ["))?;
    let mut chars = rest.chars();
    let symbol = chars.next()?;
    if chars.next()? != ']' || chars.next()? != ' ' {
        return None;
    }
    let text = chars.as_str().trim().to_string();
    Some(MarkdownTask {
        line: line_no,
        indent,
        status_symbol: symbol,
        status: task_status(symbol),
        clean_text: clean_task_text(&text),
        due_date: marker_date(&text, "📅").or_else(|| bracket_field(&text, "due")),
        scheduled_date: marker_date(&text, "⏳").or_else(|| bracket_field(&text, "scheduled")),
        done_date: marker_date(&text, "✅"),
        recurrence: recurrence(&text),
        priority: priority(&text),
        text,
    })
}

fn task_status(symbol: char) -> TaskStatus {
    match symbol {
        'x' | 'X' => TaskStatus::Done,
        '/' => TaskStatus::InProgress,
        '-' => TaskStatus::Cancelled,
        _ => TaskStatus::Pending,
    }
}

fn marker_date(text: &str, marker: &str) -> Option<String> {
    let pos = text.find(marker)?;
    text[pos + marker.len()..]
        .split_whitespace()
        .find(|part| is_date(part))
        .map(|part| part.to_string())
}

fn bracket_field(text: &str, key: &str) -> Option<String> {
    let needle = format!("[{key}::");
    let pos = text.find(&needle)?;
    let after = &text[pos + needle.len()..];
    let end = after.find(']')?;
    let value = after[..end].trim();
    if is_date(value) {
        Some(value.to_string())
    } else {
        None
    }
}

fn recurrence(text: &str) -> Option<String> {
    if let Some(pos) = text.find('🔁') {
        return Some(text[pos + '🔁'.len_utf8()..].trim().to_string());
    }
    let needle = "[repeat::";
    let pos = text.find(needle)?;
    let after = &text[pos + needle.len()..];
    let end = after.find(']')?;
    let value = after[..end].trim();
    if value.is_empty() { None } else { Some(value.to_string()) }
}

fn is_date(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 10 && bytes[4] == b'-' && bytes[7] == b'-'
}

fn priority(text: &str) -> Priority {
    if text.contains('🔺') || text.contains('⏫') || text.contains("[priority:: high]") {
        Priority::High
    } else if text.contains('🔽') || text.contains('⏬') || text.contains("[priority:: low]") {
        Priority::Low
    } else {
        Priority::Medium
    }
}

fn clean_task_text(text: &str) -> String {
    text.split_whitespace()
        .filter(|part| {
            !matches!(*part, "📅" | "⏳" | "✅" | "🔺" | "⏫" | "🔼" | "🔽" | "⏬")
                && !is_date(part)
                && !part.starts_with("[due::")
                && !part.starts_with("[scheduled::")
                && !part.starts_with("[priority::")
                && !part.starts_with("[repeat::")
                && *part != "🔁"
        })
        .collect::<Vec<_>>()
        .join(" ")
}

fn parse_outline_item(line: &str, line_no: usize) -> Option<OutlineItem> {
    let trimmed = line.trim_start();
    if let Some(text) = trimmed.strip_prefix("# ") {
        return Some(OutlineItem { line: line_no, level: 1, text: text.trim().to_string(), kind: OutlineKind::Heading });
    }
    let hashes = trimmed.chars().take_while(|c| *c == '#').count();
    if (2..=6).contains(&hashes) && trimmed.chars().nth(hashes) == Some(' ') {
        return Some(OutlineItem {
            line: line_no,
            level: hashes,
            text: trimmed[hashes + 1..].trim().to_string(),
            kind: OutlineKind::Heading,
        });
    }
    if let Some(text) = trimmed.strip_prefix("- ").or_else(|| trimmed.strip_prefix("* ")) {
        return Some(OutlineItem {
            line: line_no,
            level: line.chars().take_while(|c| c.is_whitespace()).count() / 2 + 1,
            text: text.trim().to_string(),
            kind: OutlineKind::List,
        });
    }
    None
}

fn parse_tables(lines: &[&str]) -> Vec<MarkdownTable> {
    let mut tables = Vec::new();
    let mut i = 0;
    while i + 1 < lines.len() {
        if lines[i].contains('|') && is_separator(lines[i + 1]) {
            let headers = split_row(lines[i]);
            let mut rows = Vec::new();
            i += 2;
            while i < lines.len() && lines[i].contains('|') && !lines[i].trim().is_empty() {
                rows.push(split_row(lines[i]));
                i += 1;
            }
            tables.push(MarkdownTable { line: i.saturating_sub(rows.len() + 1), headers, rows });
        } else {
            i += 1;
        }
    }
    tables
}

fn is_separator(line: &str) -> bool {
    line.trim()
        .trim_matches('|')
        .split('|')
        .all(|cell| cell.trim().chars().all(|c| c == '-' || c == ':') && cell.trim().contains("---"))
}

fn split_row(line: &str) -> Vec<String> {
    line.trim()
        .trim_matches('|')
        .split('|')
        .map(|cell| cell.trim().to_string())
        .collect()
}

pub fn parse_base_summary(content: &str) -> BaseSummary {
    let mut columns = Vec::new();
    let mut filters = Vec::new();
    let mut current = "";
    for raw in content.lines() {
        let line = raw.trim();
        if line.starts_with("columns:") {
            current = "columns";
            continue;
        }
        if line.starts_with("filters:") {
            current = "filters";
            continue;
        }
        if let Some(item) = line.strip_prefix("- ") {
            match current {
                "columns" => columns.push(item.trim().to_string()),
                "filters" => filters.push(item.trim().to_string()),
                _ => {}
            }
        }
    }
    BaseSummary { columns, filters }
}

pub fn parse_canvas_summary(content: &str) -> CanvasSummary {
    let node_count = content.matches("\"type\"").count();
    let edge_count = content.matches("\"fromNode\"").count();
    let mut files = Vec::new();
    let mut rest = content;
    while let Some(pos) = rest.find("\"file\"") {
        rest = &rest[pos + 6..];
        let Some(colon) = rest.find(':') else { break };
        rest = &rest[colon + 1..];
        let Some(start) = rest.find('"') else { break };
        rest = &rest[start + 1..];
        let Some(end) = rest.find('"') else { break };
        files.push(rest[..end].to_string());
        rest = &rest[end + 1..];
    }
    files.sort();
    files.dedup();
    CanvasSummary { node_count, edge_count, files }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_note_syntax() {
        let note = parse_markdown_note(
            "---\ntitle: Demo\n---\n# Demo\nSee [[Other Note]].\n- [/] Build parser #reader 📅 2026-07-15 🔁 every week 🔺\n\n| A | B |\n|---|---|\n| 1 | 2 |\n```dataview\nTASK\n```\n<% tp.date.now() %>\n",
        );
        assert_eq!(note.frontmatter[0], ("title".to_string(), "Demo".to_string()));
        assert_eq!(note.wikilinks, vec!["Other Note"]);
        assert!(note.embeds.is_empty());
        assert_eq!(note.tags, vec!["reader"]);
        assert_eq!(note.tasks[0].status, TaskStatus::InProgress);
        assert_eq!(note.tasks[0].priority, Priority::High);
        assert_eq!(note.tasks[0].due_date.as_deref(), Some("2026-07-15"));
        assert_eq!(note.tasks[0].recurrence.as_deref(), Some("every week 🔺"));
        assert_eq!(note.tables.len(), 1);
        assert_eq!(note.plugin_queries.len(), 1);
        assert!(note.has_templater);
    }

    #[test]
    fn parses_sample_vault_markdown() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let welcome = std::fs::read_to_string(root.join("sample-vault/Welcome to Copal.md")).unwrap();
        let treehouse = std::fs::read_to_string(root.join("sample-vault/Treehouse Learning Map.md")).unwrap();
        let welcome = parse_markdown_note(&welcome);
        let treehouse = parse_markdown_note(&treehouse);
        assert!(welcome.wikilinks.contains(&"Treehouse Learning Map".to_string()));
        assert!(welcome.tasks.len() >= 3);
        assert_eq!(welcome.tables.len(), 1);
        assert!(treehouse.tags.contains(&"skill/vault-learning-graph".to_string()));
        assert!(treehouse.fenced_blocks.iter().any(|block| block.lang == "tasks"));
    }

    #[test]
    fn parses_obsidian_fixture_syntax() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let fixture = std::fs::read_to_string(root.join("sample-vault/Syntax Gallery.md")).unwrap();
        let note = parse_markdown_note(&fixture);
        assert!(note.embeds.iter().any(|embed| embed == "Welcome to Copal.md"));
        assert!(note.callouts.iter().any(|callout| callout.kind == "warning" && callout.fold == Some('-')));
        assert!(note.footnotes.iter().any(|footnote| footnote.id == "copal-note"));
        assert!(note.math_blocks.iter().any(|block| block.body.contains("E = mc^2")));
        assert!(note.plugin_queries.iter().any(|block| block.kind == PluginQueryKind::Dataview));
        assert!(note.plugin_queries.iter().any(|block| block.kind == PluginQueryKind::Tasks));
        assert!(note.has_templater);
    }

    #[test]
    fn parses_sample_base_and_canvas() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let base = std::fs::read_to_string(root.join("sample-vault/Projects/To Watch.base")).unwrap();
        let canvas = std::fs::read_to_string(root.join("sample-vault/Canvas.canvas")).unwrap();
        let base = parse_base_summary(&base);
        let canvas = parse_canvas_summary(&canvas);
        assert!(base.columns.iter().any(|col| col == "file.name"));
        assert!(base.filters.iter().any(|filter| filter.contains("status")));
        assert_eq!(canvas.node_count, 2);
        assert_eq!(canvas.edge_count, 1);
        assert!(canvas.files.iter().any(|file| file == "Welcome to Copal.md"));
    }
}
