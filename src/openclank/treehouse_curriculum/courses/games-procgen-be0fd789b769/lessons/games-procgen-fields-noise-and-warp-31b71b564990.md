# A field gives each position a value

- A scalar field can store height, density or a mask; a vector field stores a direction. A signed distance field encodes signed distance to a boundary under its chosen convention.
- Value noise interpolates sampled values; gradient noise uses gradients. Simplex-family constructions use a different lattice. Cellular/Worley signals use distances to feature points; Voronoi regions assign points to nearest sites.
- Layering frequencies adds scales: `h(x)=n(x)+0.5n(2x)`. Frequency growth is lacunarity; amplitude reduction is gain. Ridged transforms and coordinate domain warping produce other shapes.
- A texture that resembles terrain has not yet supplied navigable terrain.

**Self-check:** Given n(x)=0.2 and n(2x)=−0.4, compute h. Then describe a mask that keeps generated trees out of a road without changing the terrain generator.
