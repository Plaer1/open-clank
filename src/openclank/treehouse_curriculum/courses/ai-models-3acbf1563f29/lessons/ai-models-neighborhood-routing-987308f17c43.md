# Aggregate neighbors and route internal experts

Two separate toys:

- Graph: A=2, neighbors B=4 and C=8. Rule newA=mean(self and neighbors), so A′=14/3. Remove C and recompute.
- MoE: four expert subnetworks have 100 parameters each. Exactly two are selected. Ignore router/shared layers for this count. Compare selecting experts 1/3 with 2/4.

Graph aggregation uses relationships; MoE routing selects internal components. Parameter counts alone do not determine FLOPs, latency or accuracy. Internal experts are not separately authorized agents.
