# Java objects and interfaces

- A class defines fields and methods; an object is an instance with its own instance state.
- `class Counter { int n = 0; void inc() { n++; } }` starts each new Counter's n at0; n++ adds one to that object's n. `static` members belong to the class rather than each instance.
- An interface states a contract implementations must satisfy; it is separate from a particular loader or game API.

**Try:** Create two conceptual Counter objects, increment one twice, and predict both n values. Name an operation a Counter interface could expose.
