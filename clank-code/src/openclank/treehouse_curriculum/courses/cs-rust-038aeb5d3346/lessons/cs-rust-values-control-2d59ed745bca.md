# Rust bindings and expressions

- `let` binds a value; `let mut` permits reassignment. Rust requires a boolean condition for `if`.
- `fn double(n: u32) -> u32 { n * 2 }` returns the final expression without a semicolon.
- u32 is a bounded unsigned integer. State an overflow policy instead of assuming arithmetic is unbounded.

**Try:** For inputs0 and 3, give double's result. Explain why all u32 inputs cannot safely be doubled into u32 under an exact-arithmetic contract.
