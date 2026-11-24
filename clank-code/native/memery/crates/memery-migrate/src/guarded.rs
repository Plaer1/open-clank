//! Explicit copy-only v16 to guarded-v17 preparation. Never replaces the source.
use rusqlite::{types::ValueRef, Connection, OpenFlags, OptionalExtension, functions::FunctionFlags};
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, fs::{self, OpenOptions}, io::{self, Read, Write}, path::{Path,PathBuf}};
#[cfg(target_os="linux")]
use std::os::unix::fs::MetadataExt;
use memery_core::store::sqlite::SqliteStore;
type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
const BASE: [&str; 4] = [
    include_str!("../../memery-core/src/store/schema-v17-additions.sql"),
    include_str!("../../memery-core/src/store/schema-v17-authority.sql"),
    include_str!("../../memery-core/src/store/schema-v17-s05-additions.sql"),
    include_str!("../../memery-core/src/store/schema-v17-writer-fence.sql"),
];
const FEEDBACK: [&str; 4] = [
    include_str!("../../memery-core/src/store/schema-v17-s06-additions.sql"),
    include_str!("../../memery-core/src/store/schema-v17-s06-admitted.sql"),
    include_str!("../../memery-core/src/store/schema-v17-s06-erase.sql"),
    include_str!("../../memery-core/src/store/schema-v17-s06-context.sql"),
];
fn quoted(name:&str)->String {format!("\"{}\"",name.replace('"',"\"\""))}
fn health(db:&Connection)->Result<()> {
    let integrity:String=db.query_row("PRAGMA integrity_check",[],|r|r.get(0))?;
    if integrity!="ok" || db.prepare("PRAGMA foreign_key_check")?.query([])?.next()?.is_some() {return Err("database integrity or foreign keys unavailable".into())} Ok(())
}
fn file_sha256(path:&Path)->Result<Vec<u8>> {
    let mut file=fs::File::open(path)?;let mut hasher=Sha256::new();let mut buffer=[0u8;65536];
    loop {let count=file.read(&mut buffer)?;if count==0 {break;}hasher.update(&buffer[..count]);}
    Ok(hasher.finalize().to_vec())
}

fn source_wal_sha256(path:&Path)->Result<Option<Vec<u8>>> {
    let mut name=path.as_os_str().to_os_string();name.push("-wal");let wal=PathBuf::from(name);
    match fs::symlink_metadata(&wal) {
        Ok(meta) if meta.file_type().is_file()=>Ok(Some(file_sha256(&wal)?)),
        Ok(_)=>Err("source WAL identity unavailable".into()),
        Err(error) if error.kind()==io::ErrorKind::NotFound=>Ok(None),
        Err(error)=>Err(error.into()),
    }
}

/// Only the exact first-party FTS5 namespace may be rebuilt. This proves the
/// schema/configuration, not the correctness of the old derived contents.
fn legacy_document_fts(db:&Connection)->Result<bool> {
    fn objects(db:&Connection)->Result<Vec<(String,String,String,Option<String>)>> {
        let mut stmt=db.prepare("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE lower(name) GLOB 'fm_v2_chunks_fts*' OR lower(tbl_name) GLOB 'fm_v2_chunks_fts*' ORDER BY type,name")?;
        let rows=stmt.query_map([],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?.collect::<rusqlite::Result<Vec<_>>>()?;Ok(rows)
    }
    fn tables(db:&Connection)->Result<Vec<(String,String,i64,i64,i64)>> {
        let mut stmt=db.prepare("SELECT name,type,ncol,wr,strict FROM pragma_table_list WHERE schema='main' AND lower(name) GLOB 'fm_v2_chunks_fts*' ORDER BY name")?;
        let rows=stmt.query_map([],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?)))?.collect::<rusqlite::Result<Vec<_>>>()?;Ok(rows)
    }
    let found=objects(db)?;if found.is_empty(){return Ok(false);}
    let template=Connection::open_in_memory()?;
    template.execute_batch(include_str!("../../memery-core/src/store/schema-v17-document-fts.sql"))?;
    if found!=objects(&template)? || tables(db)?!=tables(&template)? {return Err("legacy document FTS schema unavailable".into());}
    let external:bool=db.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE sql IS NOT NULL AND lower(name) NOT GLOB 'fm_v2_chunks_fts*' AND lower(tbl_name) NOT GLOB 'fm_v2_chunks_fts*' AND instr(lower(sql),'fm_v2_chunks_fts')>0)",[],|r|r.get(0))?;
    if external {return Err("legacy document FTS has external dependency".into());}
    let names:Vec<String>={let mut stmt=db.prepare("SELECT name FROM pragma_table_list WHERE schema='main' AND type IN ('table','virtual') AND name NOT LIKE 'sqlite_%'")?;let rows=stmt.query_map([],|r|r.get(0))?.collect::<rusqlite::Result<_>>()?;rows};
    for name in names {
        if name.to_ascii_lowercase().starts_with("fm_v2_chunks_fts") {continue;}
        let mut stmt=db.prepare(&format!("PRAGMA foreign_key_list({})",quoted(&name)))?;
        let mut rows=stmt.query([])?;while let Some(row)=rows.next()? {let target:String=row.get(2)?;if target.to_ascii_lowercase().starts_with("fm_v2_chunks_fts") {return Err("legacy document FTS has foreign-key dependency".into());}}
    }
    fn config(db:&Connection)->Result<Vec<(String,String,String,String)>> {
        let mut stmt=db.prepare("SELECT typeof(k),quote(k),typeof(v),quote(v) FROM fm_v2_chunks_fts_config ORDER BY k")?;
        let rows=stmt.query_map([],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?.collect::<rusqlite::Result<Vec<_>>>()?;Ok(rows)
    }
    if config(db)?!=config(&template)? {return Err("legacy document FTS configuration unavailable".into());}
    Ok(true)
}

// Hash ordered, typed values of every original non-shadow table, including FTS
// virtual tables. Retain the original column list across additive conversion.
fn inventory(db:&Connection, columns:Option<&BTreeMap<String,Vec<String>>>, additions:Option<&BTreeMap<String,Vec<String>>>) -> Result<(BTreeMap<String,Vec<String>>,BTreeMap<String,(u64,Vec<u8>)>)> {
    let names:Vec<String>=if let Some(c)=columns {c.keys().cloned().collect()} else {
        let mut s=db.prepare("SELECT name FROM pragma_table_list WHERE schema='main' AND type IN ('table','virtual') AND (name NOT LIKE 'sqlite_%' OR name='sqlite_sequence') ORDER BY name")?;
        let rows=s.query_map([],|r|r.get(0))?.collect::<rusqlite::Result<_>>()?;rows
    };
    let mut defs=BTreeMap::new();let mut hashes=BTreeMap::new();
    for name in names {
        let cols=if let Some(c)=columns {c[&name].clone()} else {
            let mut s=db.prepare(&format!("PRAGMA table_info({})",quoted(&name)))?;
            let rows=s.query_map([],|r|r.get::<_,String>(1))?.collect::<rusqlite::Result<Vec<_>>>()?;rows
        };
        if cols.is_empty() {return Err("unsupported empty table".into())}
        let list=cols.iter().map(|v|quoted(v)).collect::<Vec<_>>().join(",");
        // Omit only specifically authorized newly inserted key rows. Original
        // keys are never omitted: callers check their absence in the backup.
        let filter=if let Some(keys)=additions.and_then(|a|a.get(&name)) {
            let column=if name=="fm_meta" {"key"} else if name=="fm_v2_predicates" {"predicate"} else {return Err("unsupported inventory addition".into())};
            format!(" WHERE ({} IS NULL OR {} NOT IN ({}))",quoted(column),quoted(column),keys.iter().map(|v|format!("'{}'",v.replace("'", "''"))).collect::<Vec<_>>().join(","))
        } else {String::new()};
        let without_rowid:i64=db.query_row("SELECT wr FROM pragma_table_list WHERE schema='main' AND name=?1",[&name],|r|r.get(0))?;
        let rowid=if without_rowid==0 {
            Some(["rowid","_rowid_","oid"].into_iter().find(|candidate|!cols.iter().any(|c|c.eq_ignore_ascii_case(candidate))).ok_or("rowid aliases shadowed")?)
        } else {None};
        let projection=if let Some(alias)=rowid {format!("{},{}",quoted(alias),list)} else {list.clone()};
        let order=if let Some(alias)=rowid {quoted(alias)} else {list.clone()};
        let mut stmt=db.prepare(&format!("SELECT {projection} FROM {}{filter} ORDER BY {order}",quoted(&name)))?;
        let mut rows=stmt.query([])?;let mut hash=Sha256::new();let mut count=0u64;
        while let Some(row)=rows.next()? { count+=1; for i in 0..(cols.len()+if rowid.is_some(){1}else{0}) {
            let (tag,bytes):(u8,Vec<u8>)=match row.get_ref(i)? {
                ValueRef::Null=>(0,vec![]),ValueRef::Integer(v)=>(1,v.to_be_bytes().to_vec()),
                ValueRef::Real(v)=>(2,v.to_bits().to_be_bytes().to_vec()),ValueRef::Text(v)=>(3,v.to_vec()),ValueRef::Blob(v)=>(4,v.to_vec())};
            hash.update([tag]);hash.update((bytes.len() as u64).to_be_bytes());hash.update(bytes);
        }}
        defs.insert(name.clone(),cols);hashes.insert(name,(count,hash.finalize().to_vec()));
    } Ok((defs,hashes))
}
pub fn prepare(source:&str,backup:&str,shadow:&str,feedback:bool,s08:bool,maintenance:bool,policy:bool,queued:bool,s09:bool,domains:bool,s05_account_v4:bool,s02:bool,s02v2:bool,pair:bool,pair_pending:bool,s08_model:bool,s08_model_v2:bool,knowledge_ref:bool,general_hex:bool,account_chat:bool,document_fts:bool,skill_draft:bool)->Result<()> {
    let source=Path::new(source);let backup=Path::new(backup);let shadow=Path::new(shadow);
    let mut receipt_name=shadow.as_os_str().to_os_string();receipt_name.push(".receipt");let receipt=PathBuf::from(receipt_name);
    if !source.is_absolute() || !backup.is_absolute() || !shadow.is_absolute() || source==backup || source==shadow || backup==shadow
        || !source.is_file() || source.canonicalize()?!=source || fs::symlink_metadata(backup).is_ok() || fs::symlink_metadata(shadow).is_ok() || fs::symlink_metadata(&receipt).is_ok()
        || backup.parent().ok_or("backup parent")?.canonicalize()?!=backup.parent().unwrap()
        || shadow.parent().ok_or("shadow parent")?.canonicalize()?!=shadow.parent().unwrap() {
        return Err("absolute canonical source and distinct new output paths required".into())
    }
    let mut journal=OpenOptions::new().write(true).create_new(true).open(&receipt)?;
    writeln!(journal,"format=openclank.guarded-shadow.v1\nstatus=started\nsource={}\nbackup={}\nshadow={}\nfeedback={}\ns08={}",source.display(),backup.display(),shadow.display(),feedback,s08)?;journal.sync_all()?;
    let source_before=fs::symlink_metadata(source)?;
    if !source_before.file_type().is_file() {return Err("source file changed".into());}
    let source_wal=source_wal_sha256(source)?;
    let original=Connection::open_with_flags(source,OpenFlags::SQLITE_OPEN_READ_ONLY|OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
    let data_version:i64=original.query_row("PRAGMA data_version",[],|r|r.get(0))?;
    let source_hash=file_sha256(source)?;
    if original.query_row("PRAGMA user_version",[],|r|r.get::<_,i64>(0))?!=16 {return Err("source must be v16".into())}
    health(&original)?;
    let id:String=original.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?;
    if id.is_empty() {return Err("persisted database ID unavailable".into())}
    // SQLite Backup copies the committed snapshot, including WAL content,
    // while preserving rowids in the explicitly reserved destination.
    let (source_columns,source_rows)=inventory(&original,None,None)?;
    let reserved=OpenOptions::new().write(true).create_new(true).open(backup)?;
    #[cfg(target_os="linux")]
    let backup_identity=(reserved.metadata()?.dev(),reserved.metadata()?.ino());
    reserved.sync_all()?;drop(reserved);
    let mut destination=Connection::open_with_flags(backup,OpenFlags::SQLITE_OPEN_READ_WRITE|OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
    {
        let copy=rusqlite::backup::Backup::new(&original,&mut destination)?;
        if copy.step(-1)?!=rusqlite::backup::StepResult::Done {return Err("source backup was busy or incomplete".into());}
    }
    // The backup must be a self-contained single file before it is copied to
    // the shadow. SQLite checkpoint/close owns any transient destination WAL.
    let mode:String=destination.query_row("PRAGMA journal_mode=DELETE",[],|r|r.get(0))?;
    if !mode.eq_ignore_ascii_case("delete") {return Err("backup journal could not be closed".into());}
    drop(destination);
    for suffix in ["-wal","-shm","-journal"] {let mut name=backup.as_os_str().to_os_string();name.push(suffix);if fs::symlink_metadata(PathBuf::from(name)).is_ok() {return Err("backup sidecar remains".into());}}
    fs::File::open(backup)?.sync_all()?;
    let after=fs::symlink_metadata(source)?;
    if !after.file_type().is_file() || source.canonicalize()?!=source || after.len()!=source_before.len()
        || after.modified()?!=source_before.modified()? || original.query_row("PRAGMA data_version",[],|r|r.get::<_,i64>(0))?!=data_version || file_sha256(source)?!=source_hash || source_wal_sha256(source)?!=source_wal {
        return Err("source changed during snapshot".into());
    }
    #[cfg(target_os="linux")]
    if (after.dev(),after.ino())!=(source_before.dev(),source_before.ino()) {return Err("source identity changed".into());}
    let metadata=fs::symlink_metadata(backup)?;
    if !metadata.file_type().is_file() {return Err("backup file changed".into())}
    #[cfg(target_os="linux")]
    if (metadata.dev(),metadata.ino())!=backup_identity {return Err("backup file changed".into())}
    let backup_hash=file_sha256(backup)?;
    let backup_db=Connection::open_with_flags(backup,OpenFlags::SQLITE_OPEN_READ_ONLY)?;health(&backup_db)?;
    let (mut columns,mut before)=inventory(&backup_db,None,None)?;
    if columns!=source_columns || before!=source_rows {return Err("source backup rows or rowids changed".into());}
    let rebuild_legacy_fts=if document_fts {legacy_document_fts(&backup_db)?} else {false};
    let old_fts=if rebuild_legacy_fts {Some(before.get("fm_v2_chunks_fts").cloned().ok_or("legacy document FTS inventory unavailable")?)} else {None};
    let mut allowed_meta=Vec::new();
    for (enabled,key) in [
        (skill_draft,"openclank.s05.skill_draft.v1"),(pair,"openclank.assistant_interpretation.v1"),(pair_pending,"openclank.assistant_interpretation.pending_rebind.v1"),(account_chat,"openclank.s05.account_chat.v1"),
        (policy,"openclank.policy_projection.v1"),(queued,"openclank.queued_source.v1"),
        (domains,"openclank.domain_metadata.v1"),(s02,"openclank.type_tag_policy.v1"),
        (s02v2,"openclank.type_tag_policy.v2"),(knowledge_ref,"openclank.s01.knowledge-ref.v1"),
        (s08_model,"openclank.s08.model.v1"),(s08_model_v2,"openclank.s08.model.v2"),(general_hex,"openclank.general_hex.v1"),(document_fts,"openclank.document_fts.v1"),
        (s09,"openclank.question_semantics.v1")
    ] {
        if enabled {
            let exists:bool=backup_db.query_row("SELECT EXISTS(SELECT 1 FROM fm_meta WHERE key=?)",[key],|r|r.get(0))?;
            if exists {return Err("requested profile marker already exists in source".into());}
            allowed_meta.push(key.to_owned());
        }
    }
    let original_composite=if columns.contains_key("fm_v2_predicates") {
        backup_db.query_row("SELECT EXISTS(SELECT 1 FROM fm_v2_predicates WHERE predicate='s08.composite')",[],|r|r.get::<_,bool>(0))?
    } else {false};
    let mut additions=BTreeMap::new();
    if !allowed_meta.is_empty() {additions.insert("fm_meta".to_owned(),allowed_meta.clone());}
    if s08_model && !original_composite && columns.contains_key("fm_v2_predicates") {additions.insert("fm_v2_predicates".to_owned(),vec!["s08.composite".to_owned()]);}
    let mut logical=Sha256::new();for (name,(count,hash)) in &before {logical.update((name.len() as u64).to_be_bytes());logical.update(name.as_bytes());logical.update(count.to_be_bytes());logical.update(hash);}
    let fingerprint=format!("{:x}",logical.finalize());
    // Only after exact namespace qualification is this projection exempted
    // from original-row comparison; canonical tables and rowids remain pinned.
    if rebuild_legacy_fts {columns.remove("fm_v2_chunks_fts");before.remove("fm_v2_chunks_fts");}

    let mut schema=Sha256::new();let mut stmt=backup_db.prepare("SELECT type,name,coalesce(sql,'') FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")?;
    let mut schema_rows=stmt.query([])?;while let Some(row)=schema_rows.next()? {for i in 0..3 {let value:String=row.get(i)?;schema.update((value.len() as u64).to_be_bytes());schema.update(value.as_bytes());}}drop(schema_rows);drop(stmt);drop(backup_db);
    let source_hash_hex=source_hash.iter().map(|b|format!("{b:02x}")).collect::<String>();
    let backup_hash_hex=backup_hash.iter().map(|b|format!("{b:02x}")).collect::<String>();
    writeln!(journal,"database_id={}\nsource_schema_version=16\nsource_sha256={}\nbackup_sha256={}\nschema_sha256={:x}\nlogical_sha256={}\nstatus=snapshot_verified",id,source_hash_hex,backup_hash_hex,schema.finalize(),fingerprint)?;
    if let Some((count,hash))=&old_fts {let hex=hash.iter().map(|b|format!("{b:02x}")).collect::<String>();writeln!(journal,"legacy_document_fts=rebuild\nlegacy_document_fts_rows={}\nlegacy_document_fts_sha256={}",count,hex)?;}
    journal.sync_all()?;
    let mut out=OpenOptions::new().write(true).create_new(true).open(shadow)?;
    io::copy(&mut fs::File::open(backup)?,&mut out)?;out.flush()?;out.sync_all()?;drop(out);
    let mut db=Connection::open_with_flags(shadow,OpenFlags::SQLITE_OPEN_READ_WRITE)?;
    // The offline copy connection owns only the exact first-party predicate seed.
    db.create_scalar_function("fm_writer_capability",0,FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC, |_| Ok("openclank.frankenmemory.writer.v2"))?;
    let tx=db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    for (i,sql) in BASE.iter().enumerate() {
        if i==2 && account_chat {tx.execute_batch(&memery_core::store::sqlite::s05_account_chat_base_sql())?;}
        else {tx.execute_batch(sql)?;}
    } if feedback {for sql in FEEDBACK {tx.execute_batch(sql)?;}}
    if s05_account_v4 {tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-s05-account-v4.sql"))?;}
    // Existing canonical optional companions need their matching writer fence;
    // guarded opener below verifies their exact definitions after preparation.
    let has_table=|name:&str| -> rusqlite::Result<bool> {tx.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?)",[name],|r|r.get(0))};
    if has_table("fm_v2_principal_bindings")? {
        tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-principal-writer-fence.sql"))?;
    }
    let assets=has_table("fm_v2_media_assets")?;let representations=has_table("fm_v2_media_representations")?;
    if assets!=representations {return Err("partial media companion unavailable".into());}
    if assets {tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-media-writer-fence.sql"))?;}
    if s08 {tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-s08.sql"))?;}
    if maintenance {tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-maintenance.sql"))?;}
    if pair_pending && !pair {return Err("assistant interpretation pending requires assistant interpretation".into());}
    if pair {
        if !s05_account_v4 {return Err("assistant interpretation requires S05 account v4".into());}
        if account_chat {tx.execute_batch(&memery_core::store::sqlite::s05_account_chat_pair_sql())?;} else {tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-s05-assistant-pair.sql"))?;}
        tx.execute_batch(if s08 {include_str!("../../memery-core/src/store/schema-v17-s05-assistant-pair-s08.sql")} else {include_str!("../../memery-core/src/store/schema-v17-s05-assistant-pair-base.sql")})?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.assistant_interpretation.v1','1')",[])?;
    }
    if pair_pending {tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-s05-pair-pending.sql"))?;}
    tx.pragma_update(None,"user_version",17)?;
    if account_chat {tx.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.s05.account_chat.v1','1')",[])?;}
    if policy {
        tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-policy-projection.sql"))?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.policy_projection.v1','1')",[])?;
    }
    if queued {
        tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-queued-source.sql"))?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.queued_source.v1','1')",[])?;
    }
    if domains {
        tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-domain-metadata.sql"))?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.domain_metadata.v1','1')",[])?;
    }
    if s02 {
        tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-s02-policy.sql"))?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.type_tag_policy.v1','1')",[])?;
    }
    if s02v2 {
        tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-s02-policy-v2.sql"))?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.type_tag_policy.v2','1')",[])?;
    }
    if knowledge_ref {
        if !(s02v2 && s02 && domains && policy) {return Err("knowledge reference prerequisites unavailable".into());}
        tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-knowledge-ref.sql"))?;
    }
    if s08_model {
        if !(s08 && s02 && policy && domains && feedback) {return Err("S08 model prerequisites unavailable".into());}
        let predicate:Option<(String,String,i64,i64)>=tx.query_row("SELECT value_type,cardinality,version,allow_unknown FROM fm_v2_predicates WHERE predicate='s08.composite'",[],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
        match predicate {
            None=>{tx.execute("INSERT INTO fm_v2_predicates(predicate,value_type,cardinality,version,allow_unknown) VALUES('s08.composite','object','single',1,0)",[])?;},
            Some((ref kind,ref cardinality,1,0)) if kind=="object" && cardinality=="single"=>{},
            _=>return Err("S08 summary predicate conflicts".into()),
        }
        tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-s08-model.sql"))?;
        // v2 is the same additive overlay s02-policy-v2 is: applied on this stopped copy only, marker inserted by the file itself.
        if s08_model_v2 {tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-s08-model-v2.sql"))?;}
    }
    if skill_draft {tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-s05-skill-draft.sql"))?;}
    if document_fts {
        let existing: i64 = tx.query_row("SELECT count(*) FROM sqlite_schema WHERE lower(name) GLOB 'fm_v2_chunks_fts*' OR lower(tbl_name) GLOB 'fm_v2_chunks_fts*'", [], |r| r.get(0))?;
        if rebuild_legacy_fts {
            if existing==0 || !legacy_document_fts(&tx)? {return Err("legacy document FTS changed".into());}
            tx.execute("INSERT INTO fm_v2_chunks_fts(fm_v2_chunks_fts) VALUES('integrity-check')",[])?;
            tx.execute_batch("DROP TABLE fm_v2_chunks_fts")?;
        } else if existing != 0 { return Err("document FTS namespace already exists".into()); }
        tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-document-fts.sql"))?;
        tx.execute("INSERT INTO fm_v2_chunks_fts(owner_id,chunk_id,text) SELECT owner_id,chunk_id,text FROM fm_v2_chunks", [])?;
        let mismatch: bool = tx.query_row("SELECT (SELECT count(*) FROM fm_v2_chunks) != (SELECT count(*) FROM fm_v2_chunks_fts) OR EXISTS(SELECT owner_id,chunk_id,text FROM fm_v2_chunks EXCEPT SELECT owner_id,chunk_id,text FROM fm_v2_chunks_fts) OR EXISTS(SELECT owner_id,chunk_id,text FROM fm_v2_chunks_fts EXCEPT SELECT owner_id,chunk_id,text FROM fm_v2_chunks)", [], |r| r.get(0))?;
        if mismatch { return Err("document FTS backfill mismatch".into()); }
        tx.execute("INSERT INTO fm_v2_chunks_fts(fm_v2_chunks_fts) VALUES('integrity-check')", [])?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.document_fts.v1','1')", [])?;
    }
    if general_hex {
        let existing:i64=tx.query_row("SELECT count(*) FROM sqlite_schema WHERE sql IS NOT NULL AND (name GLOB 'fm_general_hex_*' OR tbl_name GLOB 'fm_general_hex_*')",[],|r|r.get(0))?;
        if existing!=0 && existing!=5 {return Err("General Hex profile is partial or contains extra objects".into());}
        tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-general-hex.sql"))?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.general_hex.v1','1')",[])?;
    }
    if s09 {
        tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-s09-questions.sql"))?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.question_semantics.v1','1')",[])?;
    }
    // Prove every original typed row after every schema and seed operation;
    // only known new marker/seed keys absent from the backup may be omitted.
    for key in &allowed_meta {
        let value:Option<String>=tx.query_row("SELECT value FROM fm_meta WHERE key=?",[key],|r|r.get(0)).optional()?;
        if value.as_deref()!=Some("1") {return Err("requested profile marker unavailable".into());}
    }
    if inventory(&tx,Some(&columns),Some(&additions))?.1!=before {return Err("source row preservation check failed".into())}
    health(&tx)?;
    tx.commit()?;drop(db);
    let (store,fb,admitted,erase,_,_)=SqliteStore::open_guarded(shadow.to_str().ok_or("shadow path encoding")?,4,&id)?;
    if feedback && !(fb && admitted && erase) {return Err("optional feedback capability unavailable".into())} drop(store);
    let source_final=fs::symlink_metadata(source)?;let backup_final=fs::symlink_metadata(backup)?;
    if !source_final.file_type().is_file() || source.canonicalize()?!=source || source_final.len()!=source_before.len() || source_final.modified()?!=source_before.modified()? || file_sha256(source)?!=source_hash || original.query_row("PRAGMA data_version",[],|r|r.get::<_,i64>(0))?!=data_version || source_wal_sha256(source)?!=source_wal || !backup_final.file_type().is_file() || file_sha256(backup)?!=backup_hash {return Err("source or backup changed during preparation".into());}
    #[cfg(target_os="linux")]
    if (source_final.dev(),source_final.ino())!=(source_before.dev(),source_before.ino()) || (backup_final.dev(),backup_final.ino())!=backup_identity {return Err("source or backup identity changed".into());}
    drop(original);
    writeln!(journal,"status=shadow_verified\nshadow_schema_version=17\ns05_account_v4={}\npolicy_projection={}\nqueued_source={}\nquestion_semantics={}\ndomain_metadata={}\nassistant_interpretation={}\nassistant_interpretation_pending={}\ns08_model={}\nknowledge_ref={}\ngeneral_hex={}\naccount_chat={}\ndocument_fts={}\nsource_replaced=false",s05_account_v4,policy,queued,s09,domains,pair,pair_pending,s08_model,knowledge_ref,general_hex,account_chat,document_fts)?;journal.sync_all()?;
    println!("prepared shadow={} backup={} database_id={} feedback={} source_replaced=false; activation not performed",shadow.display(),backup.display(),id,feedback);Ok(())
}

/// Upgrade only a separately prepared, exact guarded-v17 snapshot; never mutate source.
pub fn prepare_v18(source:&str,backup:&str,shadow:&str,vector_profiles:bool,document_vector_intent:bool,skill_purge:bool,document_vector_batch:bool,quota_purge:bool,request_purge_empty:bool,s08_v2_skill_empty:bool,document_structure:bool)->Result<()> {
    let source=Path::new(source); let backup=Path::new(backup); let shadow=Path::new(shadow);
    let mut receipt_name=shadow.as_os_str().to_os_string(); receipt_name.push(".receipt"); let receipt=PathBuf::from(receipt_name);
    if !source.is_absolute() || !backup.is_absolute() || !shadow.is_absolute() || source==backup || source==shadow || backup==shadow
        || !source.is_file() || source.canonicalize()?!=source || fs::symlink_metadata(backup).is_ok() || fs::symlink_metadata(shadow).is_ok() || fs::symlink_metadata(&receipt).is_ok()
        || backup.parent().ok_or("backup parent")?.canonicalize()?!=backup.parent().unwrap()
        || shadow.parent().ok_or("shadow parent")?.canonicalize()?!=shadow.parent().unwrap() {return Err("absolute canonical source and distinct new output paths required".into());}
    let mut journal=OpenOptions::new().write(true).create_new(true).open(&receipt)?;
    writeln!(journal,"format=openclank.guarded-shadow.v1\nstatus=started\ntarget=18\nsource={}\nbackup={}\nshadow={}",source.display(),backup.display(),shadow.display())?; journal.sync_all()?;
    let source_before=fs::symlink_metadata(source)?; if !source_before.file_type().is_file(){return Err("source file changed".into());}
    let source_hash=file_sha256(source)?; let source_wal=source_wal_sha256(source)?;
    let original=Connection::open_with_flags(source,OpenFlags::SQLITE_OPEN_READ_ONLY|OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
    let data_version:i64=original.query_row("PRAGMA data_version",[],|r|r.get(0))?;
    let source_version:i64=original.query_row("PRAGMA user_version",[],|r|r.get(0))?;
    let existing_profile = skill_purge || document_vector_batch || quota_purge || request_purge_empty || s08_v2_skill_empty
        || (document_structure && source_version == 18);
    if source_version != if existing_profile {18} else {17} || ((skill_purge || document_vector_batch || quota_purge || request_purge_empty || s08_v2_skill_empty) && (vector_profiles || document_vector_intent)) || ((skill_purge as u8 + document_vector_batch as u8 + quota_purge as u8 + request_purge_empty as u8 + s08_v2_skill_empty as u8)>1) {return Err("source profile unavailable".into());}
    if document_structure {
        let prior:bool = original.query_row("SELECT EXISTS(SELECT 1 FROM fm_meta WHERE key='openclank.document_structure.v1')",
            [], |row| row.get(0))?;
        if prior || (source_version == 18 && (vector_profiles || document_vector_intent)) {
            return Err("document structure source unavailable".into());
        }
    }
    if vector_profiles || document_vector_intent {
        let fts:Option<String>=original.query_row("SELECT value FROM fm_meta WHERE key='openclank.document_fts.v1'",[],|r|r.get(0)).optional()?;
        let prior:bool=original.query_row("SELECT EXISTS(SELECT 1 FROM fm_meta WHERE key='openclank.vector_profiles.v1')",[],|r|r.get(0))?;
        if fts.as_deref()!=Some("1") || prior {return Err("vector profile source unavailable".into());}
    }
    if document_vector_intent {
        let prior:bool=original.query_row("SELECT EXISTS(SELECT 1 FROM fm_meta WHERE key='openclank.document_vector_build_intent.v1')",[],|r|r.get(0))?;
        if prior {return Err("document vector build intent source unavailable".into());}
    }
    if document_vector_batch {
        let intent:Option<String>=original.query_row("SELECT value FROM fm_meta WHERE key='openclank.document_vector_build_intent.v1'",[],|r|r.get(0)).optional()?;
        let prior:bool=original.query_row("SELECT EXISTS(SELECT 1 FROM fm_meta WHERE key='openclank.document_vector_batch.v2')",[],|r|r.get(0))?;
        if intent.as_deref()!=Some("1") || prior {return Err("document vector batch source unavailable".into());}
    }
    if s08_v2_skill_empty {
        let valid:bool=original.query_row("SELECT (SELECT value FROM fm_meta WHERE key='openclank.s08.model.v1')='1' AND (SELECT value FROM fm_meta WHERE key='openclank.assistant_interpretation.v1')='1' AND (SELECT count(*) FROM fm_v2_s05_schema WHERE version=4)=1 AND (SELECT count(*) FROM fm_v2_s05_schema)=1 AND NOT EXISTS(SELECT 1 FROM fm_meta WHERE key IN ('openclank.s08.model.v2','openclank.s05.skill_draft.v1')) AND NOT EXISTS(SELECT 1 FROM fm_v2_jobs WHERE kind IN ('skill_draft','access_consolidation')) AND NOT EXISTS(SELECT 1 FROM fm_v2_outbox WHERE kind GLOB 'skill_draft*' OR kind GLOB 's08_*') AND NOT EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE reason GLOB 's08.*' OR reason GLOB 'skill*draft*')",[],|r|r.get::<_,Option<bool>>(0))?.unwrap_or(false);
        if !valid {return Err("empty S08 v2 and skill profile source unavailable".into());}
    }
    if request_purge_empty {
        let marker:Option<String>=original.query_row("SELECT value FROM fm_meta WHERE key='openclank.native_request.purge_terminals.v1'",[],|r|r.get(0)).optional()?;
        let objects:i64=original.query_row("SELECT count(*) FROM sqlite_schema WHERE name GLOB 'native_request_purge_*' OR tbl_name='native_request_purge_terminal'",[],|r|r.get(0))?;
        let transitions:bool=original.query_row("SELECT EXISTS(SELECT 1 FROM fm_meta WHERE key GLOB 'owner_transition_*')",[],|r|r.get(0))?;
        if marker.is_some() || objects!=0 || transitions {return Err("empty request terminal profile source unavailable".into());}
    }
    if skill_purge {
        let request:Option<String>=original.query_row("SELECT value FROM fm_meta WHERE key='openclank.native_request.purge_terminals.v1'",[],|r|r.get(0)).optional()?;
        let skill:Option<String>=original.query_row("SELECT value FROM fm_meta WHERE key='openclank.s05.skill_draft.v1'",[],|r|r.get(0)).optional()?;
        let prior:bool=original.query_row("SELECT EXISTS(SELECT 1 FROM fm_meta WHERE key='openclank.s05.skill_purge.v1')",[],|r|r.get(0))?;
        if request.as_deref()!=Some("1") || skill.as_deref()!=Some("1") || prior {return Err("skill draft purge profile source unavailable".into());}
    }
    if quota_purge {
        let marker:Option<String>=original.query_row("SELECT value FROM fm_meta WHERE key='openclank.s05.skill_purge.v1'",[],|r|r.get(0)).optional()?;
        let prior:bool=original.query_row("SELECT EXISTS(SELECT 1 FROM fm_meta WHERE key IN ('openclank.s05.quota_purge.v1','openclank.s05.quota_cutoff.v1') OR key GLOB 's05_quota_batch_*' OR key GLOB 's05_quota_*_receipt_*')",[],|r|r.get(0))?;
        if marker.as_deref()!=Some("1") || prior {return Err("S05 quota profile source unavailable".into());}
    }
    health(&original)?; let id:String=original.query_row("SELECT value FROM fm_meta WHERE key='database_id'",[],|r|r.get(0))?; if id.is_empty(){return Err("database identity unavailable".into());}
    let (columns,rows)=inventory(&original,None,None)?;
    let reserved=OpenOptions::new().write(true).create_new(true).open(backup)?;
    #[cfg(target_os="linux")] let backup_identity=(reserved.metadata()?.dev(),reserved.metadata()?.ino());
    reserved.sync_all()?; drop(reserved);
    let mut saved=Connection::open_with_flags(backup,OpenFlags::SQLITE_OPEN_READ_WRITE|OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
    {let copy=rusqlite::backup::Backup::new(&original,&mut saved)?; if copy.step(-1)?!=rusqlite::backup::StepResult::Done{return Err("backup incomplete".into());}}
    let mode:String=saved.query_row("PRAGMA journal_mode=DELETE",[],|r|r.get(0))?; if !mode.eq_ignore_ascii_case("delete"){return Err("backup journal unavailable".into());} drop(saved);
    for suffix in ["-wal","-shm","-journal"] {let mut name=backup.as_os_str().to_os_string();name.push(suffix);if fs::symlink_metadata(PathBuf::from(name)).is_ok(){return Err("backup sidecar remains".into());}}
    fs::File::open(backup)?.sync_all()?;
    let backup_hash=file_sha256(backup)?; let saved=Connection::open_with_flags(backup,OpenFlags::SQLITE_OPEN_READ_ONLY)?; health(&saved)?;
    if inventory(&saved,None,None)?!=(columns.clone(),rows.clone()){return Err("backup rows changed".into());} drop(saved);
    let (verified,_,_,_,_,_)=SqliteStore::open_guarded(backup.to_str().ok_or("backup path encoding")?,4,&id)?; drop(verified);
    if file_sha256(backup)?!=backup_hash{return Err("backup changed during verification".into());}
    let reserved=OpenOptions::new().write(true).create_new(true).open(shadow)?;
    #[cfg(target_os="linux")] let shadow_identity=(reserved.metadata()?.dev(),reserved.metadata()?.ino());
    reserved.sync_all()?; drop(reserved);
    {let saved=Connection::open_with_flags(backup,OpenFlags::SQLITE_OPEN_READ_ONLY)?; let mut target=Connection::open_with_flags(shadow,OpenFlags::SQLITE_OPEN_READ_WRITE)?; let copy=rusqlite::backup::Backup::new(&saved,&mut target)?;if copy.step(-1)?!=rusqlite::backup::StepResult::Done{return Err("shadow copy incomplete".into());}}
    let shadow_path=shadow.to_str().ok_or("shadow path encoding")?;
    let (store,_,_,_,device,inode)=SqliteStore::open_guarded(shadow_path,4,&id)?;
    if source_version == 17 {store.upgrade_verified_v17_to_v18(shadow_path,device,inode,&id)?;} drop(store);
    if document_structure {
        let (prepared,_,_,_,device,inode) = SqliteStore::open_guarded(shadow_path,4,&id)?;
        prepared.prepare_document_structure_on_shadow(shadow_path,device,inode,&id)?;
        drop(prepared);
    }
    if vector_profiles {
        let mut target=Connection::open_with_flags(shadow,OpenFlags::SQLITE_OPEN_READ_WRITE|OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
        target.execute_batch("PRAGMA foreign_keys=ON")?;
        let tx=target.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        tx.execute_batch(include_str!("../../memery-core/src/store/schema-v18-vector-profiles.sql"))?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.vector_profiles.v1','1')",[])?;
        tx.commit()?;
    }
    if document_vector_intent {
        let mut target=Connection::open_with_flags(shadow,OpenFlags::SQLITE_OPEN_READ_WRITE|OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
        target.execute_batch("PRAGMA foreign_keys=ON")?;
        let tx=target.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        tx.execute_batch(include_str!("../../memery-core/src/store/schema-v18-document-vector-build.sql"))?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.document_vector_build_intent.v1','1')",[])?;
        tx.commit()?;
    }
    if s08_v2_skill_empty {
        let mut target=Connection::open_with_flags(shadow,OpenFlags::SQLITE_OPEN_READ_WRITE|OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
        target.execute_batch("PRAGMA foreign_keys=ON")?;
        let tx=target.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-s08-model-v2.sql"))?;
        tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-s05-skill-draft.sql"))?;
        tx.commit()?;
    }
    if request_purge_empty {
        let mut target=Connection::open_with_flags(shadow,OpenFlags::SQLITE_OPEN_READ_WRITE|OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
        target.execute_batch("PRAGMA foreign_keys=ON")?;
        let tx=target.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        tx.execute_batch(include_str!("../../memery-core/src/store/schema-v17-request-purge-terminals.sql"))?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.native_request.purge_terminals.v1','1')",[])?;
        let count:i64=tx.query_row("SELECT count(*) FROM native_request_purge_terminal",[],|r|r.get(0))?;
        if count!=0{return Err("request terminal profile is not empty".into());} tx.commit()?;
    }
    if skill_purge {
        let mut target=Connection::open_with_flags(shadow,OpenFlags::SQLITE_OPEN_READ_WRITE|OpenFlags::SQLITE_OPEN_NO_MUTEX)?;target.execute_batch("PRAGMA foreign_keys=ON")?;
        let tx=target.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;tx.execute_batch(include_str!("../../memery-core/src/store/schema-v18-skill-purge.sql"))?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.s05.skill_purge.v1','1')",[])?;tx.commit()?;
    }
    if quota_purge {
        let mut target=Connection::open_with_flags(shadow,OpenFlags::SQLITE_OPEN_READ_WRITE|OpenFlags::SQLITE_OPEN_NO_MUTEX)?;target.execute_batch("PRAGMA foreign_keys=ON")?;
        let tx=target.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;tx.execute_batch(include_str!("../../memery-core/src/store/schema-v18-s05-quota-purge.sql"))?;
        tx.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.s05.quota_purge.v1','1')",[])?;
        let mut stmt=tx.prepare("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type,name")?;
        let mut rows=stmt.query([])?;let mut hash=Sha256::new();
        while let Some(row)=rows.next()? {for i in 0..4 {let part:String=row.get(i)?;hash.update((part.len() as u64).to_be_bytes());hash.update(part.as_bytes());}}
        drop(rows);drop(stmt);let schema=hash.finalize().iter().map(|v|format!("{v:02x}")).collect::<String>();
        let dbhex=id.as_bytes().iter().map(|v|format!("{v:02x}")).collect::<String>();let cutoff=chrono::Utc::now().timestamp_millis();
        if cutoff<=0 {return Err("quota cutoff clock unavailable".into());}
        tx.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.s05.quota_cutoff.v1',?1)",[format!("v1|{dbhex}|{cutoff}|{schema}")])?;tx.commit()?;
    }
    if document_vector_batch {
        let mut target=Connection::open_with_flags(shadow,OpenFlags::SQLITE_OPEN_READ_WRITE|OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
        target.execute_batch("PRAGMA foreign_keys=ON")?;
        let tx=target.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        // The raw stopped-copy connection does not install runtime writer UDFs.
        // Insert the marker before creating triggers, in this same transaction.
        tx.execute("INSERT INTO fm_meta(key,value) VALUES('openclank.document_vector_batch.v2','1')",[])?;
        tx.execute_batch(include_str!("../../memery-core/src/store/schema-v18-document-vector-epoch-v2.sql"))?;
        tx.execute_batch(include_str!("../../memery-core/src/store/schema-v18-document-vector-batch-v2.sql"))?;
        tx.commit()?;
    }
    let (verified,_,_,_,_,_)=SqliteStore::open_guarded(shadow.to_str().ok_or("shadow path encoding")?,4,&id)?;drop(verified);
    let target=Connection::open_with_flags(shadow,OpenFlags::SQLITE_OPEN_READ_ONLY)?; health(&target)?;
    let mut markers=Vec::new();if s08_v2_skill_empty {markers.push("openclank.s08.model.v2".to_string());markers.push("openclank.s05.skill_draft.v1".to_string());} if request_purge_empty {markers.push("openclank.native_request.purge_terminals.v1".to_string());} if vector_profiles {markers.push("openclank.vector_profiles.v1".to_string());} if document_vector_intent {markers.push("openclank.document_vector_build_intent.v1".to_string());} if skill_purge {markers.push("openclank.s05.skill_purge.v1".to_string());} if document_vector_batch {markers.push("openclank.document_vector_batch.v2".to_string());} if quota_purge {markers.push("openclank.s05.quota_purge.v1".to_string());markers.push("openclank.s05.quota_cutoff.v1".to_string());}
    if document_structure { markers.push("openclank.document_structure.v1".to_string()); }
    let additions=if markers.is_empty(){None}else{Some(BTreeMap::from([("fm_meta".to_string(),markers)]))};
    if target.query_row("PRAGMA user_version",[],|r|r.get::<_,i64>(0))?!=18 || inventory(&target,Some(&columns),additions.as_ref())?.1!=rows{return Err("shadow profile or rows changed".into());} drop(target);
    let source_final=fs::symlink_metadata(source)?;let backup_final=fs::symlink_metadata(backup)?;let shadow_final=fs::symlink_metadata(shadow)?;
    if !source_final.file_type().is_file() || source.canonicalize()?!=source || source_final.len()!=source_before.len() || source_final.modified()?!=source_before.modified()? || file_sha256(source)?!=source_hash || original.query_row("PRAGMA data_version",[],|r|r.get::<_,i64>(0))?!=data_version || source_wal_sha256(source)?!=source_wal || !backup_final.file_type().is_file() || file_sha256(backup)?!=backup_hash || !shadow_final.file_type().is_file(){return Err("source or backup changed during preparation".into());}
    #[cfg(target_os="linux")] if (source_final.dev(),source_final.ino())!=(source_before.dev(),source_before.ino()) || (backup_final.dev(),backup_final.ino())!=backup_identity || (shadow_final.dev(),shadow_final.ino())!=shadow_identity{return Err("copy identity changed".into());}
    drop(original); writeln!(journal,"status=shadow_verified\nshadow_schema_version=18\nsource_replaced=false")?;journal.sync_all()?;
    println!("prepared v18 shadow={} backup={} database_id={} source_replaced=false; activation not performed",shadow.display(),backup.display(),id);Ok(())
}
