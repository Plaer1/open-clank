Target stays(200,0). Replace the equivalent inline expression with this helper and call it with player x/y; append the test in main.rs.

```rust
fn overlaps_target(x: f32, y: f32) -> bool {
    (x - 200.0).abs() <= 20.0 && y.abs() <= 20.0
}
#[test]
fn inclusive_edges() {
    assert!(overlaps_target(180.0, 0.0));
    assert!(overlaps_target(220.0, 0.0));
    assert!(!overlaps_target(179.99, 0.0));
    assert!(!overlaps_target(220.01, 0.0));
    assert!(overlaps_target(200.0, 20.0));
    assert!(!overlaps_target(200.0, 20.01));
}
```

Later `cargo test --locked` should pass all six assertions; this is predicted until run. Half-widths12+8 give20. A single x150→240 step misses sampled overlap although the segment crosses the target. Reset both position and won; leaving won=true preserves the early return.