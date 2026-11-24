# Source to running process

- Preprocessing expands source directives; compilation lowers source through representations to machine code; an assembler makes an object file.
- A linker combines objects/libraries and resolves references into an executable or library. The OS loader maps the executable and required libraries into a process.
- `bump(x){return x+1;}` may become a few instructions. Optimization can inline or remove calls without preserving source shape.

**Try:** Label source, preprocessed text, assembly, object, executable, process. Explain why an unresolved external name can fail at linking after compilation succeeded.
