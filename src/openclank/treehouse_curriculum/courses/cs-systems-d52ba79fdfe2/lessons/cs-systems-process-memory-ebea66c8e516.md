# Objects, address spaces, resources

- A process has a virtual address space and OS-managed resources. Its mappings can include code, libraries, stack regions, heap allocations, and files.
- A pointer/address is meaningful in its address space and lifetime. Freed storage can be reused; a stale address is not a durable identity.
- The OS enforces process/privilege boundaries. A function call alone is not a security boundary.

**Try:** Explain why the same numeric address in two processes need not refer to the same data. Identify a use-after-free assumption.
