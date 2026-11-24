# Make conflict behavior explicit

- An override needs a declared resolution rule. “Later wins” is a policy to test, not a rule shared by all games.
- Toy policy: base record speed=2; mod A sets speed=3; mod B sets speed=5. Loading A then B gives 5. A compatibility patch can deliberately choose 4 after both.
- Dependencies say what must exist; load order says when it is applied. Sorting cannot resolve every semantic conflict, and dependency cycles need an explicit error.
- Match logical resource identity, not only archive filenames. Two different packages can target the same item or method.

**Self-check:** Evaluate the toy record under B then A, then A,B,patch. Propose a manifest dependency for the patch and a diagnostic for a missing prerequisite.
