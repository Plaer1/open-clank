# Choose a cryptographic job

- Encryption protects confidentiality under a key/protocol; a cryptographic hash produces a digest and does not by itself authenticate a sender.
- A MAC uses a shared secret for integrity/authentication. A digital signature can be checked with a public key; trust still depends on whose key it is.
- Key agreement establishes key material. Nonces, randomness, certificate validation, and key lifecycle are part of the system.

**Try:** Choose a primitive/job for hiding a stored note versus checking an update publisher. Explain why an untrusted hash beside a download is insufficient.
