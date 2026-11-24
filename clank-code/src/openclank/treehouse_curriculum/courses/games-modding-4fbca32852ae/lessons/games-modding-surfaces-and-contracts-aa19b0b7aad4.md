# Choose the supported change surface

- Configuration changes values; data mods change records; asset overrides replace resources; plugins add behavior through an interface. A package is a container, not a guarantee that its contents are passive.
- Begin with the game-specific mod contract: supported versions, identifiers, schema, dependencies, loading rules and permitted distribution. The engine name alone does not establish that contract.
- Godot can load developer-designed PCK/ZIP packs; other games may expose editors, asset bundles or scripting interfaces. A shipped build may contain cooked resources rather than editable source.
- Runtime hooks, injection and binary patching operate at a different boundary and can be version-fragile. Their general theory is in Programming; this exercise uses an owned toy data contract.

**Self-check:** Define a toy mod contract allowing only `{id, displayName, speed}` records, with speed between 1 and 5. Give one accepted record and two rejected records with reasons.
