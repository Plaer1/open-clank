# Check that an edit survived reopening

- Open an authorized disposable note in Editor. Source mode changes the view; typing changes the draft. Use File → Save file or Save, then reopen the same resource from Files or Editor's list.
- Compare content and identity after reopening. A toast alone is weaker evidence than the saved resource.

**Offline normal card:** resource P/Details.md, disk revision 1 `Color: blue`; editor draft `Color: green`. Before Save the disk remains blue. Successful save receipt: same resource, revision 2, green. Reopen reads revision 2, green.

**Offline conflict card:** another writer saved `Color: red` as revision 2 before the green draft's save against revision 1. Preserve green and red, inspect the conflict, and choose a deliberate resolution. Do not overwrite merely to clear a warning.

Optional configured practice uses your own authorized disposable note. Record its identity, before text, after text and reopened text. A failed, unavailable or unconfirmed attempt stays an attempt.
