# Transform one decision problem into another

- Problem A: given a finite simple undirected graph G=(V,E) and integer k with0≤k≤|V|, does G have a clique of size at least k? A clique has every pair joined.
- Problem B: on the same domain, does a graph have an independent set of size at least k? An independent set has no joined pair.
- Build complement H on the same vertices: each pair of distinct vertices is an edge in H exactly when it is not an edge in G. Keep k unchanged; never add self-loops.

**Try:** Transform G with V={a,b,c}, E={ab,bc}, first k=2 then k=3. Give H's edges and both answers, with witnesses or impossibility reasons. Explain both directions for an arbitrary subset, the empty graph with k=0, and why testing all vertex pairs takes polynomial work. State what a solver for B would let you solve.
