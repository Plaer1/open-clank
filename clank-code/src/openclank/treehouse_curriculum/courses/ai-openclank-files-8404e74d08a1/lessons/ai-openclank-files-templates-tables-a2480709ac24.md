# Distinguish a reusable source from its inserted content

- New from template creates a new document; Editor → Tools → Insert template inserts content into the current document.
- Editing the inserted copy does not automatically update the template. Tables store typed values or derive formula results; presentation does not establish their type.

**Offline template:** `## Decision\nChoice: undecided\nReason: add one sentence.` Insert it into Review. Change only the inserted Choice to `keep` and Reason to `clear`. The template receipt remains unchanged. A table row has quantity 3, price 4 and derived total 12; manually typing a different displayed total is not the same as changing the formula inputs.

Use the following focused exercises to trace insertion and typed table behavior.

**Saved-copy check:** supplied native saved row has quantity 3, price 4 and formula price × quantity. An exported text copy displays `12` but carries no formula evidence. Reopen the native row to test input-driven changes; do not infer a live formula from the exported characters.
