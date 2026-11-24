# Preview owner defaults and pinned decisions

- General Hexes are versioned owner defaults, separate from workspace enforcement. Untagged entries apply owner-wide; tagged entries match any current workspace/task tag, case-insensitively.
- Composition checks owner and sorts by ID/revision. Disabled, unaccepted, unmatched and budget-omitted entries have distinct reasons. Current explicit user instructions take precedence; the General budget does not trim workspace rules.

**Offline owner A, workspace tags [Writing], task tags [review], General budget 50 characters:**

| ID/revision | Title | Body | Tags | Enabled | Accepted |
|---|---|---|---|---|---|
| a/1 | Short | Be brief. | none | yes | yes |
| b/1 | Review | Check facts. | REVIEW | yes | yes |
| c/1 | Off | Use bullets. | Writing | no | yes |
| d/1 | Draft | Cite sources. | Writing | yes | no |
| e/1 | Code | Test changes. | Coding | yes | yes |

The rendered a block is `General default: Short [revision 1]\nBe brief.` (45 characters). The b block needs 49 characters plus the 2-character separator; it matches but exceeds the remaining 5. Outcomes: a included; b omitted-for-budget; c disabled; d unaccepted; e unmatched. A record owned by B makes this owner-A composition invalid instead of silently importing B's defaults.

**No-context-tags variant:** keep owner A, the same five table entries and the same 50-character budget, but set workspace tags [] and task tags []. Outcomes: a included (45 characters); b unmatched; c disabled; d unaccepted; e unmatched. Untagged a remains owner-wide; b no longer reaches the budget check because no context tag matches.

Boundary T pins the original tagged snapshot. Later enabling c changes a new preview, not T's saved snapshot.

**Promotion card:** workspace rule W says `Preserve a recoverable preimage before editing practice/*.md`. Reviewed draft D preserves W's original source/provenance and proposes General body `Preserve a recoverable preimage before editing.` Saving accepted D creates new General identity G, revision 1; W remains unchanged. Removing tags makes G owner-wide, so review that broader scope explicitly. Tags select contexts, not filesystem path enforcement. Assisted drafts need acceptance. Versioned same-owner import creates a new identity with provenance; it does not secretly replace W.

Before saving a promotion or applying a draft, review its exact current revision/hash and required owner acceptance. Updating a governing workspace rule still needs its own owner authorization; executable checks still need separate trust.
