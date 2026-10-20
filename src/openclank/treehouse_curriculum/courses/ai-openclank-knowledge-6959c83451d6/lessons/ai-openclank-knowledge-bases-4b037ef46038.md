# Interpret a filtered view

- A Base view filters and sorts source records. Hiding a row is not deleting it; a result count may describe only the matched view.

**Offline source:** record A, id a, status open, priority 2. Record B, id b, status done, priority 1. View: status equals open; priority descending. Result: A. Remove the filter: A then B; source count remains 2. The supplied write attempt on B is denied because B is read-only.

Keep filter state, source count and permissions distinct. An incomplete index can also make the visible result partial.

**Mutation variant:** if authorized A changes status to done, the open-only view becomes empty while both source records still exist. Inspect current row permission and revision before that mutation.
