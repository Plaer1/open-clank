# Find a lower-bound position

- Linear search checks items until it finds a match. Binary search discards a half only when the input ordering justifies it.
- Lower bound finds the first position whose value is at least x, or n. Keep the interval [lo,hi), initially [0,n).
- While lo<hi: m=(lo+hi)//2; if A[m]<x set lo=m+1, otherwise hi=m. Return lo.

**Try:** Trace A=[1,3,3,7], x=3; then empty A. State why `lo=m` in the first branch can get stuck.
