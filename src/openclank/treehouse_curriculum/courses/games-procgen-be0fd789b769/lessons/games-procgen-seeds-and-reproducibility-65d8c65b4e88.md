# A seed is part of a recipe

- Reproduce output with the same algorithm version, seed, parameters and relevant input data. A seed alone cannot describe a changed generator.
- Tiny illustrative generator: `next = (5×state+1) mod 16`. Starting at 3 gives 0,1,6,15. This small generator is for tracing, not production randomness.
- Use a value to choose content through an explicit distribution. Mapping values 0–3 to rare loot and the rest to common loot defines a rule you can audit.
- Separate random choices from stable identifiers. Adding one random draw earlier can shift all later results.

**Self-check:** Trace four values starting at 4. Record the complete recipe and explain why changing the modulus invalidates a seed-only replay claim.
