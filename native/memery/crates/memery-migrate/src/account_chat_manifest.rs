//! Pure object catalog for the existing offline account-chat copy owner.
//! No application database is opened or mutated by this exporter.
use rusqlite::{params, Connection};

const TABLE: &str = "fm_v2_s05_job_refs";
const CURRENT: &str = include_str!("../../memery-core/src/store/current-schema.sql");
const V17: &str = include_str!("../../memery-core/src/store/schema-v17-additions.sql");
const AUTHORITY: &str = include_str!("../../memery-core/src/store/schema-v17-authority.sql");
const S05: &str = include_str!("../../memery-core/src/store/schema-v17-s05-additions.sql");
const S05_V4: &str = include_str!("../../memery-core/src/store/schema-v17-s05-account-v4.sql");
const PAIR: &str = include_str!("../../memery-core/src/store/schema-v17-s05-assistant-pair.sql");
const PAIR_BASE: &str = include_str!("../../memery-core/src/store/schema-v17-s05-assistant-pair-base.sql");
const PAIR_S08: &str = include_str!("../../memery-core/src/store/schema-v17-s05-assistant-pair-s08.sql");
const PAIR_PENDING: &str = include_str!("../../memery-core/src/store/schema-v17-s05-pair-pending.sql");
const S08: &str = include_str!("../../memery-core/src/store/schema-v17-s08.sql");
const S08_MODEL: &str = include_str!("../../memery-core/src/store/schema-v17-s08-model.sql");

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
#[derive(Clone, PartialEq, Eq)]
struct Object { kind: String, name: String, table: String, sql: String }
#[derive(Clone, PartialEq, Eq)]
struct Catalog { table: Object, owned: Vec<Object>, external: Vec<Object>, inbound: Vec<String>, columns: Vec<String> }
struct Variant { pair: bool, pending: bool, s08: bool, s08_model: bool, old: Catalog, target: Catalog }

fn compose(account_chat: bool, pair: bool, pending: bool, s08: bool, s08_model: bool) -> Result<Connection> {
    let db = Connection::open_in_memory()?;
    for sql in [CURRENT, V17, AUTHORITY] { db.execute_batch(sql)?; }
    if account_chat { db.execute_batch(&memery_core::store::sqlite::s05_account_chat_base_sql())?; }
    else { db.execute_batch(S05)?; }
    db.execute_batch(S05_V4)?;
    if s08 { db.execute_batch(S08)?; }
    if pair {
        if account_chat { db.execute_batch(&memery_core::store::sqlite::s05_account_chat_pair_sql())?; }
        else { db.execute_batch(PAIR)?; }
        db.execute_batch(if s08 { PAIR_S08 } else { PAIR_BASE })?;
        if pending { db.execute_batch(PAIR_PENDING)?; }
    } else if pending { return Err("pending profile requires paired profile".into()); }
    if s08_model {
        if !s08 {return Err("S08 model requires S08".into());}
        db.execute_batch(S08_MODEL)?;
    }
    Ok(db)
}

fn object(row: &rusqlite::Row<'_>) -> rusqlite::Result<Object> {
    Ok(Object { kind: row.get(0)?, name: row.get(1)?, table: row.get(2)?, sql: row.get(3)? })
}
fn catalog(db: &Connection) -> Result<Catalog> {
    let table = db.query_row("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE type='table' AND name=?1 AND sql IS NOT NULL", [TABLE], object)?;
    let mut statement = db.prepare("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE tbl_name=?1 AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name")?;
    let owned = statement.query_map([TABLE], object)?.collect::<rusqlite::Result<Vec<_>>>()?;
    // Enumerate external schema text too; the copier must reject unknown
    // dependencies, not infer that this list is globally exhaustive.
    let mut statement = db.prepare("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL AND tbl_name!=?1 AND instr(lower(sql),?1)>0 ORDER BY type,name")?;
    let external = statement.query_map([TABLE], object)?.collect::<rusqlite::Result<Vec<_>>>()?;
    let mut statement = db.prepare("SELECT name FROM pragma_table_info(?1) ORDER BY cid")?;
    let columns = statement.query_map([TABLE], |row| row.get(0))?.collect::<rusqlite::Result<Vec<String>>>()?;
    let mut statement = db.prepare("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")?;
    let tables = statement.query_map([], |row| row.get(0))?.collect::<rusqlite::Result<Vec<String>>>()?;
    let mut inbound = Vec::new();
    for name in tables {
        let mut statement = db.prepare("SELECT count(*) FROM pragma_foreign_key_list(?1) WHERE lower(\"table\")=?2")?;
        let count: i64 = statement.query_row(params![name, TABLE], |row| row.get(0))?;
        if count > 0 { inbound.push(name); }
    }
    Ok(Catalog { table, owned, external, inbound, columns })
}

// SQLite's JSON encoder supplies string escaping; no runtime or build-time
// dependency on a private reference, extra package, or SQL text parser.
fn quoted(db: &Connection, value: &str) -> Result<String> {
    Ok(db.query_row("SELECT json_quote(?1)", [value], |row| row.get(0))?)
}
fn object_json(db: &Connection, value: &Object) -> Result<String> {
    Ok(format!("{{\"type\":{},\"name\":{},\"table\":{},\"sql\":{}}}",
        quoted(db,&value.kind)?,quoted(db,&value.name)?,quoted(db,&value.table)?,quoted(db,&value.sql)?))
}
fn objects_json(db: &Connection, values: &[Object]) -> Result<String> {
    Ok(format!("[{}]", values.iter().map(|v|object_json(db,v)).collect::<Result<Vec<_>>>()?.join(",")))
}
fn strings_json(db: &Connection, values: &[String]) -> Result<String> {
    Ok(format!("[{}]", values.iter().map(|v|quoted(db,v)).collect::<Result<Vec<_>>>()?.join(",")))
}
fn catalog_json(db: &Connection, value: &Catalog) -> Result<String> {
    Ok(format!("{{\"table\":{},\"owned\":{},\"external\":{},\"inbound_foreign_key_tables\":{}}}",
        object_json(db,&value.table)?,objects_json(db,&value.owned)?,objects_json(db,&value.external)?,strings_json(db,&value.inbound)?))
}

pub fn render() -> Result<String> {
    let mut variants: Vec<Variant> = Vec::new();
    for (pair,pending) in [(false,false),(true,false),(true,true)] {
        for (s08,s08_model) in [(false,false),(true,false),(true,true)] {
            let old=catalog(&compose(false,pair,pending,s08,s08_model)?)?;
            let target=catalog(&compose(true,pair,pending,s08,s08_model)?)?;
            if old.columns!=target.columns || old.columns.len()!=21 {return Err("S05 column contract unavailable".into());}
            variants.push(Variant {pair,pending,s08,s08_model,old,target});
        }
    }
    let quote_db=Connection::open_in_memory()?;
    let columns=strings_json(&quote_db,&variants[0].old.columns)?;
    // Deduplicate only exactly identical old AND target catalogs.
    let mut groups: Vec<(Catalog,Catalog,Vec<(bool,bool,bool,bool)>)> = Vec::new();
    for variant in variants {
        let selector=(variant.pair,variant.pending,variant.s08,variant.s08_model);
        if let Some(group)=groups.iter_mut().find(|(old,target,_)|*old==variant.old && *target==variant.target) {
            group.2.push(selector);
        } else { groups.push((variant.old,variant.target,vec![selector])); }
    }
    let mut encoded=Vec::new();
    for (old,target,selectors) in groups {
        let choices=selectors.iter().map(|(pair,pending,s08,s08_model)|format!("{{\"paired\":{pair},\"pending\":{pending},\"s08\":{s08},\"s08_model\":{s08_model}}}")).collect::<Vec<_>>().join(",");
        encoded.push(format!("{{\"selectors\":[{}],\"old\":{},\"target\":{}}}",choices,catalog_json(&quote_db,&old)?,catalog_json(&quote_db,&target)?));
    }
    Ok(format!("{{\"version\":1,\"profile\":\"s05-account-chat\",\"s05_version\":4,\"marker\":{{\"key\":\"openclank.s05.account_chat.v1\",\"value\":\"1\"}},\"columns\":{},\"rowid\":\"preserve\",\"autoindexes\":\"table_owned\",\"variants\":[{}]}}\n",columns,encoded.join(",")))
}
