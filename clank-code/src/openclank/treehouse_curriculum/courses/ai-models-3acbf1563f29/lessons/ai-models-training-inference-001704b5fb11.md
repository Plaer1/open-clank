# Training changes parameters; inference uses them

- Supervised learning uses example–target pairs. Self-supervised learning derives targets from the data, such as predicting held-out text.
- Unsupervised learning looks for structure without explicit answer labels. Reinforcement learning uses rewards associated with actions or outcomes.
- Fine-tuning continues training an existing model. Instruction tuning uses task/instruction examples to change its behavior.
- In ordinary inference, adding a document changes the available context; it does not itself retrain the model.

Example: showing a model a new style guide and updating its weights on style examples are different operations.
