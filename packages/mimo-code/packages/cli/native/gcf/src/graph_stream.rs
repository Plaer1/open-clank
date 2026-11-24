//! Stateless typed Symbol/Edge stream built on the generic stream. No session, no remembered
//! symbols and no references: every stream declares its own symbols. Output is provisional until `end`.
//! Input is newline-delimited JSON events, `{"op":"symbol",...}` then `{"op":"edge",...}`, in any chunking.
use std::collections::HashSet;
use crate::{codec, parser::{self, Value, MAX_BYTES}, stream::GenericStream, stream_decoder};

pub const POLICY: &str = "gcf-graph-stream-v1";
const SYMBOL_FIELDS: [&str; 6] = ["id", "kind", "name", "file", "line", "signature"];
const EDGE_FIELDS: [&str; 4] = ["from", "to", "relation", "line"];
const KINDS: [&str; 12] = ["module", "class", "interface", "struct", "enum", "type", "function", "method", "field", "property", "variable", "constant"];
const RELATIONS: [&str; 7] = ["calls", "imports", "extends", "implements", "references", "contains", "uses"];

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Symbol { pub id: String, pub kind: String, pub name: String, pub file: Option<String>, pub line: Option<i64>, pub signature: Option<String> }
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Edge { pub from: String, pub to: String, pub relation: String, pub line: Option<i64> }

fn get<'a>(items: &'a [(String, Value)], key: &str) -> Result<&'a Value, ()> { items.iter().find(|(k, _)| k == key).map(|(_, v)| v).ok_or(()) }
fn string(v: &Value) -> Result<String, ()> { if let Value::String(s) = v { Ok(s.clone()) } else { Err(()) } }
fn name(v: &Value) -> Result<String, ()> { string(v).and_then(|s| if s.is_empty() { Err(()) } else { Ok(s) }) }
fn choice(v: &Value, allowed: &[&str]) -> Result<String, ()> { string(v).and_then(|s| if allowed.contains(&s.as_str()) { Ok(s) } else { Err(()) }) }
fn opt_string(v: &Value) -> Result<Option<String>, ()> { if *v == Value::Null { Ok(None) } else { string(v).map(Some) } }
fn opt_line(v: &Value) -> Result<Option<i64>, ()> { match v { Value::Null => Ok(None), Value::Integer(n) if *n >= 0 => Ok(Some(*n)), _ => Err(()) } }

/// Closed shape: exactly the declared fields (plus `extra` bookkeeping keys), nothing unknown.
fn symbol_from(items: &[(String, Value)], extra: usize) -> Result<Symbol, ()> {
    if items.len() != SYMBOL_FIELDS.len() + extra { return Err(()) }
    Ok(Symbol { id: name(get(items, "id")?)?, kind: choice(get(items, "kind")?, &KINDS)?, name: name(get(items, "name")?)?,
        file: opt_string(get(items, "file")?)?, line: opt_line(get(items, "line")?)?, signature: opt_string(get(items, "signature")?)? })
}
fn edge_from(items: &[(String, Value)], extra: usize) -> Result<Edge, ()> {
    if items.len() != EDGE_FIELDS.len() + extra { return Err(()) }
    Ok(Edge { from: name(get(items, "from")?)?, to: name(get(items, "to")?)?, relation: choice(get(items, "relation")?, &RELATIONS)?, line: opt_line(get(items, "line")?)? })
}
fn text(s: &str) -> Value { Value::String(s.to_owned()) }
fn opt_text(s: &Option<String>) -> Value { s.as_ref().map_or(Value::Null, |s| text(s)) }
fn opt_num(n: &Option<i64>) -> Value { n.map_or(Value::Null, Value::Integer) }
fn event(op: &str, key: &str, value: Value) -> Result<Vec<u8>, ()> {
    let mut items = vec![("op".to_owned(), text(op))];
    if !key.is_empty() { items.push((key.to_owned(), value)) }
    codec::canonical_json(&Value::Object(items)).map_err(|_| ())
}
fn begin(table: &str, fields: &[&str]) -> Result<Vec<u8>, ()> {
    let items = vec![("op".to_owned(), text("begin")), ("name".to_owned(), text(table)), ("fields".to_owned(), Value::Array(fields.iter().map(|f| text(f)).collect()))];
    codec::canonical_json(&Value::Object(items)).map_err(|_| ())
}

#[derive(Clone, Copy, PartialEq)]
enum Phase { Start, Symbols, Edges }
pub struct GraphStream { inner: GenericStream, pending: Vec<u8>, all: Vec<u8>, symbols: Vec<Symbol>, edges: Vec<Edge>, ids: HashSet<String>, phase: Phase, terminal: bool }

impl GraphStream {
    pub fn new() -> Self { Self { inner: GenericStream::new(), pending: Vec::new(), all: Vec::new(), symbols: Vec::new(), edges: Vec::new(), ids: HashSet::new(), phase: Phase::Start, terminal: false } }
    fn feed(&mut self, bytes: &[u8], out: &mut Vec<u8>) -> Result<(), ()> {
        let chunk = self.inner.push(bytes)?;
        self.all.extend_from_slice(&chunk); out.extend_from_slice(&chunk); Ok(())
    }
    /// Close the symbols table (opening it first if needed) and open the edges table.
    fn open_edges(&mut self, out: &mut Vec<u8>) -> Result<(), ()> {
        if self.phase == Phase::Start { self.feed(&begin("symbols", &SYMBOL_FIELDS)?, out)? }
        if self.phase != Phase::Edges { self.feed(&event("end", "", Value::Null)?, out)?; self.feed(&begin("edges", &EDGE_FIELDS)?, out)?; self.phase = Phase::Edges }
        Ok(())
    }
    fn line(&mut self, line: &[u8], out: &mut Vec<u8>) -> Result<(), ()> {
        let Value::Object(items) = parser::parse(line).map_err(|_| ())? else { return Err(()) };
        match get(&items, "op").and_then(string)?.as_str() {
            "symbol" => {
                if self.phase == Phase::Edges { return Err(()) }
                let s = symbol_from(&items, 1)?;
                if !self.ids.insert(s.id.clone()) { return Err(()) }
                if self.phase == Phase::Start { self.feed(&begin("symbols", &SYMBOL_FIELDS)?, out)?; self.phase = Phase::Symbols }
                let row = Value::Array(vec![text(&s.id), text(&s.kind), text(&s.name), opt_text(&s.file), opt_num(&s.line), opt_text(&s.signature)]);
                self.feed(&event("row", "values", row)?, out)?; self.symbols.push(s)
            }
            "edge" => {
                let e = edge_from(&items, 1)?;
                if !self.ids.contains(&e.from) || !self.ids.contains(&e.to) { return Err(()) }
                self.open_edges(out)?;
                let row = Value::Array(vec![text(&e.from), text(&e.to), text(&e.relation), opt_num(&e.line)]);
                self.feed(&event("row", "values", row)?, out)?; self.edges.push(e)
            }
            _ => return Err(()),
        }
        Ok(())
    }
    fn push_inner(&mut self, input: &[u8]) -> Result<Vec<u8>, ()> {
        if self.terminal { return Err(()) }
        self.pending.extend_from_slice(input);
        let mut out = Vec::new();
        while let Some(at) = self.pending.iter().position(|b| *b == b'\n') {
            let line: Vec<u8> = self.pending.drain(..=at).collect();
            self.line(&line[..at], &mut out)?;
        }
        if self.pending.len() > MAX_BYTES { return Err(()) }
        Ok(out)
    }
    /// Accepts any chunking; returns the output chunk for the records completed by this input.
    pub fn push(&mut self, input: &[u8]) -> Result<Vec<u8>, ()> { let r = self.push_inner(input); if r.is_err() { self.abort() } r }
    fn end_inner(&mut self) -> Result<Vec<u8>, ()> {
        if self.terminal || !self.pending.is_empty() { return Err(()) }
        let mut out = Vec::new();
        self.open_edges(&mut out)?;
        self.feed(&event("end", "", Value::Null)?, &mut out)?;
        let tail = self.inner.close()?;
        self.all.extend_from_slice(&tail); out.extend_from_slice(&tail);
        let (symbols, edges) = decode(&self.all)?;
        if symbols != self.symbols || edges != self.edges { return Err(()) }
        Ok(out)
    }
    /// Validates the whole stream; only after this succeeds is the concatenated output usable.
    pub fn end(&mut self) -> Result<Vec<u8>, ()> { let r = self.end_inner(); self.abort_if(r.is_err()); self.terminal = true; r }
    fn abort_if(&mut self, failed: bool) { if failed { self.abort() } }
    pub fn abort(&mut self) { self.terminal = true; self.inner.abort(); self.pending.clear(); self.all.clear(); self.symbols.clear(); self.edges.clear(); self.ids.clear() }
}

/// Independent strict reader: generic stream verification, then the closed Symbol/Edge shape.
pub fn decode(input: &[u8]) -> Result<(Vec<Symbol>, Vec<Edge>), ()> {
    let json = stream_decoder::decode_stream(input).map_err(|_| ())?;
    let Value::Object(root) = parser::parse(&json).map_err(|_| ())? else { return Err(()) };
    let [(k0, Value::Array(rows0)), (k1, Value::Array(rows1))] = root.as_slice() else { return Err(()) };
    if k0 != "symbols" || k1 != "edges" { return Err(()) }
    let shaped = |row: &Value, fields: &[&str]| -> Result<Vec<(String, Value)>, ()> {
        let Value::Object(items) = row else { return Err(()) };
        if items.iter().map(|(k, _)| k.as_str()).ne(fields.iter().copied()) { return Err(()) }
        Ok(items.clone())
    };
    let mut ids = HashSet::new(); let mut symbols = Vec::new(); let mut edges = Vec::new();
    for row in rows0 { let s = symbol_from(&shaped(row, &SYMBOL_FIELDS)?, 0)?; if !ids.insert(s.id.clone()) { return Err(()) } symbols.push(s) }
    for row in rows1 { let e = edge_from(&shaped(row, &EDGE_FIELDS)?, 0)?; if !ids.contains(&e.from) || !ids.contains(&e.to) { return Err(()) } edges.push(e) }
    Ok((symbols, edges))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn sym(id: &str, kind: &str) -> String { format!(r#"{{"op":"symbol","id":"{id}","kind":"{kind}","name":"n {id}","file":"src/a|b.rs","line":3,"signature":null}}"#) }
    fn edge(a: &str, b: &str) -> String { format!(r#"{{"op":"edge","from":"{a}","to":"{b}","relation":"calls","line":null}}"#) }
    fn run(chunks: &[&[u8]]) -> Result<Vec<u8>, ()> {
        let mut s = GraphStream::new(); let mut out = Vec::new();
        for c in chunks { out.extend(s.push(c)?) }
        out.extend(s.end()?); Ok(out)
    }
    fn lines(events: &[String]) -> String { events.iter().map(|e| format!("{e}\n")).collect() }
    fn bad(input: &str) { assert!(run(&[input.as_bytes()]).is_err(), "{input}") }

    #[test] fn empty_graph_round_trips() {
        let out = run(&[]).unwrap();
        assert_eq!(decode(&out).unwrap(), (vec![], vec![]));
        assert!(String::from_utf8(out).unwrap().ends_with("##! summary counts=0,0\n"));
    }
    #[test] fn symbols_only_and_edges_round_trip_field_for_field() {
        let only = run(&[lines(&[sym("a", "function"), sym("b", "class")]).as_bytes()]).unwrap();
        let (s, e) = decode(&only).unwrap();
        assert_eq!((s.len(), e.len()), (2, 0));
        let input = lines(&[sym("a", "function"), sym("b", "class"), edge("a", "b"), edge("b", "b")]);
        let (s, e) = decode(&run(&[input.as_bytes()]).unwrap()).unwrap();
        assert_eq!(s[0], Symbol { id: "a".into(), kind: "function".into(), name: "n a".into(), file: Some("src/a|b.rs".into()), line: Some(3), signature: None });
        assert_eq!(e[1], Edge { from: "b".into(), to: "b".into(), relation: "calls".into(), line: None });
        assert_eq!(e.len(), 2);
    }
    #[test] fn chunking_never_changes_output_even_inside_a_record() {
        let events: Vec<String> = (0..60).map(|i| sym(&format!("s{i}"), "method")).chain((1..60).map(|i| edge(&format!("s{i}"), "s0"))).collect();
        let input = lines(&events);
        let whole = run(&[input.as_bytes()]).unwrap();
        for size in [1usize, 7, 50, 333] {
            let chunks: Vec<&[u8]> = input.as_bytes().chunks(size).collect();
            assert_eq!(run(&chunks).unwrap(), whole, "chunk size {size}");
        }
        let (s, e) = decode(&whole).unwrap();
        assert_eq!((s.len(), e.len()), (60, 59));
        // provisional chunks concatenate to the final output
        let mut st = GraphStream::new(); let mut cat = Vec::new();
        for c in input.as_bytes().chunks(100) { cat.extend(st.push(c).unwrap()) }
        cat.extend(st.end().unwrap()); assert_eq!(cat, whole);
    }
    #[test] fn refusals() {
        bad(&lines(&[sym("a", "function"), edge("a", "missing")]));          // unknown endpoint
        bad(&lines(&[edge("a", "a")]));                                      // endpoint never declared
        bad(&lines(&[sym("a", "function"), sym("a", "class")]));             // duplicate identity
        bad(&lines(&[sym("a", "gadget")]));                                  // unknown kind
        bad(&lines(&[sym("a", "function"), edge("a", "a").replace("calls", "eats")])); // unknown relation
        bad(&lines(&[sym("a", "function").replace(r#""line":3"#, r#""line":3.5"#)]));  // float
        bad(&lines(&[sym("a", "function").replace(r#""line":3"#, r#""line":1e2"#)]));  // exponent
        bad(&lines(&[sym("a", "function").replace(r#""line":3"#, r#""line":-1"#)]));   // negative line
        bad(&lines(&[sym("a", "function").replace(r#""line":3"#, r#""line":9007199254740992"#)])); // unsafe
        bad(&lines(&[sym("a", "function").replace(r#""signature":null"#, r#""signature":null,"extra":1"#)])); // unknown field
        bad(&lines(&[sym("a", "function").replace(r#","signature":null"#, "")]));  // missing field is not the declared shape
        bad(&lines(&[sym("a", "function"), edge("a", "a"), sym("b", "function")]));  // symbol after edges
        bad(&lines(&[r#"{"op":"nope"}"#.to_owned()]));
        bad(&lines(&[sym("a", "function")]).replace('\n', "\n\n"));          // empty line
        assert!(run(&[sym("a", "function").as_bytes()]).is_err());            // ended early: unterminated record
        let mut big = GraphStream::new();
        let junk = vec![b'x'; MAX_BYTES + 1];
        assert!(big.push(&junk).is_err());
    }
    #[test] fn oversize_output_refused_and_refusal_is_terminal() {
        let mut s = GraphStream::new(); let mut failed = false;
        for i in 0..2000 { if s.push(format!("{}\n", sym(&format!("symbol-number-{i}"), "function")).as_bytes()).is_err() { failed = true; break } }
        assert!(failed);
        assert!(s.push(b"").is_err() && s.end().is_err());
        let mut t = GraphStream::new(); t.push(lines(&[sym("a", "class")]).as_bytes()).unwrap(); t.abort();
        assert!(t.end().is_err());
    }
    #[test] fn decoder_rejects_foreign_and_tampered_streams() {
        let out = run(&[lines(&[sym("a", "function"), edge("a", "a")]).as_bytes()]).unwrap();
        let text = String::from_utf8(out.clone()).unwrap();
        assert!(decode(text.replace("counts=1,1", "counts=1,2").as_bytes()).is_err());
        assert!(decode(text.replace("## edges", "## links").as_bytes()).is_err());
        assert!(decode(&out[..out.len() - 1]).is_err());
        assert!(decode(b"GCF profile=generic\na=1\n").is_err());
        // an edge to an undeclared symbol is not a graph, even when the generic frame is valid
        let dangling = text.replace("\"a\"|\"a\"|\"calls\"", "\"a\"|\"zzz\"|\"calls\"");
        assert_ne!(dangling, text);
        assert!(decode(dangling.as_bytes()).is_err());
    }
}
