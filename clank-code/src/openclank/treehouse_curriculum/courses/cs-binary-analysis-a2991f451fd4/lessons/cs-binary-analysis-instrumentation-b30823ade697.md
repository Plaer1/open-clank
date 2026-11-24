# Observe by adding measurements

- Instrumentation records events such as calls, coverage, allocation, or timing. It can be inserted at build time or during execution.
- Logging each loop iteration adds work and can alter scheduling. Compare with a baseline and define what is measured.
- Process injection places code in another process's execution context; it is a different authority boundary, not a prerequisite for ordinary logging.

**Try:** Design a count of calls to an owned helper. Name an overhead/bias and a baseline comparison.
