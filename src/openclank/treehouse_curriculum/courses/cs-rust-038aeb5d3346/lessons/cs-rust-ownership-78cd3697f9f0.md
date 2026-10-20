# Moves and borrows

- Moving a non-Copy value transfers ownership. A reference borrows access; a read-only helper can borrow instead of owning.
- This complete `length_broken.rs` must leave the single String owned by main and retain the final print. String/str length counts UTF-8 bytes.

```rust
fn length(s: String) -> usize { s.len() }
fn main() {
    let label = String::from("hi");
    let n = length(label);
    println!("length={n}; label={label}");
}
```

**Try:** Identify the move and later conflicting use. Repair the helper signature and call with a shared borrow, preserving the print without cloning. Explain why cloning is a different ownership/cost choice. Optionally predict the repaired output; a prediction is not a compiler run.
