# Count recursive work

- A recurrence relates cost to smaller inputs. T(n)=T(n−1)+1 with T(0)=0 gives T(n)=n.
- Splitting into two halves and doing linear combine work gives T(n)=2T(n/2)+n for powers of two; each level contributes n and there are log₂n levels.
- The result counts the chosen operation model, not seconds.

**Try:** Unroll T(4) for the two-half recurrence with T(1)=1. Count levels and total work.
