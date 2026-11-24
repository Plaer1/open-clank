# Check a Skill's eligibility and tools

- A Skill is reusable guidance with its own state. Installed, eligible and active are different checks; the available tool set still limits execution.

**Offline Skill S:** purpose crop an image; requires image-edit. Installed=true; eligible=false. Current active tools: read_file only. Variant B sets eligible=true but still exposes only read_file. Neither card establishes an available image-edit tool or completed crop.

Reloading discovery may refresh what the interface knows; it does not grant a missing tool or permission. Inspect the active immutable revision when using a published Skill.
