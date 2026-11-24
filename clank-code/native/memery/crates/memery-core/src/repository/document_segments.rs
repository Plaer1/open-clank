//! Revision-bearing positions in the canonical extracted document text.
use std::rc::Rc;

use serde_json::{json, Value};

use super::{python_space, sha256_hex, unavailable, DocumentChunk, RepositoryError, MAX_CHUNKS, MAX_LOCATOR};

const WINDOW_CHARS: usize = 1200;
const OVERLAP_CHARS: usize = 120;
const MAX_HEADINGS: usize = 24;
const MAX_MENTIONS: usize = 12;

struct Line {
    start: usize,
    end: usize,
    section: Rc<Vec<(usize, String)>>,
    heading_started: bool,
    table_headings: Option<Rc<Vec<String>>>,
    mentions: Vec<(usize, usize)>,
}

fn heading(line: &str) -> Option<(usize, String)> {
    let text = line.trim_start_matches(' ');
    if line.len() - text.len() > 3 {
        return None;
    }
    let level = text.bytes().take_while(|&b| b == b'#').count();
    if !(1..=6).contains(&level) || !text.as_bytes().get(level).is_some_and(u8::is_ascii_whitespace) {
        return None;
    }
    let name: String = text[level..].trim().trim_end_matches('#').trim_end().chars().take(48).collect();
    (!name.is_empty()).then_some((level, name))
}

fn table_cells(line: &str) -> Option<Vec<String>> {
    let text = line.trim();
    if !text.contains('|') || text.contains("\\|") {
        return None;
    }
    let cells: Vec<String> = text.trim_matches('|').split('|')
        .map(|cell| cell.trim().chars().take(32).collect()).take(9).collect();
    (2..=8).contains(&cells.len()).then_some(cells)
}

fn table_separator(line: &str) -> bool {
    table_cells(line).is_some_and(|cells| cells.iter().all(|cell| {
        let dashes = cell.trim_matches(':');
        dashes.len() >= 3 && dashes.bytes().all(|byte| byte == b'-')
    }))
}

fn fence(line: &str) -> Option<(u8, usize, bool)> {
    let text = line.trim_start_matches(' ');
    let start = *text.as_bytes().first()?;
    if line.len() - text.len() > 3 || !matches!(start, b'`' | b'~') {
        return None;
    }
    let len = text.bytes().take_while(|byte| *byte == start).count();
    (len >= 3).then(|| (start, len, text[len..].trim().is_empty()))
}

fn mention_token(text: &str) -> bool {
    (1..=160).contains(&text.len())
        && text.bytes().all(|b| b.is_ascii_alphanumeric() || b"._:$#/-".contains(&b))
        && text.bytes().any(|b| b.is_ascii_alphabetic() || b == b'_')
        && !text.starts_with('/')
        && !text.contains("//")
        && !text.split('/').any(|part| part == ".." || part == ".")
}

fn inline_mentions(line: &str, offset: usize) -> Vec<(usize, usize)> {
    let mut ticks = line.match_indices('`').peekable();
    let mut open: Option<(usize, usize)> = None;
    let mut mentions = Vec::new();
    while let Some((index, _)) = ticks.next() {
        let mut width = 1;
        while ticks.peek().is_some_and(|(next, _)| *next == index + width) {
            ticks.next();
            width += 1;
        }
        match open {
            Some((begin, old_width)) if width == old_width => {
                if width == 1 && mention_token(&line[begin..index]) {
                    mentions.push((offset + begin, offset + index));
                }
                open = None;
            }
            None if index == 0 || line.as_bytes()[index - 1] != b'\\' => open = Some((index + width, width)),
            _ => (),
        }
    }
    mentions
}

fn lines(text: &str) -> Vec<Line> {
    let mut input = text.split_inclusive('\n').peekable();
    let mut result = Vec::new();
    let mut offset = 0;
    let mut headings: Vec<(usize, usize, String)> = Vec::new();
    let mut section = Rc::new(Vec::new());
    let mut table = None;
    let mut in_fence = None;
    while let Some(line) = input.next() {
        let start = offset;
        offset += line.len();
        let fenced = if let Some((kind, width)) = in_fence {
            if fence(line).is_some_and(|(k, n, closed)| k == kind && n >= width && closed) {
                in_fence = None;
            }
            true
        } else if let Some((kind, width, _)) = fence(line) {
            in_fence = Some((kind, width));
            true
        } else {
            false
        };
        let mut heading_started = false;
        if !fenced {
            if let Some((level, name)) = heading(line) {
                headings.retain(|(parent_level, _, _)| *parent_level < level);
                headings.push((level, start, name));
                heading_started = true;
                section = Rc::new(headings.iter().map(|(_, start, title)| (*start, title.clone())).collect());
            }
            if input.peek().is_some_and(|line| table_separator(line)) {
                table = table_cells(line).map(Rc::new);
            } else if table_cells(line).is_none() {
                table = None;
            }
        } else {
            table = None;
        }
        result.push(Line {
            start,
            end: offset,
            section: section.clone(),
            heading_started,
            table_headings: table.clone(),
            mentions: if fenced { Vec::new() } else { inline_mentions(line, start) },
        });
    }
    result
}

pub(super) fn mentions(text: &str) -> Value {
    let mut omitted = 0;
    let mut mentions = Vec::new();
    for (index, line) in lines(text).into_iter().enumerate() {
        for (start, end) in line.mentions {
            if mentions.len() == 64 {
                omitted += 1;
            } else {
                mentions.push(json!({
                    "start_byte":start,"end_byte":end,"line":index+1,"spelling":&text[start..end],
                }));
            }
        }
    }
    json!({"mentions":mentions,"omitted":omitted})
}

pub(super) fn chunks(text: &str, pages: &[usize]) -> Result<Vec<DocumentChunk>, RepositoryError> {
    let mut bytes: Vec<usize> = text.char_indices().map(|(offset, _)| offset).collect();
    bytes.push(text.len());
    let chars = bytes.len() - 1;
    if pages.len() > 1024
        || (!pages.is_empty() && pages.last() != Some(&text.len()))
        || pages.windows(2).any(|pair| pair[0] > pair[1])
        || pages.iter().any(|&end| !text.is_char_boundary(end))
    {
        return Err(unavailable());
    }
    let lines = lines(text);
    let mut start = 0;
    let mut result = Vec::new();
    while start < chars {
        let end = (start + WINDOW_CHARS).min(chars);
        let (start_byte, end_byte) = (bytes[start], bytes[end]);
        let excerpt = &text[start_byte..end_byte];
        if !excerpt.chars().all(python_space) {
            let first_line = lines.partition_point(|line| line.end <= start_byte);
            let last_line = lines.partition_point(|line| line.start < end_byte) - 1;
            let section = &lines[first_line].section;
            let window = &lines[first_line..=last_line];
            let table = window.iter().find_map(|line| line.table_headings.as_deref());
            let headings = window.iter().filter(|line| line.heading_started && line.start >= start_byte);
            let omitted_sections = headings.clone().skip(MAX_HEADINGS).count();
            let sections: Vec<Value> = headings.take(MAX_HEADINGS).filter_map(|line| {
                let (at, title) = line.section.last()?;
                let parent = line.section.iter().rev().nth(1).map(|(offset, _)| offset);
                Some(json!({"start_byte":at,"parent_byte":parent,"title":title}))
            }).collect();
            let mut mentions: Vec<Value> = Vec::new();
            let mut omitted_mentions = 0;
            for &(from, to) in window.iter().flat_map(|line| &line.mentions) {
                if from < start_byte || to > end_byte {
                    continue;
                }
                if mentions.len() >= MAX_MENTIONS {
                    omitted_mentions += 1;
                    continue;
                }
                mentions.push(json!({"start_byte":from,"end_byte":to,"spelling":&text[from..to]}));
            }
            let covered_pages = (!pages.is_empty()).then(|| {
                [pages.partition_point(|&page_end| page_end <= start_byte) + 1,
                 pages.partition_point(|&page_end| page_end < end_byte) + 1]
            });
            let locator = json!({
                "type":"text", "start":start, "end":end,
                "line_start":first_line+1, "line_end":last_line+1,
                "headings":section.iter().take(MAX_HEADINGS).map(|(_,title)| title).collect::<Vec<_>>(),
                "block_type":if table.is_some() {"table"} else {"text"},
                "structure_version":1, "start_byte":start_byte, "end_byte":end_byte,
                "section_starts":section.iter().take(MAX_HEADINGS).map(|(at,_)| at).collect::<Vec<_>>(),
                "sections":sections, "omitted_sections":omitted_sections,
                "table_headings":table, "pages":covered_pages,
                "code_mentions":mentions, "omitted_mentions":omitted_mentions,
            });
            let locator_json = serde_json::to_string(&locator)?;
            if result.len() == MAX_CHUNKS || locator_json.len() > MAX_LOCATOR {
                return Err(unavailable());
            }
            result.push(DocumentChunk {
                ordinal:result.len() as u64, text:excerpt.into(), text_sha256:sha256_hex(excerpt.as_bytes()), locator_json,
            });
        }
        if end == chars {
            break;
        }
        start = end - OVERLAP_CHARS;
    }
    if result.is_empty() {
        return Err(unavailable());
    }
    Ok(result)
}
