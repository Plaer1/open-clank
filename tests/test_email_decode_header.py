import pytest

from routes.email_helpers import _decode_header


@pytest.mark.parametrize(("subject", "expected"), [
    ("=?x-unknown-charset?B?aGVsbG8=?=", "hello"),
    ("=?totally-bogus?Q?caf=C3=A9?=", "café"),
    ("=?utf-8?B?SGVsbG8gV29ybGQ=?=", "Hello World"),
    ("=?iso-8859-1?Q?caf=E9?=", "café"),
    ("Weekly report", "Weekly report"),
    ("", ""),
    (None, ""),
    ("Re: =?utf-8?b?SsOzc2U=?=", "Re: Jóse"),
    ("=?utf-8?b?SsOzc2U=?= Smith", "Jóse Smith"),
    ("=?iso-8859-1?q?Caf=E9?= =?utf-8?b?5pel5pys?=", "Café日本"),
])
def test_decode_header(subject, expected):
    assert _decode_header(subject) == expected
