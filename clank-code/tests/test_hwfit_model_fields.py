import pytest

from services.hwfit.models import is_prequantized, params_b


@pytest.mark.parametrize(("model", "expected"), [
    ({"parameter_count": 7}, 0.0),
    ({"parameter_count": ["7B"]}, 0.0),
    ({"parameter_count": "1.5.3B"}, 0.0),
    ({"parameter_count": "7.0.1B"}, 0.0),
    ({"parameter_count": "7B"}, 7.0),
    ({"parameter_count": "70B"}, 70.0),
    ({"parameter_count": "355M"}, 0.355),
    ({"parameters_raw": 7_000_000_000}, 7.0),
])
def test_catalog_parameter_count(model, expected):
    assert params_b(model) == expected


@pytest.mark.parametrize(("model", "expected"), [
    ({"quantization": 8}, False),
    ({"name": "plain-model", "quantization": 123}, False),
    ({"name": "some-model-awq"}, True),
    ({"quantization": "FP8-Mixed"}, True),
])
def test_catalog_prequantization(model, expected):
    assert is_prequantized(model) is expected
