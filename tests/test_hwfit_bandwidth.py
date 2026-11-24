import pytest

from services.hwfit.fit import _lookup_apple_bandwidth, _lookup_bandwidth


@pytest.mark.parametrize(("gpu", "cores", "expected"), [
    ("Apple M3 Max", 30, 300), ("Apple M3 Max", 40, 400),
    ("Apple M4 Max", 32, 410), ("Apple M4 Max", 40, 546),
    ("Apple M5 Max", 32, 460), ("Apple M5 Max", 40, 614),
])
def test_apple_bandwidth_by_core_count(gpu, cores, expected):
    assert _lookup_bandwidth({"gpu_name": gpu, "gpu_cores": cores}) == expected


@pytest.mark.parametrize(("gpu", "expected"), [
    ("Apple M3 Max", 300), ("Apple M4 Max", 410), ("Apple M5 Max", 460),
    ("Apple M5 Pro", 307), ("Apple M5", 153),
    ("NVIDIA Quadro M3 000", None), ("NVIDIA Quadro M4 000", None),
    ("NVIDIA Quadro M5 000", None),
    ("NVIDIA GeForce RTX 4090", 1008), ("Totally Unknown GPU", None),
])
def test_bandwidth_without_core_count(gpu, expected):
    assert _lookup_bandwidth({"gpu_name": gpu}) == expected
    assert _lookup_bandwidth(gpu) == expected


@pytest.mark.parametrize("gpu", [123, ["x"], None])
def test_invalid_probe_name_has_unknown_bandwidth(gpu):
    assert _lookup_bandwidth(gpu) is None


@pytest.mark.parametrize(("gpu", "cores"), [
    ("NVIDIA GeForce RTX 4090", 128), ("AMD Radeon RX 9070 XT", 64),
])
def test_non_apple_probe_with_cores_skips_apple_table(gpu, cores):
    assert _lookup_apple_bandwidth({"gpu_name": gpu, "gpu_cores": cores}) is None
