# Hide a changeable representation

- A module exposes operations and hides decisions callers should not depend on.
- A rational-number module can expose make/add/value while keeping numerator and denominator private. Its representation invariant includes denominator≠0.
- Callers using the interface survive a later change from pairs to another representation.

**Try:** Propose a tiny stack interface, its empty-pop policy, and one internal detail callers must not use.
