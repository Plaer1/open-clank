#[cfg(unix)]
#[tokio::test]
#[ignore = "requires an explicitly supplied disposable old-format fixture"]
async fn pending_old_batch_refuses_after_resumable_pages() {
 use openclank_history::{migration_copy::{prepare,Inputs},migration_catalog::{start,advance}};
 use std::{fs,path::PathBuf};
 let old=PathBuf::from(std::env::var_os("OPENCLANK_HISTORY_OLD_FIXTURE").expect("absolute old fixture root required"));
    assert!(old.is_absolute());
    let old=old.canonicalize().unwrap();
    let t=tempfile::tempdir().unwrap();
    let p=t.path();
    fs::write(p.join("registry"),b"{}").unwrap();
    for d in ["receipts","shell","files"]{fs::create_dir(p.join(d)).unwrap();
        }
 let baseline=p.join("baseline");
    prepare(Inputs{catalog:old.join("catalog.redb"),lore:old.join("lore"),registry:p.join("registry"),receipts:p.join("receipts"),shell_journals:p.join("shell"),files_journals:p.join("files"),destination:baseline.clone()}).unwrap();
    let before=fs::read(baseline.join("catalog")).unwrap();
    let container=p.join("validation");
    start(&baseline,&container).unwrap();
    let mut pages=0;
    loop {match advance(&baseline,&container,1).await {Ok(progress)=>{assert_ne!(progress.state,"coverage_complete_no_readiness");
                pages+=1;
                assert!(pages<100);
                },Err(e)=>{assert!(e.to_string().contains("pending"),"{e}");
                break;
                }}}assert!(pages>1);
    assert_eq!(fs::read(baseline.join("catalog")).unwrap(),before);
    let cp:serde_json::Value=serde_json::from_slice(&fs::read(container.join("checkpoint.json")).unwrap()).unwrap();
    assert_eq!(cp["state"],"incomplete");
    assert!(!container.join("ready").exists());
}
