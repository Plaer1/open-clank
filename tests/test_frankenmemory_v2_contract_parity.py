import json
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads(
    (ROOT / "contracts/frankenmemory/v2/schema.json").read_text(encoding="utf-8")
)
FIXTURES = json.loads(
    (ROOT / "contracts/frankenmemory/v2/contract-fixtures.json").read_text(
        encoding="utf-8"
    )
)


def test_canonical_fixtures_match_python_wire_contract() -> None:
    for definition in ("operation", "scope", "typed_value"):
        validator = Draft202012Validator(SCHEMA["$defs"][definition])
        for case in FIXTURES[definition]:
            assert validator.is_valid(case["value"]) is case["valid"], case["name"]
