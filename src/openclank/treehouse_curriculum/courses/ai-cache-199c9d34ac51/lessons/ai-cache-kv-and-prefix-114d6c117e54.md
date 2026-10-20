# Cache computation, not knowledge

- A KV cache reuses attention states from an already-processed prefix during generation.
- Prompt caching can reuse processing across requests with eligible matching context. Provider rules determine matching, retention and accounting.
- A cache hit neither trains the model nor removes the material from context. Application memory decides what to supply again; caching may reduce repeated work.

Saving a preference is memory. Reusing processing for an unchanged supplied prefix is caching.
