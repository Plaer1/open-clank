# Read connection errors before changing settings

- Identify provider, endpoint, protocol, model and account separately. An API-compatible endpoint may support only some modalities or tools.
- A 401 usually points to authentication, 403 to access and 429 to rate/quota limits; inspect the actual error. Network failure and unknown-model errors need different checks.
- Preserve the request while diagnosing. A safe report describes status and scope without exposing a credential.

Synthetic result: the endpoint responds, but model identifier M9 is unknown. Check the supplied model catalogue and request identifier before retrying authentication. A separate 429 result calls for checking the reported quota/rate condition, not renaming M9.
