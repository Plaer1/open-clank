# Restore the intended scope

- Identify the resource and captured revision before recovery. Preserve unsaved current work before replacing it.
- A recovery mechanism can restore only the data within its captured scope.

**Offline history card:** document D has saved revision 1 blue and revision 2 green. Current unsaved draft is red. Native session N has its own session history. External file X was never captured. To restore D's blue version, first preserve red, select D revision 1 and inspect the result on D. This does not restore N or X.

For broader recovery consult Settings → Help → Recovery and the [maintained backup and restore guide](https://github.com/Plaer1/open-clank/blob/main/docs/backup-restore.md).
