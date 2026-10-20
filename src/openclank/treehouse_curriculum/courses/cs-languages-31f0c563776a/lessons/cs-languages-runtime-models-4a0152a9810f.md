# Compile, interpret, execute

- A compiler translates representations; an interpreter executes according to a representation's rules. A VM supplies an abstract execution target.
- A JIT compiles during execution. A language implementation can combine bytecode, interpretation, and native compilation.
- Garbage collection, explicit ownership, and manual allocation are different memory-management approaches; none fixes every resource bug.

**Try:** Sketch source→bytecode→VM and source→native machine code. Explain why 'interpreted language' is an incomplete description.
