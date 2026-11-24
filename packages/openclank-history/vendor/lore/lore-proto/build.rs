// Open Clank packaging policy: compile reviewed pregenerated bindings only.
fn main() {
    const GENERATED: &[&str] = &[
        "epic.events.rs",
        "epic_urc.rs",
        "lore.environment.v1.rs",
        "lore.model.v1.rs",
        "lore.notification.rs",
        "lore.repository.v1.rs",
        "lore.revision.v1.rs",
        "lore.storage.v1.rs",
        "lore.thin_client.v1.rs",
        "lore.user.v1.rs",
        "ucs.auth.rs",
        "urc.lock.rs",
        "urc.model.rs",
        "urc.notification.rs",
        "urc.rpc.rs",
    ];
    for name in GENERATED {
        let path = std::path::Path::new("src/grpc").join(name);
        println!("cargo:rerun-if-changed={}", path.display());
        assert!(path.is_file(), "required pregenerated Lore binding is missing: {}", path.display());
    }
}
