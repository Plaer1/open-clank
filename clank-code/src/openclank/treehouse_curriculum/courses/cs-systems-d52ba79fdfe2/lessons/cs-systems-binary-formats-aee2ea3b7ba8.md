# Read binary structure

- ELF, PE/COFF, and Mach-O are format families with headers and tables describing code/data and linking/loading information.
- Sections organize pieces such as code, data, and symbols; loading uses format-specific mappings/segments. File offsets and runtime virtual addresses are different coordinates.
- Relocations describe addresses/references needing adjustment; debug information can associate machine locations with source.

**Try:** An object contains a symbol for bump but an executable runs main. Explain why a symbol table alone neither executes code nor proves a function's source.
