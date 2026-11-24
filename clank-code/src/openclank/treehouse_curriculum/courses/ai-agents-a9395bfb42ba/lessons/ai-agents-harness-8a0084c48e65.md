# What the harness supplies

- The model produces outputs from supplied context. The harness assembles context, exposes tools, checks requests, stores state and decides how to continue.
- Changing tool access or instructions can change the system's behavior without changing the model.

Trace: request → model proposes a file read → harness checks scope → tool returns bytes → model explains them.
