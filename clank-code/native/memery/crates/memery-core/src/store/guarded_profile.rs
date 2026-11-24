//! Fixed source-defined writer fence conversion for a verified guarded profile.
use rusqlite::{Connection, OptionalExtension};

const V2: &str = "openclank.frankenmemory.writer.v2";
const V3: &str = "openclank.frankenmemory.writer.v3";
const FENCES: [&str; 3] = [include_str!("schema-v17-writer-fence.sql"),
    include_str!("schema-v17-principal-writer-fence.sql"), include_str!("schema-v17-media-writer-fence.sql")];

/// Caller must hold the verified v17 schema cookie in the same transaction, or
/// use the source-constructed in-memory canonical template. Never accept caller SQL.
pub(super) fn replace_verified_fences(conn: &Connection) -> rusqlite::Result<()> {
    // Refuse additional writer-capability triggers; a v2 guard must not remain
    // active after conversion under a different name.
    let allowed:std::collections::HashSet<&str>=FENCES.iter().flat_map(|source| source.lines()
        .filter_map(|line| line.strip_prefix("CREATE TRIGGER "))
        .filter_map(|line| line.split_whitespace().next())).collect();
    let mut stmt=conn.prepare("SELECT name FROM sqlite_schema WHERE type='trigger' AND (lower(name) GLOB 's03_writer_*' OR instr(lower(sql),'fm_writer_capability')>0)")?;
    let names=stmt.query_map([],|r|r.get::<_,String>(0))?.collect::<Result<Vec<_>,_>>()?;
    if names.iter().any(|name|!allowed.contains(name.as_str())) { return Err(rusqlite::Error::InvalidQuery); }
    drop(stmt);
    for (group, source) in FENCES.iter().enumerate() {
        let names: Vec<&str> = source.lines().filter_map(|line| line.strip_prefix("CREATE TRIGGER "))
            .map(|line| line.split_whitespace().next().unwrap_or("")).collect();
        if names.is_empty() || names.iter().any(|name| name.is_empty() || !name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')) {
            return Err(rusqlite::Error::InvalidQuery);
        }
        let mut definitions=Vec::new();
        for name in &names {
            let sql:Option<String>=conn.query_row("SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?1",[*name],|r|r.get(0)).optional()?;
            if let Some(sql)=sql { definitions.push((*name,sql)); }
        }
        if (group==0 && definitions.len()!=names.len()) || (!definitions.is_empty() && definitions.len()!=names.len()) {
            return Err(rusqlite::Error::InvalidQuery);
        }
        for (name,sql) in definitions {
            if sql.matches(V2).count()!=1 || sql.contains(V3) { return Err(rusqlite::Error::InvalidQuery); }
            conn.execute_batch(&format!("DROP TRIGGER \"{name}\";"))?;
            conn.execute_batch(&sql.replacen(V2,V3,1))?;
        }
    }
    Ok(())
}

/// The v18 actual writer namespace must equal the fixed source template.
pub(super) fn require_closed_writer_namespace(actual:&Connection, expected:&Connection) -> rusqlite::Result<()> {
    fn names(conn:&Connection)->rusqlite::Result<Vec<String>> {
        let mut stmt=conn.prepare("SELECT name FROM sqlite_schema WHERE type='trigger' AND (lower(name) GLOB 's03_writer_*' OR instr(lower(sql),'fm_writer_capability')>0) ORDER BY name")?;
        let result=stmt.query_map([],|r|r.get(0))?.collect();
        result
    }
    if names(actual)?!=names(expected)? {return Err(rusqlite::Error::InvalidQuery);}
    Ok(())
}
