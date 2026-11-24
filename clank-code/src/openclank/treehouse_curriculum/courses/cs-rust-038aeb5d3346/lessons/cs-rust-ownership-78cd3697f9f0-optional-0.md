The call length(label) moves ownership into s, so the later print cannot borrow label. Use `fn length(s: &str) -> usize { s.len() }` and `length(&label)`. A consistent &String signature/borrow also works; cloning creates another owned allocation. Expected repaired output: `length=2; label=hi`. For UTF-8 "é", byte length remains2, not two perceived letters.

Complete reference repair, save separately as length_fixed.rs:

```rust
fn length(s: &str) -> usize { s.len() }
fn main() {
    let label = String::from("hi");
    let n = length(&label);
    println!("length={n}; label={label}");
}
```