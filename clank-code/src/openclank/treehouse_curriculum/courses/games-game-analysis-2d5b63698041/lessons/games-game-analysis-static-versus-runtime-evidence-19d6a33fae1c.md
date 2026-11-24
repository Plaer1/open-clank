# Investigate a supplied game artifact

- These are synthetic teaching fixtures, not recorded executions. Save pairs for trace-toy-v1:

| Pair | Known state | Hex bytes |
|---|---|---|
| A | coins5,room2 | 54 52 01 05 00 02 09 AA |
| B | coins261,room2 | 54 52 01 05 01 02 09 AB |
| C | coins5,room3 | 54 52 01 05 00 03 09 AC |

- Both build reports contain literal speed=5. A loads configuration speed 2, dt1 second, x0=0→x1=2. B changes only configuration to8, same binary/start/dt, x1=8. H1: loaded configuration controls movement. H2: configuration is ignored and displacement stays2.
- Archive inventory: flattened marker.png, serialized transform, reference to a missing animation. Extraction recovers present representations; reconstruction invents/restores relationships that need evidence.

**Self-check:** Infer candidate coin/room fields and one discriminating test. Compare H1/H2 against the reports; separate preserved extraction facts from unknown original source.
