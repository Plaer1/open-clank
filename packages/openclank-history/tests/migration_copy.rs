#[cfg(unix)]
#[test]
fn copy_is_incomplete_preserves_source_and_refuses_pending() {
    use openclank_history::migration_copy::{Inputs,prepare};
    use std::fs;
    let t=tempfile::tempdir().unwrap();
    let p=t.path();
    let lore=p.join("lore");
    fs::create_dir_all(lore.join("immutable")).unwrap();
    fs::write(lore.join("immutable/lock"),b"").unwrap();
    fs::write(lore.join("immutable/data"),b"old bytes").unwrap();
    fs::write(lore.join("registry.json"),b"{}").unwrap();
    fs::create_dir(lore.join("receipts")).unwrap();
    fs::write(p.join("catalog"),b"catalog").unwrap();
    fs::create_dir(p.join("shell")).unwrap();
    fs::create_dir(p.join("files")).unwrap();
    let args=Inputs{catalog:p.join("catalog"),lore:lore.clone(),registry:lore.join("registry.json"),receipts:lore.join("receipts"),shell_journals:p.join("shell"),files_journals:p.join("files"),destination:p.join("candidate")};
    prepare(args.clone()).unwrap();
    let manifest:serde_json::Value=serde_json::from_slice(&fs::read(p.join("candidate/manifest.json")).unwrap()).unwrap();
    assert_eq!(manifest["state"],"incomplete");
    assert_eq!(manifest["components"]["registry"],"lore/registry.json");
    assert_eq!(fs::read(p.join("candidate/lore/immutable/data")).unwrap(),b"old bytes");
    assert_eq!(fs::read(lore.join("immutable/data")).unwrap(),b"old bytes");
    assert!(!p.join("candidate/lore/.openclank-history-format").exists());
    fs::write(p.join("files/pending"),b"unknown").unwrap();
    let mut refused=args;
    refused.destination=p.join("refused");
    assert!(prepare(refused).is_err());
    assert!(!p.join("refused").exists());
}

#[cfg(unix)]
#[test]
fn excessive_depth_is_refused_before_copy() {
    use openclank_history::migration_copy::{Inputs,prepare};
    use std::fs;
    let t=tempfile::tempdir().unwrap();
    let p=t.path();
    let lore=p.join("lore");
    fs::create_dir_all(lore.join("immutable")).unwrap();
    fs::write(lore.join("immutable/lock"),b"").unwrap();
    fs::write(lore.join("registry.json"),b"{}").unwrap();
    fs::create_dir(lore.join("receipts")).unwrap();
    fs::write(p.join("catalog"),b"catalog").unwrap();
    fs::create_dir(p.join("shell")).unwrap();
    fs::create_dir(p.join("files")).unwrap();
    let mut deep=lore.join("immutable");
    for _ in 0..130 { deep.push("d");
        fs::create_dir(&deep).unwrap();
        }
    let args=Inputs{catalog:p.join("catalog"),lore:lore.clone(),registry:lore.join("registry.json"),receipts:lore.join("receipts"),shell_journals:p.join("shell"),files_journals:p.join("files"),destination:p.join("candidate")};
    assert!(prepare(args).unwrap_err().to_string().contains("depth"));
    assert!(!p.join("candidate").exists());
}
