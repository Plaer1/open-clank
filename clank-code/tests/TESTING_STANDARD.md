# Open Clank testing standard

Keep a test when it demonstrates a user-visible result, a failure that has regressed, or a meaningful security, data or deployment contract. A new case should distinguish a failure that existing coverage would miss.

Exercise the real decision or entry point. Prefer an observable result over matching variable names, CSS spelling, source snippets, or the existence of a button. Static checks can be appropriate for distributable manifests, packaging boundaries and forbidden dependencies when the source or artifact is itself the contract.

Use the smallest fixture that reaches the behavior. Fix time, randomness and network inputs where they affect the result. Use disposable storage and restore environment variables, imported modules and process state after a test. Parameterize related cases when their setup and expected behavior genuinely match.

Keep expensive browser or platform checks where their environment matters. When a workflow changes, retire the superseded rehearsal after checking the maintained path still catches the original failure. The [suite guide](README.md) lists the focused runners and shared helpers.
