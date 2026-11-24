# Build, detect, recover

- Validate shape and meaning at boundaries; use APIs that keep data separate from commands. Encode output for its destination context.
- Use least privilege, reviewed dependencies/build inputs, safe error paths, and tests of forbidden actions.
- Plan detection and recovery: useful logs without secrets, update ownership, rollback, and root-cause fixes.

**Try:** For a toy upload service, propose an input limit, permission check, error behavior, and recovery signal.
