//! Explicit offline preparation. No runtime activation or cutover.
#[cfg(unix)]
fn main() {
    use openclank_history::migration_catalog;
    use openclank_history::migration_copy::{prepare, Inputs};
    use std::path::PathBuf;

    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.len() == 1 && args[0] == "limits" {
        println!("Offline candidate limits: 256 MiB inventory; 64 unique references; 4,096 pages; 100,000 rows; 4 MiB/page; 1 MiB/row; 30 seconds/page or final preparation; 600 seconds accumulated successful page time. Deadlines are cooperative. All producer journal directories must be empty. Larger histories and additional codecs/layouts are not qualified.");
        return;
    }
    let fail = |message: &str| -> ! {
        eprintln!("{message}");
        std::process::exit(1)
    };
    if args.len() == 3 && args[0] == "start" {
        migration_catalog::start(&PathBuf::from(&args[1]), &PathBuf::from(&args[2]))
            .unwrap_or_else(|e| fail(&format!("Validation start refused or incomplete: {e}")));
        println!("Incomplete validation checkpoint created; no activation performed.");
        return;
    }
    if (args.len() == 3 && args[0] == "finalize") || (args.len() == 4 && args[0] == "advance") {
        let baseline = PathBuf::from(&args[1]);
        let container = PathBuf::from(&args[2]);
        let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build()
            .unwrap_or_else(|e| fail(&format!("Runtime unavailable: {e}")));
        if args[0] == "advance" {
            let size = args[3].to_str().and_then(|s| s.parse::<usize>().ok())
                .unwrap_or_else(|| fail("Page size must be an integer from 1 to 64."));
            let progress = runtime.block_on(migration_catalog::advance(&baseline, &container, size))
                .unwrap_or_else(|e| fail(&format!("Validation refused or incomplete: {e}")));
            println!("{}", serde_json::to_string(&progress).unwrap());
        } else {
            let receipt = runtime.block_on(migration_catalog::prepare_final(&baseline, &container))
                .unwrap_or_else(|e| fail(&format!("Preparation refused or outcome unknown: {e}")));
            println!("{}", serde_json::to_string(&receipt).unwrap());
        }
        return;
    }
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    if args.len() == 9 && args[0] == "activate" {
        let path = |index| PathBuf::from(&args[index]);
        let baseline = path(1);
        let container = path(2);
        let inputs = Inputs { catalog: path(3), lore: path(4), registry: path(5), receipts: path(6),
            shell_journals: path(7), files_journals: path(8), destination: container.clone() };
        let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build()
            .unwrap_or_else(|e| fail(&format!("Runtime unavailable: {e}")));
        let rollback = runtime.block_on(openclank_history::migration_copy::activate(&baseline, &container, inputs))
            .unwrap_or_else(|e| fail(&format!("Cutover incomplete: {e}. Keep writers stopped and preserve both directories.")));
        println!("Prepared History store activated. Previous store retained at {}", rollback.display());
        return;
    }
    if args.len() == 7 {
        let path = |index| PathBuf::from(&args[index]);
        let inputs = Inputs { catalog: path(0), lore: path(1), registry: path(2), receipts: path(3),
            shell_journals: path(4), files_journals: path(5), destination: path(6) };
        prepare(inputs).unwrap_or_else(|e| fail(&format!("Copy refused or incomplete: {e}")));
        println!("Incomplete copy inventoried; no conversion or activation performed.");
        return;
    }
    eprintln!("Usage: openclank-history-prepare-copy limits | CATALOG LORE REGISTRY RECEIPTS SHELL_JOURNALS FILES_JOURNALS NEW_DESTINATION | start BASELINE CONTAINER | advance BASELINE CONTAINER PAGE_SIZE | finalize BASELINE CONTAINER | activate BASELINE CONTAINER CATALOG LORE REGISTRY RECEIPTS SHELL_JOURNALS FILES_JOURNALS\nStop and drain all History, Files and shell writers first; locks do not prove quiescence. Only activate switches the verified store.");
    std::process::exit(2);
}

#[cfg(not(unix))]
fn main() {
    eprintln!("Offline History migration is unavailable on this platform. Keep the previous v0.12-compatible application and helper with the original History data. This command made no changes.");
    std::process::exit(1);
}
