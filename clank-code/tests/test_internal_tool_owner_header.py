import httpx

from core.middleware import INTERNAL_TOOL_OWNER_HEADER
from src.tools._common import _internal_headers


def test_internal_owner_header_is_valid_and_shared_by_tool_calls():
    assert INTERNAL_TOOL_OWNER_HEADER == "X-Open-Clank-Owner"
    assert " " not in INTERNAL_TOOL_OWNER_HEADER
    headers = _internal_headers("alice")
    request = httpx.Request("GET", "http://127.0.0.1/", headers=headers)
    assert request.headers[INTERNAL_TOOL_OWNER_HEADER] == "alice"
