With rustc 1.97.1 and edition 2024, save this as lib.rs in a fresh owned directory:

```rust
pub fn bump(x:u32)->Option<u32>{x.checked_add(1)}
#[test] fn small(){assert_eq!(bump(2),Some(3));}
#[test] fn boundary(){assert_eq!(bump(u32::MAX),None);}
```

Run `rustc --edition=2024 --test lib.rs -o rust-tests`, then `./rust-tests`. Expect two passing tests. Record your actual version and result; preserve any error and repair it before claiming success. In a Cargo library package these tests can live in src/lib.rs and run with `cargo test`; keep the package's chosen toolchain. Written test design and an actual passing run are distinct outcomes.