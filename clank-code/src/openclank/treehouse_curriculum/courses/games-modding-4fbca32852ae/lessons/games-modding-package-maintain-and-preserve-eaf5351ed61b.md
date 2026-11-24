# Package a change with a recovery path

- A release needs identity/version, compatible game/loader versions, dependency list, original payload, provenance, installation, removal and known conflicts.
- Test baseline → install → exercise changed behavior → remove → baseline. Save-format changes may need migration or backup restoration rather than simple deletion.
- A checksum detects changed bytes; it does not by itself prove authorship or safe behavior. Keep the exact source and build recipe when preserving an old version.
- Workshop, Nexus Mods and mod.io are distribution ecosystems with their own rules. A supported upload route is not permission to bundle somebody else's base game or assets.
- Multiplayer participation adds server policy and shared-state concerns; test in your own isolated environment.

**Self-check:** Write a release record for the toy speed mod, including one conflict, removal test and a preservation record. List exactly which files you would package.
