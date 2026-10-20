# Capacity, delay and missing deliveries

- Capacity limits how much data fits per second. Delay is travel time; delay variation changes spacing. Loss means a delivery never occurs. A fast connection can still have a long path.
- Synthetic clocks are synchronized. Packets 0–3, each 100 bytes, leave at 0, 50, 100, 150 ms. Observation ends at 350 ms.

| Trace | Arrivals for IDs 0,1,2,3 (ms) |
|---|---|
| A | 100,150,200,250 |
| B | 100,180,205,310 |
| C | 100,150,not delivered,250 |

- The fixture declares packet 2 in C dropped. Without that ground truth, silence could mean pending delivery.
- Another request leaves at 10 ms; its reply returns at 210 ms.

**Self-check:** Classify A/B/C. Calculate B's delays and max−min variation, payload rate at 20 updates/second, and request RTT. Explain why RTT/2 needs an assumption.
