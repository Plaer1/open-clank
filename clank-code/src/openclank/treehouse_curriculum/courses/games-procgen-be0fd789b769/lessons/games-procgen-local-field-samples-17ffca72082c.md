# Values and gradients give different fields

Optional practice.

- On x∈[0,1], endpoint values v0=0,v1=1 give V(x)=(1−x)v0+xv1.
- Endpoint gradients g0=+1,g1=−1 give dot contributions d0=x,d1=1−x, blended G(x)=(1−x)d0+x·d1. Linear blending is a teaching simplification, not production Perlin noise.
- Separately, a 3×3 placement mask must exclude road column x=1; coordinates begin at upper left.

**Self-check:** Compute V/G at x=0,1/4,1/2,1. Give the three mask rows (1 permits placement). Explain why a placement mask does not establish walkability.
