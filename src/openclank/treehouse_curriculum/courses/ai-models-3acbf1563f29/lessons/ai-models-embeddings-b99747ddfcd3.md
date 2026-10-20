# Represent items for comparison

- An embedding is a numerical representation. A similarity measure lets a system rank candidate items in that representation space.
- Useful proximity depends on the model, task and data. Similar text can disagree; a close match is not necessarily a correct answer.
- Embeddings also support clustering/grouping, duplicate candidates, recommendations, classification and ranking. Example: represent product descriptions, rank nearby candidates, then validate recommendations against availability and user needs. The representation and downstream operation are distinct from generating language.
- Retrieval may combine keyword search, embeddings, filters and reranking.

For “return policy,” a nearby passage about refunds may help. An expired policy with excellent similarity still needs a date and source check.
