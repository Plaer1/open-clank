# Test the mod you will distribute

- Run `./gradlew build`; identify the intended mod JAR in `build/libs` rather than assuming every JAR is installable. Check its manifest, resources and dependency declarations.
- Test that JAR in a separate matching Fabric client profile with the declared Fabric API. A development launch can hide packaging mistakes.
- Before removal, back up the disposable world and quit. Remove only the authored mod JAR; reopen that disposable copy and record unknown-item/recipe behavior or load failure. Quit, restore the JAR and original backup, then recheck. Restoring a JAR after saving a stripped world may not recover lost contents.
- For a version port, compare game APIs, mappings, loader/API, resource schemas and side behavior. Changing only the version string is not a compatibility test.
- Distribute your mod and its permitted dependencies, not the base game. Recheck current game and distribution terms before publishing.

**Self-check:** Record installation, item/recipe behavior and removal in the disposable profile. Produce a short compatibility record and a package inventory with provenance.
