# Recover a native session without overwriting newer work

- Native session state, conversation revisions and filesystem versions are different scopes. Inspect owner, current revision and busy state before a session operation.

**Offline card:** native session N's inspected revision is 7; current revision is 9 and busy. Disk file D has a newer saved version than the session's remembered snapshot. A stale control request against revision 7 is rejected.

Reload N's current state before choosing a permitted session action. A session fork or rewind is not a filesystem restore and does not justify replacing newer D.
