from services.search.query import _extract_entities


def _names(q):
    return _extract_entities(q)["names"]


def test_non_ascii_names_are_extracted():
    assert "İstanbul" in _names("İstanbul weather")
    assert "Zürich" in _names("Zürich hotels")
    assert set(_names("trip to São Paulo")) >= {"São", "Paulo"}


def test_ascii_names_unchanged():
    assert _names("What did Alice do in 2024") == ["Alice"]
    assert _names("news about OpenAI and Google") == ["OpenAI", "Google"]


def test_lowercase_camelcase_and_numbers_are_not_names():
    assert _names("the iphone price") == []
    assert _names("iPhone price") == []       # mid-word capital is not a name
    assert _names("top 50 albums") == []


def test_extract_entities_handles_non_string_query():
    # _detect_question_type already guards non-strings, but the function then
    # runs re.findall over `query` directly, which raises TypeError on a
    # non-string. A non-str query should yield no entities.
    assert _extract_entities(None) == {"names": [], "dates": []}
    assert _extract_entities(123) == {"names": [], "dates": []}
