# Local patterns and carried state

- A CNN reuses learned filters across positions, making local patterns useful even when their position changes.
- An RNN processes a sequence while carrying state between steps. LSTM adds gates that regulate what state is retained and updated.
- These are design choices, not guarantees of accuracy. The data, training and evaluation still matter.

A shifted edge in an image suggests shared local filters. A stream of sensor readings suggests a need to preserve information across time.
