# Maps, hashes, collisions

- A map associates keys with values. Hashing selects a bucket; distinct keys can collide.
- Keep equality checks and a collision strategy. Resizing helps control load but costs work.
- Expected constant-time lookup needs assumptions about hashing/load and key work; it is not an unconditional worst-case promise.

**Try:** Keys 2 and 7 hash by key mod 5. Show their bucket and how a lookup for 7 avoids returning 2's value.
