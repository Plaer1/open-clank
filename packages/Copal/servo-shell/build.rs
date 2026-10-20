use std::path::Path;

fn inspect(root: &Path) {
    if !root.exists() {
        panic!("public native assets are missing; run the reviewed public build first");
    }
    for entry in std::fs::read_dir(root).expect("cannot inspect public native assets") {
        let entry = entry.expect("cannot inspect public native asset");
        let name = entry.file_name();
        let name = name.to_string_lossy();
        let kind = entry.file_type().expect("cannot inspect public native asset type");
        if kind.is_symlink() || [".clanker", ".references", ".archive", ".obsidian", "move-data.json"].contains(&name.as_ref())
            || name.starts_with("history-credentials") || name.starts_with(".env") {
            panic!("private runtime payload refused in embedded public assets");
        }
        if kind.is_dir() { inspect(&entry.path()); }
    }
}

fn main() {
    if std::env::var_os("CARGO_FEATURE_EMBEDDED_ASSETS").is_some() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../out");
        println!("cargo:rerun-if-changed={}", root.display());
        inspect(&root);
        if !root.join("examples/planning.json").is_file() {
            panic!("synthetic public planning fallback is missing");
        }
    }
}
