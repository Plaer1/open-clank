//! Explicit offline, copy-only preparation. No live catalog is opened for writing.
#[cfg(unix)]
fn main() {
    let args: Vec<_> = std::env::args_os().collect();
    if args.len() != 4 && !(args.len()==5 && (args[4]=="--format3" || args[4]=="--format4" || args[4]=="--format5")) {
        eprintln!("usage: openclank-history-index-copy SOURCE_QUIESCENT_CATALOG DESTINATION_COPY BATCH_SIZE [--format3|--format4|--format5]");
        std::process::exit(2);
    }
    let batch = args[3].to_str().and_then(|v| v.parse::<usize>().ok()).unwrap_or(0);
    let prepare=if args.len()==5 && args[4]=="--format5" {openclank_history::catalog::prepare_origin_index_copy} else if args.len()==5 && args[4]=="--format4" {openclank_history::catalog::prepare_checkpoint_index_copy} else if args.len()==5 {openclank_history::catalog::prepare_ordered_index_copy} else {openclank_history::catalog::prepare_change_index_copy};
    match prepare(std::path::Path::new(&args[1]), std::path::Path::new(&args[2]), batch) {
        Ok(()) => println!("Prepared copy only. No live catalog was changed or activated."),
        Err(error) => { eprintln!("Preparation refused or incomplete: {error}"); std::process::exit(1); }
    }
}

#[cfg(not(unix))]
fn main() {
    eprintln!("Offline History migration is unavailable on this platform. Keep the previous v0.12-compatible application and helper with the original History data. This command made no changes.");
    std::process::exit(1);
}
