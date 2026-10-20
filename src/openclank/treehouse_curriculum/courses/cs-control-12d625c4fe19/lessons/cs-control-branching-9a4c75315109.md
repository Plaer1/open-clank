# Decisions and errors

- A predicate selects a branch. Conditions at a boundary need an explicit rule.
- `if age >= 18: label = "adult"` includes 18; `>` would exclude it.
- Invalid input is a separate case: a missing age is not automatically a young age.

**Try:** Write cases for age 17, 18, and missing. State an error or unknown result for missing data.
