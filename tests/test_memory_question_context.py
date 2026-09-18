import pytest

from services.memory.question_context import QuestionContextError, normalize_question_context


def test_question_context_normalizes_typed_handler_target():
    value = normalize_question_context(
        {"mode": "missing_slot", "target": {"kind": "entity", "id": "principal_handler_123"}, "predicate": "preferred_name", "claim_slot": "identity.preferred_name"},
        owner="allie", workspace_id="ws",
    )
    assert value["contract"] == "openclank.question-context/v1"
    assert value["target"]["id"] == "principal_handler_123"
    assert value["scope"]["owner"] == "allie"


@pytest.mark.parametrize("value", [None, {}, {"target": {"kind": "entity"}}, {"mode": "guess", "target": {"kind": "entity", "id": "x"}}])
def test_question_context_rejects_ambiguous_or_invalid_targets(value):
    if value is None:
        assert normalize_question_context(value) is None
    else:
        with pytest.raises(QuestionContextError):
            normalize_question_context(value)
