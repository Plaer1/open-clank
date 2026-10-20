# Follow one task across views

- Tasks, Timeline and Calendar can show the same underlying task. Compare IDs before creating a duplicate.

**Offline record:** id practice-review; title Review draft; track Writing; done=false; due `2026-10-20T09:00:00-10:00`. Tasks shows a row, Timeline shows a dated item and Calendar shows an event, all with id practice-review. The instant is 2026-10-20 19:00 UTC.

Changing the shared record's completion state is different from hiding one view. Use the supplied offset for this conversion rather than guessing the viewer's timezone.

If the due date is changed later, reopen each view and compare the same task ID and new instant. A standalone `09:00` without date/timezone is insufficient to establish that instant.
