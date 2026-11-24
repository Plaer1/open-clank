mod legacy;
mod guarded;
mod account_chat_manifest;
mod ontology;
#[cfg(test)]
use rusqlite::Connection;
use std::env;
#[cfg(test)]
use std::path::Path;
pub fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.first().map(String::as_str)==Some("--prepare-guarded-v18-s08-v2-skill-empty") {
        if args.len()!=5 || args[4]!="--writers-stopped" {return Err("usage: --prepare-guarded-v18-s08-v2-skill-empty SOURCE BACKUP SHADOW --writers-stopped".into());}
        return guarded::prepare_v18(&args[1],&args[2],&args[3],false,false,false,false,false,false,true,false);
    }
    if args.first().map(String::as_str)==Some("--prepare-guarded-v18-request-purge-empty") {
        if args.len()!=5 || args[4]!="--writers-stopped" {return Err("usage: --prepare-guarded-v18-request-purge-empty SOURCE BACKUP SHADOW --writers-stopped".into());}
        return guarded::prepare_v18(&args[1],&args[2],&args[3],false,false,false,false,false,true,false,false);
    }
    if args.first().map(String::as_str)==Some("--prepare-guarded-v18-s05-quota") {
        if args.len()!=5 || args[4]!="--writers-stopped" {return Err("usage: --prepare-guarded-v18-s05-quota SOURCE BACKUP SHADOW --writers-stopped".into());}
        return guarded::prepare_v18(&args[1],&args[2],&args[3],false,false,false,false,true,false,false,false);
    }
    if args.first().map(String::as_str)==Some("--prepare-guarded-v18-document-batch") {
        if args.len()!=5 || args[4]!="--writers-stopped" {return Err("usage: --prepare-guarded-v18-document-batch SOURCE BACKUP SHADOW --writers-stopped".into());}
        return guarded::prepare_v18(&args[1],&args[2],&args[3],false,false,false,true,false,false,false,false);
    }
    if args.first().map(String::as_str)==Some("--prepare-guarded-v18-skill-purge") {
        if args.len()!=5 || args[4]!="--writers-stopped" {return Err("usage: --prepare-guarded-v18-skill-purge SOURCE BACKUP SHADOW --writers-stopped".into());}
        return guarded::prepare_v18(&args[1],&args[2],&args[3],false,false,true,false,false,false,false,false);
    }
    if args.first().map(String::as_str)==Some("--prepare-guarded-v18") {
        if args.len()<5 || args.len()>8 || args[4]!="--writers-stopped" || args[5..].iter().any(|v|v!="--vector-profiles" && v!="--document-vector-build-intent" && v!="--document-structure")
            || ["--vector-profiles","--document-vector-build-intent","--document-structure"].iter().any(|name|args[5..].iter().filter(|v|v.as_str()==*name).count()>1) {return Err("usage: --prepare-guarded-v18 SOURCE BACKUP SHADOW --writers-stopped [--vector-profiles] [--document-vector-build-intent] [--document-structure]".into());}
        return guarded::prepare_v18(&args[1],&args[2],&args[3],args[5..].iter().any(|v|v=="--vector-profiles"),args[5..].iter().any(|v|v=="--document-vector-build-intent"),false,false,false,false,false, args[5..].iter().any(|v| v=="--document-structure"));
    }
    if args.first().map(String::as_str)==Some("--prepare-guarded-v17") {
        if args.len()<4 || args.len()>24 {return Err("usage: --prepare-guarded-v17 SOURCE BACKUP SHADOW --writers-stopped [--feedback] [--s08] [--s08-model] [--s08-model-v2] [--maintenance] [--policy-projection] [--queued-source] [--s09] [--domain-metadata] [--s05-account-v4] [--s02-policy] [--s02-policy-v2] [--knowledge-ref] [--assistant-interpretation] [--assistant-interpretation-pending] [--general-hex] [--s05-account-chat] [--document-fts] [--s05-skill-draft]".into());}
        let options=&args[4..];
        if options.iter().any(|v|v!="--writers-stopped" && v!="--feedback" && v!="--s08" && v!="--s08-model" && v!="--s08-model-v2" && v!="--maintenance" && v!="--policy-projection" && v!="--queued-source" && v!="--s09" && v!="--domain-metadata" && v!="--s05-account-v4" && v!="--s02-policy" && v!="--s02-policy-v2" && v!="--knowledge-ref" && v!="--assistant-interpretation" && v!="--assistant-interpretation-pending" && v!="--general-hex" && v!="--s05-account-chat" && v!="--document-fts" && v!="--s05-skill-draft") || ["--writers-stopped","--feedback","--s08","--s08-model","--maintenance","--policy-projection","--queued-source","--s09","--domain-metadata","--s05-account-v4","--s02-policy","--s02-policy-v2","--knowledge-ref","--assistant-interpretation","--assistant-interpretation-pending","--general-hex","--s05-account-chat","--document-fts","--s08-model-v2","--s05-skill-draft"].iter().any(|name|options.iter().filter(|v|v.as_str()==*name).count()>1) {return Err("unknown or duplicate guarded preparation option".into());}
        if !options.iter().any(|v|v=="--writers-stopped") {return Err("explicit stopped-writer acknowledgment required".into());}
        let document_fts=options.iter().any(|v|v=="--document-fts");
        let general_hex=options.iter().any(|v|v=="--general-hex");
        let feedback=options.iter().any(|v|v=="--feedback");let s08=options.iter().any(|v|v=="--s08");
        let maintenance=options.iter().any(|v|v=="--maintenance");
        if (s08 || maintenance) && !feedback {return Err("optional preparation requires feedback".into());}
        let policy=options.iter().any(|v|v=="--policy-projection");
        let queued=options.iter().any(|v|v=="--queued-source");
        let s09=options.iter().any(|v|v=="--s09");
        let domains=options.iter().any(|v|v=="--domain-metadata");
        if domains && !policy {return Err("domain metadata requires policy projection".into());}
        if s09 && !policy {return Err("question semantics requires policy projection".into());}
        if queued && !(policy && feedback) {return Err("queued source requires policy projection and feedback".into());}
        let s05_account_v4=options.iter().any(|v|v=="--s05-account-v4");
        let account_chat=options.iter().any(|v|v=="--s05-account-chat");
        if account_chat && !s05_account_v4 {return Err("account chat requires S05 account v4".into());}
        let pair=options.iter().any(|v|v=="--assistant-interpretation");
        if pair && !s05_account_v4 {return Err("assistant interpretation requires S05 account v4".into());}
        let pair_pending=options.iter().any(|v|v=="--assistant-interpretation-pending");
        if pair_pending && !pair {return Err("assistant interpretation pending requires assistant interpretation".into());}
        let s02v2=options.iter().any(|v|v=="--s02-policy-v2");
        let s02=options.iter().any(|v|v=="--s02-policy");
        if s02v2 && !s02 {return Err("S02 v2 requires S02 v1 base".into());}
        if s02 && !(policy && domains) {return Err("S02 policy requires policy projection and domain metadata".into());}
        let knowledge_ref=options.iter().any(|v|v=="--knowledge-ref");
        if knowledge_ref && !s02v2 {return Err("knowledge reference requires S02 v2".into());}
        let s08_model=options.iter().any(|v|v=="--s08-model");
        if s08_model && !(s08 && s02) {return Err("S08 model requires S08 and S02 policy".into());}
        let s08_model_v2=options.iter().any(|v|v=="--s08-model-v2");
        if s08_model_v2 && !s08_model {return Err("S08 model v2 requires the S08 model profile".into());}
        let skill_draft=options.iter().any(|v|v=="--s05-skill-draft");
        if skill_draft && !(s05_account_v4 && pair && s08_model_v2) {return Err("skill draft requires S05 account v4, pair and S08 model v2".into());}
        return guarded::prepare(&args[1],&args[2],&args[3],feedback,s08,maintenance,policy,queued,s09,domains,s05_account_v4,s02,s02v2,pair,pair_pending,s08_model,s08_model_v2,knowledge_ref,general_hex,account_chat,document_fts,skill_draft);
    }
    if args.len() == 2 && args[0] == "--export-s05-account-chat-manifest" {
        use std::io::Write;
        let manifest=account_chat_manifest::render()?;
        let mut output=std::fs::OpenOptions::new().write(true).create_new(true).open(&args[1])?;
        output.write_all(manifest.as_bytes())?;
        output.sync_all()?;
        return Ok(());
    }
    if args.len() == 2 && args[0] == "--export-current-schema" {
        use std::io::Write;
        let mut output=std::fs::OpenOptions::new().write(true).create_new(true).open(&args[1])?;
        output.write_all(include_str!("../../memery-core/src/store/current-schema.sql").as_bytes())?;
        output.sync_all()?;
        return Ok(());
    }
    Err("only explicit guarded copy preparation, schema export, or account-chat manifest export is supported".into())
}

#[cfg(test)]
mod s04_fixture_tests {
    use super::*;
    #[test]
    fn v15_upgrade_is_empty_rich_idempotent_and_transactional() {
        let conn=Connection::open_in_memory().unwrap();
        conn.execute_batch(include_str!("../../memery-core/src/store/current-schema.sql")).unwrap();
        conn.execute_batch("DROP TABLE code_resolutions;DROP TABLE code_file_evidence;PRAGMA user_version=15;").unwrap();
        conn.execute("INSERT INTO code_files(codebase,rel_path,blake3,mtime_ns,size,indexed_at) VALUES('unmapped-old','a.py','old-hash',1,9,'before')",[]).unwrap();
        legacy::upgrade(&conn).unwrap();legacy::upgrade(&conn).unwrap();
        assert_eq!(conn.query_row("PRAGMA user_version",[],|r|r.get::<_,i64>(0)).unwrap(),16);
        for table in ["code_file_evidence","code_resolutions"] {assert_eq!(conn.query_row(&format!("SELECT count(*) FROM {table}"),[],|r|r.get::<_,i64>(0)).unwrap(),0);}
        assert_eq!(conn.query_row("SELECT blake3 FROM code_files",[],|r|r.get::<_,String>(0)).unwrap(),"old-hash");
        conn.execute_batch("DROP TABLE code_resolutions;DROP TABLE code_file_evidence;CREATE TABLE code_file_evidence(injected INTEGER);PRAGMA user_version=15;").unwrap();
        assert!(legacy::upgrade(&conn).is_err());
        assert_eq!(conn.query_row("PRAGMA user_version",[],|r|r.get::<_,i64>(0)).unwrap(),15);
        assert_eq!(conn.query_row("SELECT count(*) FROM sqlite_schema WHERE name='code_resolutions'",[],|r|r.get::<_,i64>(0)).unwrap(),0);
    }
}

#[cfg(test)]
mod s03_fixture_tests {
    use super::*;
    fn v14_fixture(path:&Path){
        let conn=Connection::open(path).unwrap();conn.execute_batch(include_str!("../../memery-core/src/store/current-schema.sql")).unwrap();
        conn.execute_batch("DROP TABLE code_resolutions;DROP TABLE code_file_evidence;ALTER TABLE code_files DROP COLUMN config_fingerprint;ALTER TABLE code_files DROP COLUMN source_generation;ALTER TABLE code_index_runs DROP COLUMN heartbeat_at;ALTER TABLE code_index_runs DROP COLUMN completed_files;ALTER TABLE code_index_runs DROP COLUMN total_files;ALTER TABLE code_index_runs DROP COLUMN admission_epoch;ALTER TABLE code_index_runs DROP COLUMN config_fingerprint;DROP TABLE code_registrations;DROP TABLE code_scope_epochs;DROP TABLE code_legacy_mappings;PRAGMA user_version=14;").unwrap();
        for namespace in ["alice\u{1f}ws\u{1f}repo_git_old","ownerless-old"]{conn.execute("INSERT INTO code_files(codebase,rel_path,blake3,mtime_ns,size,symbol_count,indexed_at) VALUES(?1,'main.py','original-hash',1,9,1,'original-time')",[namespace]).unwrap();}
    }
    #[test]
    fn fixture_upgrade_idempotence_explicit_mapping_and_rollback(){
        let directory=env::temp_dir().join(format!("fm-s03-migration-{}",std::process::id()));std::fs::create_dir_all(&directory).unwrap();let path=directory.join("fixture.sqlite");v14_fixture(&path);
        assert!(memery_core::store::sqlite::SqliteStore::new(path.to_str().unwrap(),4).is_err());
        let conn=Connection::open(&path).unwrap();let backup=directory.join("preimage.sqlite");conn.execute("VACUUM INTO ?1",[backup.to_str().unwrap()]).unwrap();legacy::upgrade(&conn).unwrap();legacy::upgrade(&conn).unwrap();
        let store=memery_core::store::sqlite::SqliteStore::new(path.to_str().unwrap(),4).unwrap();drop(store);assert_eq!(conn.query_row("SELECT count(*) FROM code_files WHERE blake3='original-hash'",[],|r|r.get::<_,i64>(0)).unwrap(),2);
        assert_eq!(conn.query_row("SELECT status FROM code_legacy_mappings WHERE legacy_key='ownerless-old'",[],|r|r.get::<_,String>(0)).unwrap(),"unavailable");
        conn.execute("INSERT INTO code_registrations(owner,workspace_id,registration_id,checkout_id,root_binding,owner_subject_id) VALUES('alice','ws','registration-fixture','checkout-fixture','private-binding','subject-a')",[]).unwrap();
        legacy::map_legacy(&conn,"alice","ws","repo_git_old","checkout-fixture").unwrap();legacy::map_legacy(&conn,"alice","ws","repo_git_old","checkout-fixture").unwrap();assert!(legacy::map_legacy(&conn,"bob","ws","repo_git_old","checkout-fixture").is_err());
        let changes=conn.total_changes();
        assert!(legacy::mapping_is_replay(&conn,"alice","ws","repo_git_old","checkout-fixture").unwrap());
        legacy::map_legacy(&conn,"alice","ws","repo_git_old","checkout-fixture").unwrap();
        assert_eq!(conn.total_changes(),changes,"exact replay must not write");
        conn.execute("INSERT INTO code_registrations(owner,workspace_id,registration_id,checkout_id,root_binding,owner_subject_id) VALUES('alice','ws','registration-other','checkout-other','binding-other','subject-a')",[]).unwrap();assert!(legacy::map_legacy(&conn,"alice","ws","repo_git_old","checkout-other").is_err());
        assert_eq!(conn.query_row("SELECT checkout_id FROM code_legacy_mappings WHERE owner='alice'",[],|r|r.get::<_,String>(0)).unwrap(),"checkout-fixture");assert_eq!(conn.query_row("SELECT count(*) FROM code_files WHERE codebase='alice'||char(31)||'ws'||char(31)||'repo_git_old'",[],|r|r.get::<_,i64>(0)).unwrap(),1);
        drop(conn);let restored=Connection::open(&backup).unwrap();assert_eq!(restored.query_row("PRAGMA user_version",[],|r|r.get::<_,i64>(0)).unwrap(),14);assert_eq!(restored.query_row("SELECT count(*) FROM code_files WHERE blake3='original-hash'",[],|r|r.get::<_,i64>(0)).unwrap(),2);drop(restored);
        let failed=directory.join("failed.sqlite");v14_fixture(&failed);let conn=Connection::open(&failed).unwrap();conn.execute("CREATE TABLE code_registrations(injected INTEGER)",[]).unwrap();assert!(legacy::upgrade(&conn).is_err());assert_eq!(conn.query_row("PRAGMA user_version",[],|r|r.get::<_,i64>(0)).unwrap(),14);assert_eq!(conn.query_row("SELECT count(*) FROM pragma_table_info('code_files') WHERE name='config_fingerprint'",[],|r|r.get::<_,i64>(0)).unwrap(),0);drop(conn);std::fs::remove_dir_all(directory).unwrap();
    }
}
