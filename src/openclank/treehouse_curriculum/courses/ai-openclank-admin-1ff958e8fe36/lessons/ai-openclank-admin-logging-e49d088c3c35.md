# State what the log actually captured

- Logs have time, scope and redaction boundaries. Missing data can mean capture ended before an event, not that the event failed.

**Offline card:** request q1 starts at 09:00. Log capture ends 09:01 with no result entry. Independent same-resource readback at 09:02 shows the requested green revision. A shareable diagnostic excerpt retains q1, timestamps and redacted error/status fields, but removes credentials and private content.

Use the readback for the later resource state; do not invent a log entry that was never captured.
