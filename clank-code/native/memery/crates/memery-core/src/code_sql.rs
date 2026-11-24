//! Bounded static SQL syntax profile. No SQL is executed.
use super::{error,limit,EvidenceError,EvidenceErrorCode,ExtractionBudget};
#[derive(Clone,Debug)]
struct Token { start:usize,end:usize, word:bool }
#[derive(Clone,Debug)]
pub(super) struct Name { pub start:usize,pub end:usize,pub name:String }
fn bad(msg:&str)->EvidenceError {error(EvidenceErrorCode::InvalidSource,msg)}
fn lex(bytes:&[u8],budget:&ExtractionBudget,comments:bool)->Result<Vec<Token>,EvidenceError>{
 let(mut i,mut depth,mut out)=(0usize,0usize,Vec::new());
 while i<bytes.len(){limit(out.len()<budget.max_ast_nodes,"SQL token limit")?;let b=bytes[i];if b.is_ascii_whitespace(){i+=1;continue;}
  if b==b'-'&&bytes.get(i+1)==Some(&b'-'){if !comments{return Err(bad("SQL comment unsupported"));}i+=2;while i<bytes.len()&&bytes[i]!=b'\n'{i+=1;}continue;}
  if b==b'/'&&bytes.get(i+1)==Some(&b'*'){if !comments{return Err(bad("SQL comment unsupported"));}i+=2;let mut closed=false;while i<bytes.len(){if bytes[i]==b'/'&&bytes.get(i+1)==Some(&b'*'){return Err(bad("nested SQL comment unsupported"));}if bytes[i]==b'*'&&bytes.get(i+1)==Some(&b'/'){i+=2;closed=true;break;}i+=1;}if !closed{return Err(bad("unterminated SQL comment"));}continue;}
  let start=i;
  if b==b'\'' {i+=1;let mut closed=false;while i<bytes.len(){if bytes[i]==b'\''{i+=1;if bytes.get(i)==Some(&b'\''){i+=1;continue;}closed=true;break;}i+=1;}if !closed{return Err(bad("unterminated SQL string"));}out.push(Token{start,end:i,word:false});continue;}
  if matches!(b, b'"' | b'`' | b'[') {
      let closing = if b == b'[' { b']' } else { b };
      i += 1;
      loop {
          let ch = *bytes.get(i).ok_or_else(|| bad("unterminated SQL identifier"))?;
          i += 1;
          if ch != closing { continue; }
          if b != b'[' && bytes.get(i) == Some(&closing) { i += 1; continue; }
          break;
      }
      out.push(Token { start, end: i, word: true });
      continue;
  }
  if b == b'$' {
      // PostgreSQL migration bodies are opaque: a delimiter or CREATE inside
      // one cannot declare a top-level object. SQLite $parameters are tokens.
      i += 1;
      if bytes.get(i).is_some_and(|c| c.is_ascii_alphabetic() || *c == b'_') {
          while bytes.get(i).is_some_and(|c| c.is_ascii_alphanumeric() || *c == b'_') { i += 1; }
      }
      if bytes.get(i) == Some(&b'$') {
          i += 1;
          let tag = &bytes[start..i];
          limit(tag.len() <= 128, "SQL dollar quote tag limit")?;
          let close = bytes[i..].windows(tag.len()).position(|part| part == tag)
              .ok_or_else(|| bad("unterminated SQL dollar quote"))?;
          i += close + tag.len();
      }
      out.push(Token { start, end: i, word: false });
      continue;
  }
  if b.is_ascii_alphabetic()||b==b'_' {i+=1;while i<bytes.len()&&(bytes[i].is_ascii_alphanumeric()||bytes[i]==b'_'){i+=1;}out.push(Token{start,end:i,word:true});continue;}
  if !b.is_ascii(){return Err(bad("non-ASCII SQL unsupported"));}
  if b==b'(' {depth+=1;limit(depth<=budget.max_depth.min(128),"SQL nesting limit")?;}else if b==b')' {if depth==0{return Err(bad("unbalanced SQL parentheses"));}depth-=1;}
  i+=1;if matches!(b,b'<'|b'>'|b'!')&&bytes.get(i)==Some(&b'='){i+=1;}
  out.push(Token{start,end:i,word:false});
 }
 if depth!=0{return Err(bad("unbalanced SQL parentheses"));}Ok(out)
}
fn tok<'a>(bytes:&'a [u8],t:&Token)->&'a [u8]{&bytes[t.start..t.end]}
fn keyword(bytes:&[u8],t:&Token,w:&str)->bool{t.word&&tok(bytes,t).eq_ignore_ascii_case(w.as_bytes())}
fn sym(bytes:&[u8],t:&Token,v:&[u8])->bool{tok(bytes,t)==v}
fn id(bytes: &[u8], token: &Token) -> Name {
    let raw = tok(bytes, token);
    let name = match raw[0] {
        b'"' | b'`' => {
            let quote = raw[0] as char;
            String::from_utf8_lossy(&raw[1..raw.len()-1]).replace(&format!("{quote}{quote}"), &quote.to_string())
        }
        b'[' => String::from_utf8_lossy(&raw[1..raw.len()-1]).into_owned(),
        _ => String::from_utf8_lossy(raw).into_owned(),
    };
    Name { start: token.start, end: token.end, name: name.to_ascii_lowercase() }
}
pub(super) struct View {
    pub declaration: Name,
    pub table: Name,
    pub start: usize,
    pub end: usize,
}

pub(super) fn declarations(
    bytes: &[u8],
    budget: &ExtractionBudget,
) -> Result<(Vec<Name>, Vec<View>, usize), EvidenceError> {
    limit(bytes.len() <= budget.max_source_bytes, "SQL source limit")?;
    let mut tokens = lex(bytes, budget, true)?;
    // A migration's final statement may end at EOF without a semicolon.
    let implicit_end = tokens.last().is_some_and(|t| !sym(bytes, t, b";"));
    if implicit_end { tokens.push(Token { start: bytes.len(), end: bytes.len(), word: false }); }
    let (mut tables, mut views) = (Vec::new(), Vec::new());
    let (mut unsupported, mut begin, mut depth) = (0, 0, 0);
    for (i, token) in tokens.iter().enumerate() {
        if sym(bytes, token, b"(") {
            depth += 1;
        } else if sym(bytes, token, b")") {
            depth -= 1;
        }
        if depth != 0 || !(sym(bytes, token, b";") || (implicit_end && i + 1 == tokens.len())) {
            continue;
        }
        let row = &tokens[begin..i];
        begin = i + 1;
        if row.is_empty() {
            continue;
        }
        if row.len() < 4 || !keyword(bytes, &row[0], "create") {
            unsupported += 1;
            continue;
        }
        let mut name = 2;
        if row
            .get(name)
            .is_some_and(|token| keyword(bytes, token, "if"))
        {
            if !row
                .get(name + 1)
                .is_some_and(|token| keyword(bytes, token, "not"))
                || !row
                    .get(name + 2)
                    .is_some_and(|token| keyword(bytes, token, "exists"))
            {
                unsupported += 1;
                continue;
            }
            name += 3;
        }
        let Some(declaration) = row.get(name).filter(|token| token.word) else {
            unsupported += 1;
            continue;
        };
        if keyword(bytes, &row[1], "table")
            && row
                .get(name + 1)
                .is_some_and(|token| sym(bytes, token, b"("))
            && row.last().is_some_and(|token| sym(bytes, token, b")"))
            && {
                let mut nest = 0;
                row[name + 1..].iter().enumerate().all(|(at, token)| {
                    if sym(bytes, token, b"(") {
                        nest += 1;
                    }
                    if sym(bytes, token, b")") {
                        nest -= 1;
                    }
                    nest > 0 || at == row.len() - name - 2
                })
            }
        {
            limit(
                tables.len() + views.len() < budget.max_declarations,
                "SQL declaration limit",
            )?;
            tables.push(id(bytes, declaration));
            continue;
        }
        if keyword(bytes, &row[1], "view")
            && row
                .get(name + 1)
                .is_some_and(|token| keyword(bytes, token, "as"))
        {
            if let Some(table) = select_tokens(bytes, &row[name + 2..]) {
                limit(
                    tables.len() + views.len() < budget.max_declarations,
                    "SQL declaration limit",
                )?;
                views.push(View {
                    declaration: id(bytes, declaration),
                    table,
                    start: row[0].start,
                    end: token.end,
                });
                continue;
            }
        }
        unsupported += 1;
    }
    if begin != tokens.len() {
        return Err(bad("SQL statement boundary missing"));
    }
    Ok((tables, views, unsupported))
}
pub(super) fn select(bytes:&[u8],budget:&ExtractionBudget)->Option<Name>{
 let tokens=lex(bytes,budget,false).ok()?;
 select_tokens(bytes, &tokens)
}
fn select_tokens(bytes:&[u8],t:&[Token])->Option<Name>{
 let mut n=0usize;let take=|n:&mut usize|{let old=*n;*n+=1;t.get(old)};
 if !keyword(bytes,take(&mut n)?,"select"){return None;}
 let first=take(&mut n)?;if !first.word&&!sym(bytes,first,b"*"){return None;}
 if sym(bytes,first,b"*") && t.get(n).is_some_and(|v|sym(bytes,v,b",")){return None;}
 while t.get(n).is_some_and(|v|sym(bytes,v,b",")){n+=1;if !take(&mut n)?.word{return None;}}
 if !keyword(bytes,take(&mut n)?,"from"){return None;}let table=take(&mut n)?;if !table.word{return None;}let name=id(bytes,table);
 if t.get(n).is_some_and(|v|keyword(bytes,v,"where")){n+=1;loop{if !take(&mut n)?.word{return None;}if !matches!(tok(bytes,take(&mut n)?),b"="|b"!="|b"<"|b"<="|b">"|b">="){return None;}if !sym(bytes,take(&mut n)?,b"?"){return None;}if t.get(n).is_some_and(|v|keyword(bytes,v,"and")){n+=1;}else{break;}}}
 if t.get(n).is_some_and(|v|sym(bytes,v,b";")){n+=1;}if n==t.len(){Some(name)}else{None}
}
