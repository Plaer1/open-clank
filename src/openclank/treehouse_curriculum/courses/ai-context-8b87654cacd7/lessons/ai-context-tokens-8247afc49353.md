# Tokens are encoded units

- A tokenizer turns text into units such as word pieces, punctuation or whitespace patterns. Different tokenizers can split the same text differently.
- Word counts and character counts are not reliable token limits. Images/audio can have their own accounting.
- Input, output and cached-input counters answer different questions. Missing counters mean unknown, not zero.

Synthetic receipt: input 800, output 120, cached input 500 included in input. Fresh input is 300; total input remains 800.
