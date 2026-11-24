//! Unix source-only fixtures; no execution/qualification implied by authoring.
#![cfg(any(target_os="macos",target_os="linux"))]
use super::*;
use std::os::unix::{ffi::OsStrExt,fs::PermissionsExt};

// Separate opt-in scanner-only calibration. No authority/database/index job,
// source-body hashing, publication, or modification of the frozen corpus.
const POLICY_CORPUS:&str="/private/tmp/openclank-graph-qol-s06.xq1AvF/codebase";
const POLICY_MANIFEST:&str="cc4f7300ac1d899348cc5d45fa00738230272801524af618dad783e029476537";
fn frozen_policy_manifest(){
 assert_eq!(std::env::var("OPENCLANK_POLICY_CALIBRATION").ok().as_deref(),Some(POLICY_MANIFEST),"explicit root-coordinated corpus admission required");
 let manifest=Path::new(POLICY_CORPUS).parent().unwrap().join("source-manifest.json");
 let metadata=std::fs::symlink_metadata(&manifest).expect("frozen manifest metadata unavailable");
 assert!(metadata.is_file()&&!metadata.file_type().is_symlink()&&metadata.len()<=8*1024*1024,"invalid manifest envelope");
 // No SHA implementation/dependency is added to production. This read-only
 // metadata tool verifies the exact manifest, not the source bodies it lists.
 let output=std::process::Command::new("/usr/bin/shasum").args(["-a","256"]).arg(&manifest).output().expect("manifest digest tool unavailable");
 assert!(output.status.success(),"manifest digest command failed");
 assert!(output.stdout.starts_with(format!("{POLICY_MANIFEST} ").as_bytes()),"frozen manifest changed");
}
fn policy_calibration_scan(root:&Path,identity:&str,full:bool)->(SelectionPolicy,serde_json::Value){
 let start=std::time::Instant::now();
 let((policy,stats,files,coverage),metrics)=crate::code_metrics::capture_code_metrics(||{
  if full {let scan=scan_codebase_pinned(root,identity).unwrap_or_else(|_|panic!("full policy calibration scan failed"));(scan.policy,None,scan.files.len(),scan.coverage.len())}
  else {let(policy,stats)=scan_selection_policy_with_stats(root,identity).unwrap_or_else(|_|panic!("directory policy calibration scan failed"));(policy,Some(stats),0,0)}
 });
 let elapsed=start.elapsed().as_nanos();
 assert!(policy.inputs.windows(2).all(|p|p[0].0<p[1].0),"policy input ordering changed");
 let stats=stats.map(|s|serde_json::json!({"entries":s.entries,"known_non_directories_skipped":s.known_non_directories_skipped,"unknown_type_metadata":s.unknown_type_metadata,"directories":s.directories,"full_scanner_fallback":s.full_scanner_fallback}));
 let row=serde_json::json!({"method":if full{"full_inventory"}else{"policy_directory"},"elapsed_ns":elapsed,"policy_inputs":policy.inputs.len(),"selected_files":files,"coverage_entries":coverage,"discovery_stats":stats,"metrics":metrics});
 (policy,row)
}

#[test]
#[ignore="explicit frozen-corpus policy-only calibration; root serial measurement slot required"]
fn frozen_policy_discovery_calibration(){
 frozen_policy_manifest();let root=Path::new(POLICY_CORPUS);
 assert_eq!(root.canonicalize().expect("frozen corpus unavailable"),root,"unexpected corpus alias");
 let identity=directory_identity(root).expect("frozen corpus root invalid");
 // Excluded warmup is still checked, never a measurement sample. Its policy
 // remains an equality oracle for every subsequent fresh scan.
 let(oracle,_)=policy_calibration_scan(root,&identity,true);let(warm,_)=policy_calibration_scan(root,&identity,false);
 assert!(oracle.inputs==warm.inputs&&oracle.digest()==warm.digest(),"warmup policy mismatch");
 for iteration in 0..5 {
  let order=if iteration%2==0{[true,false]}else{[false,true]};
  let mut rows=Vec::new();
  for full in order {
   let(policy,row)=policy_calibration_scan(root,&identity,full);
   assert!(policy.inputs==oracle.inputs&&policy.digest()==oracle.digest(),"measured policy mismatch");rows.push(row);
  }
  assert_eq!(directory_identity(root).expect("frozen root unavailable"),identity,"frozen root changed");
  println!("S01_POLICY_DISCOVERY {}",serde_json::json!({"schema":1,"iteration":iteration,"source_manifest_sha256":POLICY_MANIFEST,"warmup_excluded":true,"exact_inputs_and_digest_equal":true,"samples":rows}));
 }
 frozen_policy_manifest();
}

struct Fixture {base:PathBuf,root:PathBuf}
impl Fixture {
 fn new()->Self{let base=std::env::temp_dir().join(format!("fm-policy-discovery-{}",uuid::Uuid::new_v4()));std::fs::create_dir(&base).unwrap();let base=base.canonicalize().unwrap();let root=base.join("source");std::fs::create_dir(&root).unwrap();Self{base,root}}
 fn write(&self,path:&str,bytes:impl AsRef<[u8]>){let path=self.root.join(path);std::fs::create_dir_all(path.parent().unwrap()).unwrap();std::fs::write(path,bytes).unwrap();}
 fn policy(&self)->(SelectionPolicy,PolicyDiscoveryStats){scan_selection_policy_with_stats(&self.root,&directory_identity(&self.root).unwrap()).unwrap()}
 fn parity(&self)->SelectionPolicy{
  let identity=directory_identity(&self.root).unwrap();let old=scan_codebase_pinned(&self.root,&identity).unwrap().policy;let(new,stats)=scan_selection_policy_with_stats(&self.root,&identity).unwrap();
  assert_eq!(new.inputs,old.inputs);assert_eq!(new.digest(),old.digest());assert!(!stats.full_scanner_fallback);
  let(unknown,unknown_stats)=scan_selection_policy_unix(&self.root,&identity,true).unwrap();assert_eq!(unknown.inputs,old.inputs);assert_eq!(unknown.digest(),old.digest());assert!(unknown_stats.unknown_type_metadata>0||unknown_stats.entries==0);new
 }
 fn error_parity(&self){let identity=directory_identity(&self.root).unwrap();let old=scan_codebase_pinned(&self.root,&identity).err().expect("full scanner must reject fixture");let new=scan_selection_policy_pinned(&self.root,&identity).err().expect("policy scanner must reject fixture");assert_eq!(old.split(':').next(),new.split(':').next());}
}
impl Drop for Fixture{fn drop(&mut self){POLICY_DISCOVERY_TEST_HOOK.with(|s|s.borrow_mut().take());assert!(self.base.file_name().unwrap().to_str().unwrap().starts_with("fm-policy-discovery-"));let _=std::fs::remove_dir_all(&self.base);}}

#[test]
fn policy_directory_discovery_exact_inputs_match_full_scanner_and_unknown_fallback(){
 let f=Fixture::new();
 f.write(".gitignore","ignored/\n!node_modules/\n!.hidden/\n!.ssh/\n*.tmp\n");
 f.write("src/.gitignore","sub/\n!keep/\n");f.write("src/main.py","def public(): pass\n");f.write("src/keep/.gitignore","*.secret\n");f.write("src/keep/file.txt","asset");
 f.write("src/sub/.gitignore",[0xff]);f.write("ignored/.gitignore",[0xff]);f.write(".ssh/.gitignore",[0xff]);f.write(".clanker/.gitignore",[0xff]);
 f.write("node_modules/.gitignore","generated/\n");f.write(".hidden/visible.py","def visible(): pass\n");
 let policy=f.parity();let paths:Vec<_>=policy.inputs.iter().map(|(p,_)|p.to_string_lossy().into_owned()).collect();
 assert!(paths.contains(&"src/keep".into()));assert!(paths.contains(&"node_modules".into()));assert!(paths.contains(&".hidden".into()));assert!(!paths.iter().any(|p|p.starts_with(".ssh")||p.starts_with(".clanker")||p=="ignored"||p=="src/sub"));
}

#[test]
fn known_non_directory_route_has_no_metadata_and_actual_discovery_reads_only_controls(){
 let f=Fixture::new();f.write(".gitignore","# one control\n");for n in 0..256{f.write(&format!("ordinary-{n}.py"),"source body must not be read");}
 f.write("private.txt","unreadable body");std::fs::set_permissions(f.root.join("private.txt"),std::fs::Permissions::from_mode(0o000)).unwrap();
 let pin=open_pinned_path(&f.root,&f.root,&directory_identity(&f.root).unwrap()).unwrap();let mut stats=PolicyDiscoveryStats::default();
 // A nonexistent name would fail fstatat: success proves the known-type path
 // does not fetch metadata, rather than merely yielding an equal policy.
 assert!(!policy_entry_is_directory(&pin,std::ffi::OsStr::new("absent"),libc::DT_REG,&mut stats).unwrap());assert_eq!(stats.known_non_directories_skipped,1);assert_eq!(stats.unknown_type_metadata,0);
 assert!(policy_entry_is_directory(&pin,std::ffi::OsStr::new("absent"),libc::DT_UNKNOWN,&mut stats).is_err());assert_eq!(stats.unknown_type_metadata,1);
 let((policy,actual),metrics)=crate::code_metrics::capture_code_metrics(||f.policy());
 assert_eq!(policy.inputs.len(),1);assert_eq!(actual.entries,3*258);assert_eq!(actual.directories,1);
 assert_eq!(actual.known_non_directories_skipped+actual.unknown_type_metadata,actual.entries);
 let reads=&metrics.stages[crate::code_metrics::Stage::Read as usize].counters;assert_eq!(reads.bytes,2*"# one control\n".len() as u64);assert_eq!(reads.items,2);
 f.parity();
}

#[test]
fn fresh_policy_discovers_new_dirs_file_to_dir_same_stat_edits_rename_and_absence(){
 let f=Fixture::new();f.write(".gitignore","# a\n");f.write("item","ordinary file");let first=f.parity();
 std::fs::remove_file(f.root.join("item")).unwrap();f.write("item/nested/file.py","pass\n");let directory=f.parity();assert_ne!(directory.digest(),first.digest());assert!(directory.inputs.iter().any(|(p,_)|p==Path::new("item/nested")));
 f.write("item/nested/.gitignore","# a\n");let added=f.parity();assert_ne!(added.digest(),directory.digest());
 let path=f.root.join("item/nested/.gitignore");let meta=std::fs::metadata(&path).unwrap();f.write("item/nested/.gitignore","# b\n");std::fs::File::open(&path).unwrap().set_times(std::fs::FileTimes::new().set_modified(meta.modified().unwrap())).unwrap();
 let edited=f.parity();assert_ne!(edited.digest(),added.digest());assert!(added.validate(&f.root,&directory_identity(&f.root).unwrap()).is_err());
 std::fs::rename(f.root.join("item"),f.root.join("renamed")).unwrap();let renamed=f.parity();assert_ne!(renamed.digest(),edited.digest());
 std::fs::remove_file(f.root.join("renamed/nested/.gitignore")).unwrap();let removed=f.parity();assert_ne!(removed.digest(),renamed.digest());assert!(removed.inputs.iter().any(|(p,h)|p==Path::new("renamed/nested")&&h.is_none()));
}

#[test]
fn policy_symlinks_special_and_non_utf8_names_never_add_unadmitted_inputs(){
 let f=Fixture::new();f.write("safe/file.py","pass\n");let outside=f.base.join("outside");std::fs::create_dir(&outside).unwrap();std::fs::write(outside.join(".gitignore"),[0xff]).unwrap();
 std::os::unix::fs::symlink(&outside,f.root.join("link")).unwrap();
 let invalid=std::ffi::OsStr::from_bytes(b"invalid-\xff");assert!(invalid.to_str().is_none());
 #[cfg(target_os="macos")] assert_eq!(std::fs::create_dir(f.root.join(invalid)).unwrap_err().raw_os_error(),Some(libc::EILSEQ),"macOS rejects this name before traversal");
 #[cfg(not(target_os="macos"))] {std::fs::create_dir(f.root.join(invalid)).unwrap();std::fs::write(f.root.join(invalid).join(".gitignore"),[0xff]).unwrap();}
 let pipe=std::ffi::CString::new(f.root.join("pipe").as_os_str().as_bytes()).unwrap();assert_eq!(unsafe{libc::mkfifo(pipe.as_ptr(),0o600)},0);
 let policy=f.parity();assert_eq!(policy.inputs.len(),2);assert!(!policy.inputs.iter().any(|(p,_)|p==Path::new("link")));
 std::os::unix::fs::symlink(&outside,f.base.join("symlink-root")).unwrap();assert!(directory_identity(&f.base.join("symlink-root")).is_err());
 std::os::unix::fs::symlink(outside.join(".gitignore"),f.root.join(".gitignore")).unwrap();f.error_parity();
}

#[test]
fn policy_error_and_control_budgets_remain_identical(){
 // Gitignore deliberately permits unclosed character classes as literals.
 let literal=Fixture::new();literal.write(".gitignore","[abc\n");literal.parity();
 for bytes in [vec![0xff],b"[z-a]\n".to_vec(),vec![b'#';MAX_IGNORE_FILE_BYTES+1],vec![b'\n';MAX_IGNORE_LINES+1]] {
  let f=Fixture::new();f.write(".gitignore",bytes);f.error_parity();
 }
 let f=Fixture::new();for n in 0..17{f.write(&format!("dir{n:02}/.gitignore"),format!("#{}","x".repeat(63_000)));}f.error_parity();
}

#[test]
fn policy_directory_budget_counts_only_eligible_retained_names(){
 let f=Fixture::new();f.write("one/file.txt","asset");f.write("two/file.txt","asset");
 let pin=open_pinned_path(&f.root,&f.root,&directory_identity(&f.root).unwrap()).unwrap();let mut stats=PolicyDiscoveryStats::default();
 // Exercise the same collector's remaining-capacity gate without a costly
 // 100,000-directory fixture. Excluded dirs do not consume its slots.
 assert_eq!(MAX_POLICY_DIRECTORIES,100_000);
 assert!(policy_directory_entries(&pin,&mut stats,false,1,|_|true).unwrap_err().starts_with("ignore_policy_budget_exceeded"));
 let(only_one,_)=policy_directory_entries(&pin,&mut stats,false,1,|n|n=="one").unwrap();assert_eq!(only_one,vec![std::ffi::OsString::from("one")]);
}

#[test]
fn directory_entry_probe_is_order_independent_and_detects_type_rename_additions(){
 let mut one=PolicyDirectoryEntries::default();one.add(b"a").unwrap();one.add(b"b").unwrap();
 let mut reversed=PolicyDirectoryEntries::default();reversed.add(b"b").unwrap();reversed.add(b"a").unwrap();assert_eq!(one,reversed);
 let mut renamed=PolicyDirectoryEntries::default();renamed.add(b"a").unwrap();renamed.add(b"renamed").unwrap();assert_ne!(one,renamed);
 let mut added=PolicyDirectoryEntries::default();added.add(b"a").unwrap();assert_ne!(one,added);
 let f=Fixture::new();f.write("name","file");let pin=open_pinned_path(&f.root,&f.root,&directory_identity(&f.root).unwrap()).unwrap();
 let(_,before)=policy_directory_entries(&pin,&mut PolicyDiscoveryStats::default(),false,0,|_|false).unwrap();
 std::fs::remove_file(f.root.join("name")).unwrap();std::fs::create_dir(f.root.join("name")).unwrap();
 let(_,after)=policy_directory_entries(&pin,&mut PolicyDiscoveryStats::default(),false,0,|_|false).unwrap();assert_ne!(before,after,"type change detected independently of timestamp comparison");
}

#[test]
fn policy_unreadable_directory_fails_closed_without_bypassing_private_rules(){
 assert_ne!(unsafe{libc::geteuid()},0,"permission-denial fixture requires non-root execution");
 let f=Fixture::new();f.write("blocked/.gitignore","# hidden\n");let path=f.root.join("blocked");std::fs::set_permissions(&path,std::fs::Permissions::from_mode(0o000)).unwrap();
 let identity=directory_identity(&f.root).unwrap();let old=scan_codebase_pinned(&f.root,&identity).is_err();let new=scan_selection_policy_pinned(&f.root,&identity).is_err();std::fs::set_permissions(&path,std::fs::Permissions::from_mode(0o700)).unwrap();assert!(old&&new);
}

#[test]
fn policy_type_discovery_root_and_control_races_fail_closed(){
 for mode in ["new_directory","file_to_directory","rename","root","control","symlink"] {
  let f=Fixture::new();f.write(".gitignore","# a\n");f.write("child/.gitignore","# safe\n");f.write("ordinary","body");
  let root=f.root.clone();let base=f.base.clone();let identity=directory_identity(&f.root).unwrap();
  POLICY_DISCOVERY_TEST_HOOK.with(|slot|*slot.borrow_mut()=Some(Box::new(move||match mode {
   "new_directory"=>{std::fs::create_dir(root.join("late")).unwrap();std::fs::write(root.join("late/.gitignore"),"*.py\n").unwrap();},
   "file_to_directory"=>{std::fs::remove_file(root.join("ordinary")).unwrap();std::fs::create_dir(root.join("ordinary")).unwrap();},
   "rename"=>std::fs::rename(root.join("child"),root.join("renamed")).unwrap(),
   "root"=>{std::fs::rename(&root,base.join("old-root")).unwrap();std::fs::create_dir(&root).unwrap();},
   "control"=>{let path=root.join(".gitignore");let m=std::fs::metadata(&path).unwrap();std::fs::write(&path,"# b\n").unwrap();std::fs::File::open(&path).unwrap().set_times(std::fs::FileTimes::new().set_modified(m.modified().unwrap())).unwrap();},
   "symlink"=>{std::fs::rename(root.join("child"),root.join("old-child")).unwrap();let outside=base.join("outside");std::fs::create_dir(&outside).unwrap();std::fs::write(outside.join(".gitignore"),[0xff]).unwrap();std::os::unix::fs::symlink(outside,root.join("child")).unwrap();},_=>unreachable!(),
  })));
  assert!(scan_selection_policy_pinned(&f.root,&identity).is_err(),"{mode}");
 }
}
