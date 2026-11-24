# Feedback, stocks, and stability

- A stock accumulates net flow: `next = current + inflow − outflow`. Choose a time step and units before calculating.
- Feedback uses measured state to change later input. Delay can make a correction arrive after conditions change.
- In the model `x_next = a*x`, deviation shrinks for |a|<1; a negative a alternates its sign. This result belongs to this model, not every feedback system.

**Try:** Starting x=8, trace a=0.5 for three steps and a=−1.5 for two. Name one omitted real-world constraint.
