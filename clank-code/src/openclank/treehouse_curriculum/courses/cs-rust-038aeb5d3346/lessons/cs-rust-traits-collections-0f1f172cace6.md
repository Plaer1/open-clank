# Traits and collection operations

- A trait describes shared behavior; a type implements it. Generic code can require a trait rather than a concrete type.
- Vec stores a growable sequence; HashMap associates keys and values. Borrowing rules still apply when accessing elements.
- An iterator offers values step by step; ownership/borrowing depends on the iterator chosen.

**Try:** Choose Vec or HashMap for ordered recent scores versus lookup by player ID. Name a method/behavior a generic score source needs.
