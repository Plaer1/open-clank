# Mediated execution and isolation

- Dynamic binary translation converts blocks during execution; QEMU TCG translates target instructions for the host.
- Virtualization presents a guest machine using hypervisor facilities; emulation and virtualization can be combined.
- A behavioral sandbox observes execution within an isolated environment. VM introspection observes guest state from outside the guest; neither guarantees faithful behavior or perfect containment.

**Try:** Classify translating another CPU's instructions versus observing a guest's memory from the hypervisor. Name one possible environment mismatch.
