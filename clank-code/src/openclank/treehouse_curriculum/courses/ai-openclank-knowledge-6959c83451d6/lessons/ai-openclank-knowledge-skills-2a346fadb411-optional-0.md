- In Memery's Skills tab, inspect the expanded Skill's draft/head and active revision/hash. Saving changes stages a draft; invocation can still read the immutable active revision.

**Owned Skill S, owner A:** active r1/H1 procedure: `Count the supplied rows. Return the count.` Changed draft r2/H2 procedure: `Count the supplied rows. Return the count and the positions of rows with empty labels.` Input rows, in order: `[{label: "Brush"}, {label: ""}, {label: "Saw"}]`.

**State cards:** before editing, next invocation reads r1 and returns count 3. After Save r2/H2, active remains r1/H1; next invocation still uses r1. Audit E1 is pinned to r1/H1, so it does not establish publication readiness for r2/H2. A completed Test of r2 can return count 3 and empty-label position 2, but does not publish. A successful authorized Publish of r2/H2 with its required current evidence changes the active pointer; next invocation then uses r2. Explicit Unpublish of the active revision removes that active publication; it does not establish a new active draft.

Wrong owner cannot publish S. An unavailable audit or model leaves the needed check pending. Compare exact revision and hash; do not reuse an old passing label or assume Test is an inspection-only operation. These are simplified display cards, not invented API audit payloads.

**Try on paper:** name the next invocation's revision after Save, Test and successful Publish, then identify the stale audit. **Check:** r1, r1, r2; E1 applies only to H1. No real agent test or privileged waiver is required.