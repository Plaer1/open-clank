# Potential paths and observed runs

- Static analysis studies artifacts without relying on a particular target execution: headers, strings, imports, instructions, and control flow.
- Dynamic analysis records what happened for selected inputs and environment. It can reveal values unavailable in the file.
- A string's presence does not prove it was used; an unobserved branch is not necessarily unreachable.

**Try:** A binary contains 'denied' but one run prints 'ok'. State what each observation supports and one next experiment.
