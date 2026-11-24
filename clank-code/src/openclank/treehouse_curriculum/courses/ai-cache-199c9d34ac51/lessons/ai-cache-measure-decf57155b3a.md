# Measure useful cost, not guessed savings

- Read the provider's usage fields and current rates. Determine whether cached tokens are a subset and whether writes have separate accounting.
- Compare latency and outcome quality too. A cheaper wrong answer is not a useful result.

Toy rates per 1,000 tokens: fresh input 2 credits, cached input 0.5, output 4. Receipt: input 1,000 including 600 cached; output 100. Cost = 0.4×2 + 0.6×0.5 + 0.1×4 = 1.5 credits. These are invented exercise rates.
