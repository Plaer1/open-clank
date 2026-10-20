# Structured data across boundaries

- Serialization turns structured values into a transport/storage representation. JSON represents objects, arrays, strings, numbers, booleans, and null.
- Python `json.loads('{"score": 4}')` produces a dictionary. That does not prove `score` is valid for your application.
- Round trips preserve only supported distinctions: a Python tuple encoded as a JSON array decodes as a list; object identity is not preserved.

**Try:** For a record with score and player name, propose a shape check and a value check. Explain one thing JSON does not preserve.
