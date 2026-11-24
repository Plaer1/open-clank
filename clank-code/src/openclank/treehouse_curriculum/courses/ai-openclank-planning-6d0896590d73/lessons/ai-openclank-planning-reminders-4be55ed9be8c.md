# A saved schedule is not a delivered reminder

- Inspect the target, timezone, enabled state and run/delivery evidence separately. A schedule can exist without ever running.

**Offline cards:** S1 targets practice-review, weekly Monday at 09:00, timezone Pacific/Honolulu, enabled=false, paused, lastRun=null. S2 says Monday 09:00 but has no timezone. Run R1 failed with `model unavailable`; delivery=null.

S1 is configured but paused; S2 needs a timezone before its intended instant is clear; R1 has no delivered result. No schedule activation, external send or automatic worker restart is needed for this exercise.

Cancelling a schedule or run changes its future execution state; it does not erase an already delivered message. Inspect the cancellation result separately from any earlier delivery receipt.
