# Detection is not response

- A collider describes a test shape. A trigger reports overlap; a solid-body solver can also compute response. A visible sprite alone does neither.
- For circles with radii 2 and 3, centers 4 units apart overlap because 4 < 2+3. Decide separately whether touching at 5 counts.
- Broad-phase checks reject distant pairs cheaply; narrow-phase checks test candidate shapes more precisely.
- Fast motion can cross a thin wall between samples. Smaller steps, swept tests or an engine's continuous collision mode address different cases; test the actual setup.

**Self-check:** Test center distances 4,5,6 using an inclusive touching rule. Then describe a response for a collectible and a response for a wall.
