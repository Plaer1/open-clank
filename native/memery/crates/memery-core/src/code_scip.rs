//! Bounded SCIP decoding for private, Host-authorized evidence reads.
use crate::{
    code_evidence::SourceLocator,
    code_query::{QueryError, QueryReceipt},
};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::atomic::{AtomicBool, Ordering},
};

pub const MAX_INDEX_BYTES: usize = 512 * 1024;
pub const MAX_ENCODED_BYTES: usize = MAX_INDEX_BYTES.div_ceil(3) * 4;
const MAX_DOCUMENT_BYTES: usize = 32768;
const MAX_DOCUMENTS: usize = 128;
const MAX_OCCURRENCES: usize = 16384;
const MAX_CANDIDATES: usize = 6;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub receipt: QueryReceipt,
    pub index_base64: String,
    pub source: Option<SourceLocator>,
}

#[derive(Serialize)]
pub struct Response {
    pub receipt: QueryReceipt,
    index_sha256: String,
    producer: BTreeMap<&'static str, String>,
    observation: Option<Observation>,
}

#[derive(Serialize)]
struct Observation {
    source_sha256: String,
    definitions: Vec<Definition>,
    incomplete: bool,
}

#[derive(Serialize)]
struct Definition {
    path: String,
    start: usize,
    end: usize,
    document_sha256: String,
}

fn error(code: &str, message: &str) -> QueryError {
    QueryError {
        code: code.into(),
        message: message.into(),
    }
}

fn invalid() -> QueryError {
    error("unavailable", "Selected SCIP index does not qualify")
}

fn check_cancel(cancel: &AtomicBool) -> Result<(), QueryError> {
    if cancel.load(Ordering::Relaxed) {
        return Err(error("cancelled", "Code query cancelled"));
    }
    Ok(())
}

fn sha(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

enum Value<'a> {
    Number(u64),
    Bytes(&'a [u8]),
}

fn varint(bytes: &mut &[u8]) -> Option<u64> {
    let mut result = 0u64;
    for shift in (0..=63).step_by(7) {
        let (byte, rest) = bytes.split_first()?;
        *bytes = rest;
        if shift == 63 && *byte > 1 {
            return None;
        }
        result |= u64::from(byte & 127) << shift;
        if byte & 128 == 0 {
            return Some(result);
        }
    }
    None
}

fn take<'a>(bytes: &mut &'a [u8], size: usize) -> Option<&'a [u8]> {
    if size > bytes.len() {
        return None;
    }
    let (value, rest) = bytes.split_at(size);
    *bytes = rest;
    Some(value)
}

fn fields(mut bytes: &[u8]) -> Option<Vec<(u32, Value<'_>)>> {
    let mut result = Vec::new();
    while !bytes.is_empty() {
        let tag = varint(&mut bytes)?;
        let field = u32::try_from(tag >> 3).ok()?;
        if !(1..(1 << 29)).contains(&field) {
            return None;
        }
        let value = match tag & 7 {
            0 => Value::Number(varint(&mut bytes)?),
            1 => {
                take(&mut bytes, 8)?;
                continue;
            }
            2 => {
                let size = usize::try_from(varint(&mut bytes)?).ok()?;
                Value::Bytes(take(&mut bytes, size)?)
            }
            5 => {
                take(&mut bytes, 4)?;
                continue;
            }
            _ => return None,
        };
        result.push((field, value));
    }
    Some(result)
}

fn one<'a, T>(
    fields: &[(u32, Value<'a>)],
    field: u32,
    get: impl Fn(&Value<'a>) -> Option<T>,
) -> Result<Option<T>, QueryError> {
    let mut found = fields
        .iter()
        .filter(|(key, _)| *key == field)
        .filter_map(|(_, v)| get(v));
    let first = found.next();
    if found.next().is_some() {
        Err(invalid())
    } else {
        Ok(first)
    }
}

fn blob<'a>(fields: &[(u32, Value<'a>)], field: u32) -> Result<Option<&'a [u8]>, QueryError> {
    one(fields, field, |v| match v {
        Value::Bytes(bytes) => Some(*bytes),
        _ => None,
    })
}

fn number(fields: &[(u32, Value<'_>)], field: u32) -> Result<u64, QueryError> {
    Ok(one(fields, field, |v| match v {
        Value::Number(n) => Some(*n),
        _ => None,
    })?
    .unwrap_or(0))
}

fn relative(path: &str) -> bool {
    !path.is_empty()
        && path.len() <= 4096
        && !path.contains(['\\', ':'])
        && !path.chars().any(char::is_control)
        && path.split('/').all(|part| !matches!(part, "" | "." | ".."))
}

struct Document<'a> {
    text: Option<&'a str>,
    encoding: u64,
    occurrences: Vec<&'a [u8]>,
}

impl Document<'_> {
    fn usable(&self) -> bool {
        self.text.is_some() && matches!(self.encoding, 1..=3)
    }
}

type Documents<'a> = BTreeMap<&'a str, Document<'a>>;
type Producer = BTreeMap<&'static str, String>;

fn decode<'a>(
    bytes: &'a [u8],
    cancel: &AtomicBool,
) -> Result<(Producer, Documents<'a>), QueryError> {
    let root = fields(bytes).ok_or_else(invalid)?;
    let metadata = fields(blob(&root, 1)?.ok_or_else(invalid)?).ok_or_else(invalid)?;
    if number(&metadata, 1)? != 0 || number(&metadata, 4)? > 1 {
        return Err(invalid());
    }
    let tool = fields(blob(&metadata, 2)?.unwrap_or_default()).ok_or_else(invalid)?;
    let mut producer = BTreeMap::new();
    for (field, name) in [(1, "name"), (2, "version")] {
        if let Some(raw) = blob(&tool, field)? {
            if (1..=96).contains(&raw.len())
                && raw
                    .iter()
                    .all(|b| b.is_ascii_alphanumeric() || b"_.@/+ -".contains(b))
            {
                producer.insert(name, String::from_utf8_lossy(raw).into_owned());
            }
        }
    }
    let mut documents = BTreeMap::new();
    let mut occurrences = 0;
    for (key, value) in root {
        check_cancel(cancel)?;
        let (2, Value::Bytes(bytes)) = (key, value) else {
            continue;
        };
        if documents.len() >= MAX_DOCUMENTS {
            return Err(invalid());
        }
        let fields = fields(bytes).ok_or_else(invalid)?;
        let path =
            std::str::from_utf8(blob(&fields, 1)?.ok_or_else(invalid)?).map_err(|_| invalid())?;
        if !relative(path) || documents.contains_key(path) {
            return Err(invalid());
        }
        let text = blob(&fields, 5)?
            .filter(|raw| (1..=MAX_DOCUMENT_BYTES).contains(&raw.len()))
            .and_then(|raw| std::str::from_utf8(raw).ok());
        let encoding = number(&fields, 6)?;
        let rows: Vec<_> = fields
            .into_iter()
            .filter_map(|(key, v)| {
                if let (2, Value::Bytes(bytes)) = (key, v) {
                    Some(bytes)
                } else {
                    None
                }
            })
            .collect();
        occurrences += rows.len();
        if occurrences > MAX_OCCURRENCES {
            return Err(invalid());
        }
        documents.insert(
            path,
            Document {
                text,
                encoding,
                occurrences: rows,
            },
        );
    }
    if !documents.values().any(Document::usable) {
        return Err(invalid());
    }
    Ok((producer, documents))
}

fn range(fields_: &[(u32, Value<'_>)]) -> Result<[u64; 4], QueryError> {
    let single = blob(fields_, 8)?;
    let multi = blob(fields_, 9)?;
    match (single, multi) {
        (Some(_), Some(_)) => Err(invalid()),
        (Some(raw), None) => {
            let f = fields(raw).ok_or_else(invalid)?;
            let line = number(&f, 1)?;
            Ok([line, number(&f, 2)?, line, number(&f, 3)?])
        }
        (None, Some(raw)) => {
            let f = fields(raw).ok_or_else(invalid)?;
            Ok([
                number(&f, 1)?,
                number(&f, 2)?,
                number(&f, 3)?,
                number(&f, 4)?,
            ])
        }
        (None, None) => {
            let mut values = Vec::with_capacity(4);
            for (key, value) in fields_ {
                if *key != 1 {
                    continue;
                }
                match value {
                    Value::Number(n) => values.push(*n),
                    Value::Bytes(mut raw) => {
                        while !raw.is_empty() {
                            values.push(varint(&mut raw).ok_or_else(invalid)?);
                            if values.len() > 4 {
                                return Err(invalid());
                            }
                        }
                    }
                }
                if values.len() > 4 {
                    return Err(invalid());
                }
            }
            match values.as_slice() {
                [a, b, c] => Ok([*a, *b, *a, *c]),
                [a, b, c, d] => Ok([*a, *b, *c, *d]),
                _ => Err(invalid()),
            }
        }
    }
}

struct Line<'a> {
    start: usize,
    text: &'a str,
    // UTF-16 surrogate interiors are deliberately unaddressable.
    offsets: Vec<Option<usize>>,
}

struct Positions<'a> {
    encoding: u64,
    lines: Vec<Line<'a>>,
}

impl<'a> Positions<'a> {
    fn new(document: &Document<'a>) -> Option<Self> {
        if !document.usable() {
            return None;
        }
        let mut start = 0;
        let lines = document
            .text?
            .split('\n')
            .map(|line| {
                let text = line.strip_suffix('\r').unwrap_or(line);
                let mut offsets = Vec::new();
                if document.encoding != 1 {
                    for (offset, ch) in text.char_indices() {
                        offsets.push(Some(offset));
                        if document.encoding == 2 && ch.len_utf16() == 2 {
                            offsets.push(None);
                        }
                    }
                    offsets.push(Some(text.len()));
                }
                let row = Line {
                    start,
                    text,
                    offsets,
                };
                start += line.len() + 1;
                row
            })
            .collect();
        Some(Self {
            encoding: document.encoding,
            lines,
        })
    }

    fn point(&self, line: u64, column: u64) -> Option<usize> {
        let row = self.lines.get(usize::try_from(line).ok()?)?;
        let column = usize::try_from(column).ok()?;
        let offset = if self.encoding == 1 {
            if !row.text.is_char_boundary(column) {
                return None;
            }
            column
        } else {
            (*row.offsets.get(column)?)?
        };
        Some(row.start + offset)
    }

    fn offsets(&self, fields: &[(u32, Value<'_>)]) -> Option<(usize, usize)> {
        let [a, b, c, d] = range(fields).ok()?;
        let start = self.point(a, b)?;
        let end = self.point(c, d)?;
        (start < end).then_some((start, end))
    }
}

fn observe(
    documents: &Documents<'_>,
    source: &SourceLocator,
    cancel: &AtomicBool,
) -> Result<Observation, QueryError> {
    let selected = documents
        .get(source.rel_path.as_str())
        .ok_or_else(invalid)?;
    let positions = Positions::new(selected).ok_or_else(invalid)?;
    let text = selected.text.ok_or_else(invalid)?;
    let start = usize::try_from(source.span.start_byte).map_err(|_| invalid())?;
    let end = usize::try_from(source.span.end_byte).map_err(|_| invalid())?;
    if start >= end || text.get(start..end).is_none() {
        return Err(invalid());
    }
    let mut symbols = BTreeSet::new();
    for raw in &selected.occurrences {
        check_cancel(cancel)?;
        let fields = fields(raw).ok_or_else(invalid)?;
        if let Some(symbol) = blob(&fields, 2)?.filter(|s| (1..=4096).contains(&s.len())) {
            if number(&fields, 3)? & 1 == 0 && positions.offsets(&fields) == Some((start, end)) {
                symbols.insert(symbol);
                if symbols.len() > MAX_CANDIDATES {
                    return Err(invalid());
                }
            }
        }
    }
    let mut found = BTreeSet::new();
    let mut definitions = Vec::new();
    let mut incomplete = symbols.len() != 1;
    if !symbols.is_empty() {
        for (&path, document) in documents {
            let mut positions = None;
            let mut document_sha256 = None;
            for raw in &document.occurrences {
                check_cancel(cancel)?;
                let fields = fields(raw).ok_or_else(invalid)?;
                let Some(symbol) = blob(&fields, 2)? else {
                    continue;
                };
                let role = number(&fields, 3)?;
                if !symbols.contains(symbol)
                    || role & 1 == 0
                    || role & 64 != 0
                    || (symbol.starts_with(b"local ") && path != source.rel_path)
                {
                    continue;
                }
                let Some(text) = document.text.filter(|_| document.usable()) else {
                    incomplete = true;
                    continue;
                };
                let Some((start, end)) = positions
                    .get_or_insert_with(|| Positions::new(document).unwrap())
                    .offsets(&fields)
                else {
                    incomplete = true;
                    continue;
                };
                if found.insert((path, start, end)) {
                    if definitions.len() >= MAX_CANDIDATES {
                        return Err(invalid());
                    }
                    let digest = document_sha256.get_or_insert_with(|| sha(text.as_bytes()));
                    definitions.push(Definition {
                        path: path.into(),
                        start,
                        end,
                        document_sha256: digest.clone(),
                    });
                }
            }
        }
    }
    definitions.sort_by(|a, b| (&a.path, a.start, a.end).cmp(&(&b.path, b.start, b.end)));
    Ok(Observation {
        source_sha256: sha(text.as_bytes()),
        definitions,
        incomplete,
    })
}

pub fn run(request: Request, cancel: &AtomicBool) -> Result<Response, QueryError> {
    if request.index_base64.len() > MAX_ENCODED_BYTES {
        return Err(invalid());
    }
    let bytes = STANDARD
        .decode(request.index_base64)
        .map_err(|_| invalid())?;
    if bytes.is_empty() || bytes.len() > MAX_INDEX_BYTES {
        return Err(invalid());
    }
    let (producer, documents) = decode(&bytes, cancel)?;
    let observation = request
        .source
        .as_ref()
        .map(|source| observe(&documents, source, cancel))
        .transpose()?;
    check_cancel(cancel)?;
    Ok(Response {
        receipt: request.receipt,
        index_sha256: sha(&bytes),
        producer,
        observation,
    })
}
