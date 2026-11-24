//! Bounded ordered JSON input for the safe-integer GCF policy.
use std::collections::HashSet;

pub const MAX_BYTES: usize = 50 * 1024;
const MAX_VALUES: usize = 16_384;
const MAX_DEPTH: usize = 32;
const MAX_INTEGER: i64 = 9_007_199_254_740_991;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Value { Null, Bool(bool), Integer(i64), String(String), Array(Vec<Value>), Object(Vec<(String,Value)>) }
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Reject { TooLarge, InvalidJson, InvalidUnicode, DuplicateKey, NumericDomain, TooDeep, TooManyValues }

pub fn parse(input: &[u8]) -> Result<Value, Reject> {
    if input.len() > MAX_BYTES { return Err(Reject::TooLarge); }
    let text = std::str::from_utf8(input).map_err(|_| Reject::InvalidUnicode)?;
    let mut p = Parser { text, s: text.as_bytes(), at: 0, values: 0 };
    p.ws(); let value = p.value(1)?; p.ws();
    if p.at != p.s.len() { return Err(Reject::InvalidJson); }
    Ok(value)
}
struct Parser<'a> { text: &'a str, s: &'a [u8], at: usize, values: usize }
impl Parser<'_> {
    fn ws(&mut self) { while matches!(self.s.get(self.at), Some(b' ' | b'\n' | b'\r' | b'\t')) { self.at += 1; } }
    fn byte(&mut self, c: u8) -> Result<(),Reject> { if self.s.get(self.at)==Some(&c) { self.at+=1; Ok(()) } else { Err(Reject::InvalidJson) } }
    fn value(&mut self, depth: usize) -> Result<Value,Reject> {
        if depth > MAX_DEPTH { return Err(Reject::TooDeep); }
        self.values += 1; if self.values > MAX_VALUES { return Err(Reject::TooManyValues); }
        match self.s.get(self.at).copied() {
            Some(b'n') => { self.word(b"null")?; Ok(Value::Null) },
            Some(b't') => { self.word(b"true")?; Ok(Value::Bool(true)) },
            Some(b'f') => { self.word(b"false")?; Ok(Value::Bool(false)) },
            Some(b'"') => Ok(Value::String(self.string()?)),
            Some(b'[') => self.array(depth), Some(b'{') => self.object(depth),
            Some(b'-' | b'0'..=b'9') => self.number(), _ => Err(Reject::InvalidJson),
        }
    }
    fn word(&mut self, word: &[u8]) -> Result<(),Reject> { if self.s.get(self.at..self.at+word.len())==Some(word) { self.at+=word.len(); Ok(()) } else { Err(Reject::InvalidJson) } }
    fn number(&mut self) -> Result<Value,Reject> {
        let start=self.at; if self.s.get(self.at)==Some(&b'-') { self.at+=1; }
        match self.s.get(self.at) {
            Some(b'0') => { self.at+=1; if self.s.get(self.at).is_some_and(u8::is_ascii_digit) { return Err(Reject::InvalidJson); } },
            Some(b'1'..=b'9') => { self.at+=1; while self.s.get(self.at).is_some_and(u8::is_ascii_digit) { self.at+=1; } },
            _ => return Err(Reject::InvalidJson),
        }
        if matches!(self.s.get(self.at),Some(b'.'|b'e'|b'E')) { return Err(Reject::NumericDomain); }
        let raw=std::str::from_utf8(&self.s[start..self.at]).map_err(|_| Reject::InvalidUnicode)?;
        if raw=="-0" { return Err(Reject::NumericDomain); }
        let n=raw.parse::<i64>().map_err(|_| Reject::NumericDomain)?;
        if !(-MAX_INTEGER..=MAX_INTEGER).contains(&n) { return Err(Reject::NumericDomain); }
        Ok(Value::Integer(n))
    }
    fn hex4(&mut self) -> Result<u16,Reject> {
        let mut n=0u16; for _ in 0..4 { let b=*self.s.get(self.at).ok_or(Reject::InvalidJson)?; self.at+=1;
            n=n*16 + match b { b'0'..=b'9'=>(b-b'0') as u16,b'a'..=b'f'=>(b-b'a'+10) as u16,b'A'..=b'F'=>(b-b'A'+10) as u16,_=>return Err(Reject::InvalidJson) }; } Ok(n)
    }
    fn string(&mut self) -> Result<String,Reject> {
        self.byte(b'"')?; let mut out=String::new();
        loop { let b=*self.s.get(self.at).ok_or(Reject::InvalidJson)?;
            match b {
                b'"' => { self.at+=1; return Ok(out); },
                0..=31 => return Err(Reject::InvalidJson),
                b'\\' => { self.at+=1; let esc=*self.s.get(self.at).ok_or(Reject::InvalidJson)?; self.at+=1;
                    match esc { b'"'=>out.push('"'),b'\\'=>out.push('\\'),b'/'=>out.push('/'),b'b'=>out.push('\u{8}'),b'f'=>out.push('\u{c}'),b'n'=>out.push('\n'),b'r'=>out.push('\r'),b't'=>out.push('\t'),
                        b'u'=> { let a=self.hex4()?; let cp=if (0xd800..=0xdbff).contains(&a) {
                            if self.s.get(self.at..self.at+2)!=Some(b"\\u") { return Err(Reject::InvalidUnicode); } self.at+=2; let b=self.hex4()?;
                            if !(0xdc00..=0xdfff).contains(&b) { return Err(Reject::InvalidUnicode); }
                            0x10000+(((a as u32)-0xd800)<<10)+(b as u32-0xdc00)
                        } else if (0xdc00..=0xdfff).contains(&a) { return Err(Reject::InvalidUnicode); } else { a as u32 };
                        out.push(char::from_u32(cp).ok_or(Reject::InvalidUnicode)?); },
                        _=>return Err(Reject::InvalidJson) }
                },
                _ => { let remaining=self.text.get(self.at..).ok_or(Reject::InvalidUnicode)?;
                    let c=remaining.chars().next().ok_or(Reject::InvalidJson)?; self.at+=c.len_utf8(); out.push(c); }
            }
        }
    }
    fn array(&mut self, depth: usize) -> Result<Value,Reject> {
        self.byte(b'[')?;self.ws();let mut out=Vec::new();if self.s.get(self.at)==Some(&b']') { self.at+=1;return Ok(Value::Array(out)); }
        loop { self.ws();out.push(self.value(depth+1)?);self.ws(); match self.s.get(self.at) { Some(b',')=>{self.at+=1},Some(b']')=>{self.at+=1;return Ok(Value::Array(out))},_=>return Err(Reject::InvalidJson) } }
    }
    fn object(&mut self, depth: usize) -> Result<Value,Reject> {
        self.byte(b'{')?;self.ws();let mut out=Vec::new();let mut keys=HashSet::new();if self.s.get(self.at)==Some(&b'}') { self.at+=1;return Ok(Value::Object(out)); }
        loop { self.ws();let key=self.string()?;if !keys.insert(key.clone()) {return Err(Reject::DuplicateKey)} self.ws();self.byte(b':')?;self.ws();let value=self.value(depth+1)?;out.push((key,value));self.ws();match self.s.get(self.at) {Some(b',')=>{self.at+=1},Some(b'}')=>{self.at+=1;return Ok(Value::Object(out))},_=>return Err(Reject::InvalidJson)} }
    }
}
