# Test a pure Rust helper

- Cargo manages a Rust package; Cargo.toml declares it. Unit tests commonly use `#[test]` and assertions.
- For `fn bump(x:u32)->Option<u32>{x.checked_add(1)}`, check2→Some(3) and u32::MAX→None.
- Compilation checks language/type rules; assertions check selected expected behavior.

**Try:** Write both test assertions and explain what defect each catches. If running externally, record tool version and actual result.
