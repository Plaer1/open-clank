#[cfg(unix)]
#[tokio::test]
#[ignore = "requires an explicitly supplied disposable old-format fixture"]
async fn old_candidate_exact_receipts_and_corruption() {
    use openclank_history::{catalog::ResourceKey,migration_copy::{Inputs,prepare,verify_candidate_before}};
    use std::{fs,path::PathBuf};
    let old=PathBuf::from(std::env::var_os("OPENCLANK_HISTORY_OLD_FIXTURE").expect("set OPENCLANK_HISTORY_OLD_FIXTURE to an absolute disposable fixture root"));
    assert!(old.is_absolute());
    let old=old.canonicalize().unwrap();
    let temp=tempfile::tempdir().unwrap();
    let root=temp.path();
    fs::write(root.join("registry"),b"{}").unwrap();
    for d in ["receipts","shell","files"]{fs::create_dir(root.join(d)).unwrap();
        }
    let cases=[("small",Some("7f9907c250056c2d7fa7aed445377eb2d3f06d111e5e4ef52f37dab32fd87d30")),("large",Some("e2306c6367dab8e0f580a56d0023a55239547d84a3863de55d9c6d32d68ab7ca")),("empty",Some("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")),("absent",None)];
    for (id,digest) in cases {let target=root.join(format!("candidate-{id}"));
        prepare(Inputs{catalog:old.join("catalog.redb"),lore:old.join("lore"),registry:root.join("registry"),receipts:root.join("receipts"),shell_journals:root.join("shell"),files_journals:root.join("files"),destination:target.clone()}).unwrap();
        let key=ResourceKey{account_id:"fixture-account".into(),workspace_id:"fixture-workspace".into(),provider:"files".into(),resource_id:id.into()};
        let got=verify_candidate_before(&target,&key,"fixture-batch",digest).await.unwrap();
        assert_eq!(got,match id{"empty"=>"empty","absent"=>"tombstone",_=>digest.unwrap()});
        }
    let target=root.join("corrupt");
    prepare(Inputs{catalog:old.join("catalog.redb"),lore:old.join("lore"),registry:root.join("registry"),receipts:root.join("receipts"),shell_journals:root.join("shell"),files_journals:root.join("files"),destination:target.clone()}).unwrap();
    fs::remove_file(target.join("lore/immutable/index/74/pack/1")).unwrap();
    let key=ResourceKey{account_id:"fixture-account".into(),workspace_id:"fixture-workspace".into(),provider:"files".into(),resource_id:"small".into()};
    assert!(verify_candidate_before(&target,&key,"fixture-batch",cases[0].1).await.is_err());
}
