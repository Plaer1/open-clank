//! Offline inventory/copy only. This is not a converter or a quiescence broker.
#[cfg(unix)]
mod unix {
    use serde::{Deserialize, Serialize};
    use sha2::{Digest, Sha256};
    use std::{collections::{BTreeMap, BTreeSet}, fs::{self, File, OpenOptions}, io::{self, Read, Write}, os::{fd::AsRawFd, unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt}}, path::{Component, Path, PathBuf}};
    const MAX_FILES: usize = 100_000;
    const MAX_BYTES: u64 = 1 << 40;
    const MAX_DEPTH: usize = 128;
    #[derive(Clone, Debug)]
    pub struct Inputs {
        pub catalog: PathBuf,
        pub lore: PathBuf,
        pub registry: PathBuf,
        pub receipts: PathBuf,
        pub shell_journals: PathBuf,
        pub files_journals: PathBuf,
        pub destination: PathBuf,
    }
    #[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
    struct Entry {
        component: String,
        relative: String,
        kind: String,
        bytes: u64,
        sha256: Option<String>,
        device: u64,
        inode: u64,
        mtime: i64,
        mtime_ns: i64,
    }
    #[derive(Serialize, Deserialize)]
    struct Manifest {
        version: u8,
        state: String,
        assumption: String,
        components: BTreeMap<String,String>,
        entries: Vec<Entry>,
        #[serde(default)]
        absent: BTreeSet<String>,
    }
    fn fail(s: &str) -> io::Error { io::Error::other(s) }
    fn checked(path: &Path) -> io::Result<PathBuf> {
        if !path.is_absolute() { return Err(fail("all authority paths must be absolute"));
            }
        let mut cur=PathBuf::from("/");
        for c in path.components() { match c { Component::RootDir=>{}, Component::Normal(n)=> { cur.push(n);
                    if fs::symlink_metadata(&cur)?.file_type().is_symlink() { return Err(fail("symlink in authority path"));
                        } }, _=>return Err(fail("non-normal authority path")) } }
        cur.canonicalize()
    }
    fn is_absent(path: &Path) -> io::Result<bool> {
        match fs::symlink_metadata(path) {
            Ok(_) => Ok(false),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(true),
            Err(e) => Err(e),
        }
    }

    // A never-created optional authority has a name and a verified absent leaf.
    // Resolve existing ancestors without following symlinks, including on retries.
    fn authority_path(name: &str, path: &Path) -> io::Result<PathBuf> {
        if !matches!(name, "registry" | "receipts" | "shell_journals" | "files_journals") {
            return checked(path);
        }
        if !path.is_absolute() { return Err(fail("all authority paths must be absolute")); }
        let mut canonical = PathBuf::new();
        for part in path.components() {
            match part {
                Component::RootDir => canonical.push("/"),
                Component::Normal(v) => {
                    canonical.push(v);
                    match fs::symlink_metadata(&canonical) {
                        Ok(m) if m.file_type().is_symlink() => return Err(fail("symlink in authority path")),
                        Ok(_) => {},
                        Err(e) if e.kind() == io::ErrorKind::NotFound => {},
                        Err(e) => return Err(e),
                    }
                }
                _ => return Err(fail("non-normal authority path")),
            }
        }
        Ok(canonical)
    }

    fn absent_components(paths: &BTreeMap<String, PathBuf>) -> io::Result<BTreeSet<String>> {
        let mut absent = BTreeSet::new();
        for (name, path) in paths {
            if is_absent(path)? {
                if !matches!(name.as_str(), "registry" | "receipts" | "shell_journals" | "files_journals") {
                    return Err(fail("required authority is missing"));
                }
                absent.insert(name.clone());
            }
        }
        Ok(absent)
    }

    pub(crate) fn require_copy_space(parent: &Path, required: u64) -> io::Result<()> {
        use std::os::unix::ffi::OsStrExt;
        let path = std::ffi::CString::new(parent.as_os_str().as_bytes())?;
        let mut info = std::mem::MaybeUninit::<libc::statvfs>::uninit();
        if unsafe { libc::statvfs(path.as_ptr(), info.as_mut_ptr()) } != 0 {
            return Err(io::Error::last_os_error());
        }
        let info = unsafe { info.assume_init() };
        let available = u128::from(info.f_bavail) * u128::from(info.f_frsize);
        if available < u128::from(required) {
            return Err(fail("insufficient space for History copy"));
        }
        Ok(())
    }
    fn lock(path: &Path) -> io::Result<File> {
        let f=OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(path)?;
        if !f.metadata()?.is_file() { return Err(fail("lock is not a regular file"));
            }
        if unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX|libc::LOCK_NB) } != 0 { return Err(fail("source lock unavailable"));
            } Ok(f)
    }
    fn empty(path: &Path) -> io::Result<()> { if !path.is_dir() || fs::read_dir(path)?.next().is_some() { return Err(fail("producer journals or staging are not empty; pending state requires review"));
            } Ok(()) }
    pub(crate) fn digest(path: &Path, limit:u64, deadline:Option<std::time::Instant>) -> io::Result<(String,u64)> {
        let mut f=OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(path)?;
        let mut h=Sha256::new();
        let mut b=[0u8;65536];
        let mut size=0u64;
        loop { if deadline.is_some_and(|d|std::time::Instant::now()>=d){return Err(fail("verification deadline"));
                } let n=f.read(&mut b)?;
            if n==0 { break } size=size.checked_add(n as u64).ok_or_else(||fail("size overflow"))?;
            if size>limit { return Err(fail("byte budget exceeded"));
                } h.update(&b[..n]);
            } Ok((format!("{:x}",h.finalize()),size))
    }
    fn scan_one(root: &Path, name: &str, path: &Path, out: &mut Vec<Entry>, total: &mut u64, depth: usize, limit:u64, deadline:Option<std::time::Instant>) -> io::Result<()> {
        if deadline.is_some_and(|d|std::time::Instant::now()>=d){return Err(fail("verification deadline"));
            }
        if depth > MAX_DEPTH { return Err(fail("directory depth budget exceeded"));
            }
        if out.len()>=MAX_FILES { return Err(fail("entry budget exceeded"));
            }
        let meta=fs::symlink_metadata(path)?;
        let ft=meta.file_type();
        if ft.is_symlink() || (!ft.is_file() && !ft.is_dir()) { return Err(fail("unsupported source object"));
            }
        if ft.is_file() && meta.nlink()!=1 { return Err(fail("hard-linked source refused"));
            }
        let rel=path.strip_prefix(root).map_err(|_|fail("source escaped root"))?.to_str().ok_or_else(||fail("non-UTF-8 path"))?.to_owned();
        let basename=path.file_name().and_then(|x|x.to_str()).unwrap_or("").to_ascii_lowercase();
        if basename.contains("credential") || basename.contains("secret") || basename.contains("token") { return Err(fail("credential-like source file refused"));
            }
        let (hash,size)=if ft.is_file() { let (h,n)=digest(path,limit.saturating_sub(*total),deadline)?;
            if n!=meta.len() { return Err(fail("source changed during read"));
                } *total=total.checked_add(n).ok_or_else(||fail("size overflow"))?;
            if *total>limit{return Err(fail("byte budget exceeded"));
                } (Some(h),n) } else {(None,0)};
        out.push(Entry{component:name.into(),relative:rel,kind:if ft.is_dir(){"directory"}else{"file"}.into(),bytes:size,sha256:hash,device:meta.dev(),inode:meta.ino(),mtime:meta.mtime(),mtime_ns:meta.mtime_nsec()});
        if ft.is_dir() { let mut children=Vec::new();
            for child in fs::read_dir(path)? { if children.len() >= MAX_FILES.saturating_sub(out.len()) { return Err(fail("entry budget exceeded"));
                    } children.push(child?.path());
                } children.sort();
            for c in children { scan_one(root,name,&c,out,total,depth+1,limit,deadline)?;
                } } Ok(())
    }
    fn inventory(roots:&[(String,PathBuf)],limit:u64,deadline:Option<std::time::Instant>) -> io::Result<Vec<Entry>> { let mut out=Vec::new();
        let mut total=0;
        for (name,path) in roots { scan_one(path,name,path,&mut out,&mut total,0,limit,deadline)?;
            } Ok(out) }
    pub fn prepare(i: Inputs) -> io::Result<()> {
        let mut source=BTreeMap::new();
        for (n,p) in [("catalog",i.catalog),("lore",i.lore),("registry",i.registry),("receipts",i.receipts),("shell_journals",i.shell_journals),("files_journals",i.files_journals)] { source.insert(n.to_string(),authority_path(n,&p)?);
            }
        let absent = absent_components(&source)?;
        if source.values().collect::<BTreeSet<_>>().len() != source.len() { return Err(fail("duplicate authority paths")); }
        if !source["catalog"].is_file() || !source["lore"].is_dir() || (!absent.contains("registry") && !source["registry"].is_file()) { return Err(fail("invalid required component types"));
            }
        for name in ["shell_journals", "files_journals"] {
            if !absent.contains(name) { empty(&source[name])?; }
        }
        let stage=source["registry"].parent().ok_or_else(||fail("registry parent missing"))?.join(".openclank-history-staging");
        if stage.exists() { empty(&checked(&stage)?)?;
            }
        // Never sweep arbitrary settings or credentials from the store parent. Only
        // the Lore-owned layout and explicitly mapped nested components are admissible.
        for entry in fs::read_dir(&source["lore"])? {
            let entry=entry?;
            let child=entry.path();
            let name=entry.file_name();
            let allowed=name=="immutable" || name==".openclank-history-format" || child==stage ||
                source.values().any(|p|p.starts_with(&child));
            if !allowed { return Err(fail("unknown Lore-root component refused"));
                }
        }
        if !absent.contains("receipts") { for entry in fs::read_dir(&source["receipts"])? {
            let entry=entry?;
            let name=entry.file_name();
            let text=name.to_str().ok_or_else(||fail("invalid receipt name"))?;
            if !text.ends_with(".receipt") || !entry.file_type()?.is_file() { return Err(fail("unknown receipt-root component refused"));
                }
        }
        }
        let _catalog_lock=lock(&source["catalog"])?;
        let _lore_lock=lock(&source["lore"].join("immutable/lock"))?;
        let mut roots:Vec<(String,PathBuf)>=source.iter().map(|(n,p)|(n.clone(),p.clone())).collect();
        roots.sort_by_key(|(_,p)|p.components().count());
        let mut top:Vec<(String,PathBuf)>=Vec::new();
        let mut mapping=BTreeMap::new();
        for (n,p) in roots { if let Some((owner,base))=top.iter().find(|(_,base)|p.starts_with(base)) { let rel=p.strip_prefix(base).unwrap().to_str().ok_or_else(||fail("non-UTF-8 path"))?;
                mapping.insert(n,if rel.is_empty(){owner.clone()}else{format!("{owner}/{rel}")});
                } else { mapping.insert(n.clone(),n.clone());
                top.push((n,p));
                } }
        let destination=i.destination;
        let parent=checked(destination.parent().ok_or_else(||fail("destination parent missing"))?)?;
        let leaf=destination.file_name().ok_or_else(||fail("destination name missing"))?;
        let dest=parent.join(leaf);
        if fs::symlink_metadata(&dest).is_ok() || source.values().any(|p|dest.starts_with(p) || p.starts_with(&dest)) { return Err(fail("destination exists or overlaps source"));
            }
        top.retain(|(name,_)| !absent.contains(name));
        let before=inventory(&top,MAX_BYTES,None)?;
        let mut seen=BTreeSet::new();
        for e in &before { if e.kind=="file" && !seen.insert((e.device,e.inode)) {return Err(fail("aliased source files refused"));
                } }
        require_copy_space(&parent, before.iter().map(|entry| entry.bytes).sum())?;
        fs::create_dir(&dest)?;
        fs::set_permissions(&dest,fs::Permissions::from_mode(0o700))?;
        let mut manifest=File::create(dest.join("manifest.json"))?;
        let initial=Manifest{version:1,state:"incomplete".into(),assumption:"All History, Files and shell writers were stopped and drained by the operator; locks do not prove this.".into(),components:mapping.clone(),entries:vec![],absent:absent.clone()};
        serde_json::to_writer_pretty(&mut manifest,&initial)?;
        manifest.sync_all()?;
        for e in &before { let root=top.iter().find(|(n,_)|*n==e.component).unwrap();
            let src=if e.relative.is_empty(){root.1.clone()}else{root.1.join(&e.relative)};
            let target_root=dest.join(&e.component);
            let target=if e.relative.is_empty(){target_root}else{target_root.join(&e.relative)};
            if e.kind=="directory" { fs::create_dir(&target)?;
                fs::set_permissions(&target,fs::Permissions::from_mode(0o700))?;
                } else { let mut from=OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(&src)?;
                let m=from.metadata()?;
                if m.dev()!=e.device || m.ino()!=e.inode {return Err(fail("source identity changed"));
                    } let mut to=OpenOptions::new().write(true).create_new(true).mode(0o600).open(&target)?;
                let copied=io::copy(&mut (&mut from).take(e.bytes+1),&mut to)?;
                let after=from.metadata()?;
                if copied!=e.bytes || after.dev()!=e.device || after.ino()!=e.inode || after.len()!=e.bytes || after.mtime()!=e.mtime || after.mtime_nsec()!=e.mtime_ns {return Err(fail("source changed while copying"));
                    } to.sync_all()?;
                let (h,n)=digest(&target,MAX_BYTES,None)?;
                if Some(h)!=e.sha256 || n!=e.bytes { return Err(fail("copy digest mismatch"));
                    } } }
        if absent_components(&source)? != absent || inventory(&top,MAX_BYTES,None)?!=before { return Err(fail("source changed during snapshot"));
            }
        let final_manifest=Manifest{version:1,state:"incomplete".into(),assumption:initial.assumption,components:mapping,entries:before,absent};
        let mut f=OpenOptions::new().write(true).create_new(true).mode(0o600).open(dest.join("manifest.complete.tmp"))?;
        serde_json::to_writer_pretty(&mut f,&final_manifest)?;
        f.write_all(b"\n")?;
        f.sync_all()?;
        fs::rename(dest.join("manifest.complete.tmp"),dest.join("manifest.json"))?;
        File::open(&dest)?.sync_all()?;
        Ok(())
    }

    /// The store is the only authority whose format changes. A single directory exchange
    /// switches it and its nested components. Peers outside it keep their inventoried
    /// identities; the catalog rows and all external authorities were compared in preparation.
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    pub async fn activate(baseline: &Path, container: &Path, inputs: Inputs) -> io::Result<PathBuf> {
        use std::{ffi::CString, os::unix::ffi::OsStrExt, time::{Duration, Instant}};
        use crate::migration_catalog::{prepare_final, hash_tree};
        #[derive(Serialize, Deserialize, PartialEq, Eq)]
        struct Exchange {
            authorities: BTreeMap<String, PathBuf>,
            previous: (u64, u64),
            prepared: (u64, u64),
            incoming: PathBuf,
            baseline_digest: String,
            receipt_digest: String,
        }
        let identity = |p: &Path| -> io::Result<(u64,u64)> {
            let m = fs::symlink_metadata(p)?;
            if !m.is_dir() { return Err(fail("expected directory")); }
            Ok((m.dev(), m.ino()))
        };
        let baseline = checked(baseline)?;
        let container = checked(container)?;
        let work = checked(&container.join("work"))?;
        let container_lock = File::open(&container)?;
        if unsafe { libc::flock(container_lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err(fail("another cutover is active"));
        }
        let mut authorities = BTreeMap::new();
        for (name, path) in [("catalog",inputs.catalog),("lore",inputs.lore),("registry",inputs.registry),
            ("receipts",inputs.receipts),("shell_journals",inputs.shell_journals),("files_journals",inputs.files_journals)] {
            let path = authority_path(name, &path)?;
            if [&baseline,&container].iter().any(|private| path.starts_with(private) || private.starts_with(&path)) {
                return Err(fail("live authorities overlap private preparation"));
            }
            authorities.insert(name.to_owned(), path);
        }
        let deadline = Instant::now() + Duration::from_secs(30);
        validate_candidate(&baseline, deadline)?;
        let manifest_path = baseline.join("manifest.json");
        let manifest: Manifest = serde_json::from_reader(File::open(&manifest_path)?)?;
        let baseline_digest = digest(&manifest_path, 64 << 20, Some(deadline))?.0;
        let receipt_path = container.join("preparation-receipt.json");
        let receipt_digest = digest(&receipt_path, 1_000_000, Some(deadline))?.0;
        let live = &authorities["lore"];
        let incoming = checked(&candidate_path(&work, &manifest.components["lore"])?)?;
        let mut roots = Vec::new();
        for (name, mapped) in &manifest.components {
            let path = Path::new(mapped);
            let owner = path.iter().next().and_then(|v| v.to_str()).ok_or_else(||fail("invalid component mapping"))?;
            let root = authorities.get(owner).ok_or_else(||fail("unknown component mapping"))?;
            if root.join(path.strip_prefix(owner).map_err(io::Error::other)?) != authorities[name] {
                return Err(fail("live component layout differs from baseline"));
            }
            if mapped == name { roots.push((name.clone(),root.clone())); }
        }
        if manifest.components["lore"] != "lore" || identity(live)?.0 != identity(&incoming)?.0 {
            return Err(fail("atomic exchange requires a separate Lore root and the same filesystem"));
        }
        let _old_lock = lock(&live.join("immutable/lock"))?;
        let intent_path = container.join("cutover-intent.json");
        let existing = match OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(&intent_path) {
            Ok(f) => Some(serde_json::from_reader::<_,Exchange>(f.take(64 << 10))?),
            Err(e) if e.kind() == io::ErrorKind::NotFound => None,
            Err(e) => return Err(e),
        };
        let previous = manifest.entries.iter().find(|e| e.component == "lore" && e.relative.is_empty())
            .map(|e| (e.device,e.inode)).ok_or_else(||fail("missing original store identity"))?;
        if let Some(record) = &existing {
            if record.authorities != authorities || record.incoming != incoming || record.previous != previous ||
                record.baseline_digest != baseline_digest || record.receipt_digest != receipt_digest {
                return Err(fail("cutover intent differs from supplied authorities"));
            }
            if identity(live)? == record.prepared && identity(&incoming)? == previous {
                if fs::read(live.join(crate::FORMAT_MARKER))? != crate::canonical_format_marker() {
                    return Err(fail("exchanged store marker differs"));
                }
                File::open(live.parent().ok_or_else(||fail("missing live parent"))?)?.sync_all()?;
                File::open(incoming.parent().ok_or_else(||fail("missing rollback parent"))?)?.sync_all()?;
                return Ok(incoming);
            }
        }
        // All writers must remain stopped. Take both physical locks before the exchange.
        // If the catalog lives inside the Lore root its copy moves with the store.
        if absent_components(&authorities)? != manifest.absent { return Err(fail("original authority presence changed")); }
        roots.retain(|(name,_)| !manifest.absent.contains(name));
        let _catalog_lock = lock(&authorities["catalog"])?;
        let receipt = prepare_final(&baseline, &container).await?;
        let _new_lock = lock(&incoming.join("immutable/lock"))?;
        let deadline = Instant::now() + Duration::from_secs(30);
        if hash_tree(&work, deadline)? != receipt.work_hash {return Err(fail("prepared data changed"));}
        let mut original = manifest.entries;
        let mut actual = inventory(&roots, 256 << 20, Some(deadline))?;
        let by_path = |a: &Entry,b: &Entry| (&a.component,&a.relative).cmp(&(&b.component,&b.relative));
        original.sort_by(by_path);
        actual.sort_by(by_path);
        if absent_components(&authorities)? != manifest.absent || actual != original || identity(live)? != previous { return Err(fail("original data changed after copy")); }
        let record = Exchange { authorities: authorities.clone(), previous, prepared: identity(&incoming)?,
            incoming: incoming.clone(), baseline_digest, receipt_digest };
        match existing {
            Some(prior) if prior != record => return Err(fail("exchange identities changed")),
            Some(_) => {},
            None => {
                let temp = container.join(format!("cutover-intent-{}.tmp", std::process::id()));
                let mut f = OpenOptions::new().create_new(true).write(true).mode(0o600).open(&temp)?;
                serde_json::to_writer(&mut f,&record)?;
                f.sync_all()?;
                fs::hard_link(&temp,&intent_path)?;
                fs::remove_file(temp)?;
                container_lock.sync_all()?;
            }
        }
        let live_c = CString::new(live.as_os_str().as_bytes())?;
        let incoming_c = CString::new(incoming.as_os_str().as_bytes())?;
        #[cfg(target_os="linux")]
        let rc = unsafe { libc::renameat2(libc::AT_FDCWD,live_c.as_ptr(),libc::AT_FDCWD,incoming_c.as_ptr(),libc::RENAME_EXCHANGE) };
        #[cfg(target_os="macos")]
        let rc = unsafe { libc::renamex_np(live_c.as_ptr(),incoming_c.as_ptr(),libc::RENAME_SWAP) };
        if rc != 0 { return Err(io::Error::last_os_error()); }
        File::open(live.parent().ok_or_else(||fail("missing live parent"))?)?.sync_all()?;
        File::open(incoming.parent().ok_or_else(||fail("missing rollback parent"))?)?.sync_all()?;
        Ok(incoming)
    }

    pub(crate) fn candidate_path(root:&Path, value:&str)->io::Result<PathBuf>{
        let path=Path::new(value);
        if value.is_empty() || path.is_absolute() || path.components().any(|c|!matches!(c,Component::Normal(_))) {return Err(fail("invalid candidate component path"));
            } Ok(root.join(path))
    }
    pub(crate) fn validate_candidate(root:&Path,deadline:std::time::Instant)->io::Result<(PathBuf,PathBuf)> {
        let root=checked(root)?;
        if !root.is_dir(){return Err(fail("candidate is not directory"));
            }
        let manifest_path=root.join("manifest.json");
        let metadata=fs::symlink_metadata(&manifest_path)?;
        if !metadata.is_file() || metadata.len()>64*1024*1024{return Err(fail("invalid candidate manifest"));
            }
        let file=OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(manifest_path)?;
        let manifest:Manifest=serde_json::from_reader(file)?;
        if manifest.version!=1 || manifest.state!="incomplete" || manifest.entries.is_empty() || manifest.entries.len()>MAX_FILES {return Err(fail("candidate inventory unavailable"));
            }
        let required=["catalog","lore","registry","receipts","shell_journals","files_journals"];
        if manifest.components.len()!=required.len() || required.iter().any(|n|!manifest.components.contains_key(*n)) {return Err(fail("incomplete component map"));
            }
        let mut mapped = BTreeMap::new();
        for (name,value) in &manifest.components {
            let path = candidate_path(&root,value)?;
            mapped.insert(name.clone(), authority_path(name,&path)?);
        }
        if absent_components(&mapped)? != manifest.absent { return Err(fail("copied authority presence changed")); }
        let names:BTreeSet<String>=manifest.entries.iter().map(|e|e.component.clone()).collect();
        if names.is_empty() || names.len()>required.len() || names.iter().any(|name| !required.contains(&name.as_str())) {return Err(fail("invalid candidate roots"));
            }
        let mut actual_names=BTreeSet::new();
        for e in fs::read_dir(&root)? {let name=e?.file_name().into_string().map_err(|_|fail("invalid root name"))?;
            if name!="manifest.json" {actual_names.insert(name);
                }} if actual_names!=names{return Err(fail("unlisted candidate root"));
            }
        let roots:Vec<_>=names.into_iter().map(|name|{let path=root.join(&name);
            (name,path)}).collect();
        let actual=inventory(&roots,256*1024*1024,Some(deadline))?;
        let project=|e:&Entry|(e.component.clone(),e.relative.clone(),e.kind.clone(),e.bytes,e.sha256.clone());
        let mut expected:Vec<_>=manifest.entries.iter().map(project).collect();
        let mut observed:Vec<_>=actual.iter().map(project).collect();
        expected.sort();
        observed.sort();
        if expected!=observed {return Err(fail("candidate inventory differs"));
            }
        let source_objects:BTreeSet<_>=manifest.entries.iter().map(|e|(e.device,e.inode)).collect();
        let mut seen=BTreeSet::new();
        for e in &actual {
            if source_objects.contains(&(e.device,e.inode)){return Err(fail("candidate aliases source object"));
                }
            if !seen.insert((e.device,e.inode)){return Err(fail("candidate alias"));
                }
        }
        let catalog=candidate_path(&root,&manifest.components["catalog"])?;
        let lore=candidate_path(&root,&manifest.components["lore"])?;
        if !crate::supported_copy_source(&fs::read(lore.join(crate::FORMAT_MARKER))?) {return Err(fail("unsupported copied Lore marker"));
            }
        Ok((catalog,lore))
    }
    /// Verify one recorded before receipt on a previously inventoried C1 candidate.
    /// Candidate may be mutated by catalog/Lore recovery and must be disposable.
    pub async fn verify_candidate_before(root:&Path, resource:&crate::catalog::ResourceKey, action_id:&str, expected_sha256:Option<&str>) -> io::Result<String> {
        use crate::catalog::{Catalog,VersionContent};
        use lore_storage::{Address,Context,Partition};
        use std::time::{Duration,Instant};
        let deadline=Instant::now()+Duration::from_secs(30);
        let (catalog_path,lore)=validate_candidate(root,deadline)?;
        let account=resource.account_id.as_str();
        let catalog=Catalog::open(&catalog_path,account).map_err(|e|fail(&format!("candidate catalog: {e}")))?;
        let action=catalog.get_action(action_id).map_err(|e|fail(&format!("candidate action: {e}")))?.ok_or_else(||fail("action absent"))?;
        if action.actor_account_id!=account || action.resource_key.account_id!=account{return Err(fail("account mismatch"));
            }
        if catalog.expiry(action_id).map_err(|e|fail(&format!("expiry: {e}")))?.is_some(){return Err(fail("expiry requires separate review"));
            }
        let mut found=action.before_resources.iter().filter(|x|x.resource_key==*resource);
        let receipt=found.next().and_then(|x|x.before.as_ref()).ok_or_else(||fail("resource receipt absent"))?;
        if found.next().is_some(){return Err(fail("ambiguous resource receipt"));
            }
        let reference=match &receipt.content {VersionContent::Empty=>{if expected_sha256!=Some("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"){return Err(fail("empty digest mismatch"));
                    }return Ok("empty".into());
                },VersionContent::Tombstone=>{if expected_sha256.is_some(){return Err(fail("tombstone digest mismatch"));
                    }return Ok("tombstone".into());
                },VersionContent::Bytes(r)=>r};
        let expected=expected_sha256.ok_or_else(||fail("expected byte digest required"))?;
        let partition=Partition::from(catalog.owner_partition(account).map_err(|e|fail(&format!("partition: {e}")))?);
        let context=Context::from(reference.context);
        let hash=reference.hash_hex.parse().map_err(|_|fail("invalid stored hash"))?;
        verify_reference(lore,partition,Address{context,hash},Some(expected),deadline).await
    }
    pub(crate) async fn verify_reference(lore:PathBuf,partition:lore_storage::Partition,address:lore_storage::Address,expected:Option<&str>,deadline:std::time::Instant)->io::Result<String>{
        use lore_storage::{Address,FragmentFlags,FragmentReference,ImmutableStore,ImmutableStoreSettings,LocalImmutableStore,StoreMatch,TypedBytes};
        use std::{sync::Arc,time::Instant};
        const LIMIT:u64=64*1024*1024;
        const NODE_LIMIT:usize=4096;
        const TREE_DEPTH:usize=32;
        let context=address.context;
        let hash=address.hash;
        let settings=ImmutableStoreSettings{protect_local_fragment:true,implicit_durable_stored:false,isolate_partitions:true,flush_background:false,..Default::default()};
        let opened=tokio::time::timeout_at(tokio::time::Instant::from_std(deadline),LocalImmutableStore::new(Some(lore),settings)).await.map_err(|_|fail("verification deadline"))?.map_err(|e|fail(&format!("candidate store: {e}")))?;
        let store:Arc<dyn ImmutableStore>=opened;
        let mut pending=vec![(Address{context,hash},0u64,None::<u64>,Vec::<String>::new())];
        let mut nodes=0usize;
        let mut bytes=0u64;
        let mut work=0u64;
        let mut sum=Sha256::new();
        while let Some((address,start,want,ancestors))=pending.pop(){
            nodes+=1;
            if nodes>NODE_LIMIT || ancestors.len()>TREE_DEPTH || Instant::now()>=deadline{return Err(fail("verification budget exceeded"));
                }
            let id=address.hash.to_string();
            if ancestors.contains(&id){return Err(fail("fragment cycle"));
                }
            let meta=tokio::time::timeout_at(tokio::time::Instant::from_std(deadline),store.clone().get_metadata(partition,address)).await.map_err(|_|fail("verification deadline"))?.map_err(|e|fail(&format!("metadata: {e}")))?;
            if meta.match_made!=StoreMatch::MatchFull || meta.partition!=partition {return Err(fail("fragment scope mismatch"));
                }
            let f=meta.fragment;
            let flag=f.flags;
            let known=FragmentFlags::PayloadFragmented.bits()|FragmentFlags::PayloadCompressedLZ4.bits()|FragmentFlags::PayloadCompressedZstd.bits()|FragmentFlags::PayloadLocalCachePriority.bits()|FragmentFlags::PayloadStored.bits()|FragmentFlags::PayloadDoNotReplicate.bits();
            if flag & !known!=0 || (flag&FragmentFlags::PayloadCompressed.bits()).count_ones()>1 || f.size_payload==0 || f.size_payload as u64>256*1024 || f.size_content>LIMIT || want.is_some_and(|n|n!=f.size_content){return Err(fail("unsupported fragment or size"));
                }
            work=work.checked_add(f.size_payload as u64).filter(|n|*n<=128*1024*1024).ok_or_else(||fail("fragment input budget exceeded"))?;
            lore_storage::validate_fragment_metadata(&f).map_err(|e|fail(&format!("fragment metadata: {e}")))?;
            let got=tokio::time::timeout_at(tokio::time::Instant::from_std(deadline),store.clone().get(partition,address)).await.map_err(|_|fail("verification deadline"))?.map_err(|e|fail(&format!("fragment: {e}")))?;
            if got.match_made!=StoreMatch::MatchFull || got.partition!=partition || got.fragment!=f{return Err(fail("fragment metadata changed"));
                }
            let raw=got.payload.ok_or_else(||fail("fragment payload missing"))?;
            if raw.len()!=f.size_payload as usize{return Err(fail("fragment length mismatch"));
                }
            let (_,content)=lore_storage::read::verify_receipt(f,raw,address).map_err(|e|fail(&format!("fragment integrity: {e}")))?;
            if flag&FragmentFlags::PayloadFragmented.bits()!=0 {
                lore_storage::validate_fragment_list(&f,&content).map_err(|e|fail(&format!("fragment list: {e}")))?;
                let aligned=content.to_aligned::<FragmentReference>();
                let refs=aligned.as_type_slice::<FragmentReference>();
                if refs.len()>NODE_LIMIT.saturating_sub(nodes+pending.len()) || refs[0].offset_content!=start{return Err(fail("fragment list range mismatch"));
                    }
                let end=start.checked_add(f.size_content).ok_or_else(||fail("fragment range overflow"))?;
                let mut next_ancestors=ancestors;
                next_ancestors.push(id);
                for index in (0..refs.len()).rev(){let r=refs[index];
                    let until=if index+1<refs.len(){refs[index+1].offset_content}else{end};
                    let len=until.checked_sub(r.offset_content).filter(|n|*n>0).ok_or_else(||fail("fragment range invalid"))?;
                    pending.push((Address{context,hash:r.hash},r.offset_content,Some(len),next_ancestors.clone()));
                    }
            } else {if content.len() as u64!=f.size_content || start!=bytes{return Err(fail("fragment gap or overlap"));
                    } bytes=bytes.checked_add(content.len() as u64).filter(|n|*n<=LIMIT).ok_or_else(||fail("output budget exceeded"))?;
                sum.update(&content);
                }
        }
        let actual=format!("{:x}",sum.finalize());
        if expected.is_some_and(|value|actual!=value){return Err(fail("content digest mismatch"));
            }Ok(actual)
    }
}
#[cfg(unix)] pub use unix::{Inputs,prepare,verify_candidate_before};
#[cfg(unix)] pub(crate) use unix::{candidate_path,digest,validate_candidate,verify_reference};

#[cfg(any(target_os = "linux", target_os = "macos"))] pub use unix::activate;
