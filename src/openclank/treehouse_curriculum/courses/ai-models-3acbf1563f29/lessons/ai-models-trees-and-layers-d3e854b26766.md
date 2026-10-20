# Choose a predictor for the data

- Linear models combine weighted input features; logistic models can turn such a score into a classification probability.
- A decision tree branches on feature tests. A forest combines trees. These are useful candidates for structured tables.
- A feed-forward neural network transforms inputs through learned layers, usually with nonlinear operations. It can learn intermediate representations.

For twelve machine-sensor readings, first define the predicted outcome and compare a simple baseline with a more complex model. Text generation is unnecessary if the required output is a failure probability.
