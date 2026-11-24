"""WeCom file cipher, independent of untrusted URL transport.

Format: official WecomTeam Python SDK `aibot/crypto_utils.py` (AES-256-CBC,
IV = first 16 key bytes, 32-byte PKCS#7). Fetching uses the existing
DNS-pinned public HTTPS owner and never forwards channel credentials.
"""
import base64
import binascii
from urllib.parse import unquote
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAX_CIPHERTEXT = 10 * 1024 * 1024 + 32

def decrypt_attachment(ciphertext: bytes, encoded_key: str) -> bytes:
    if not isinstance(ciphertext,bytes) or not ciphertext or len(ciphertext)>MAX_CIPHERTEXT or len(ciphertext)%16:
        raise ValueError('Invalid encrypted attachment')
    if not isinstance(encoded_key,str) or not encoded_key or len(encoded_key)>128:
        raise ValueError('Invalid attachment key')
    try:
        key_text=unquote(encoded_key).encode('ascii')
        key=base64.b64decode(key_text+b'='*(-len(key_text)%4),altchars=b'-_',validate=True)
    except (UnicodeError,ValueError,binascii.Error) as exc:
        raise ValueError('Invalid attachment key') from exc
    if len(key)!=32: raise ValueError('Invalid attachment key')
    decryptor=Cipher(algorithms.AES(key),modes.CBC(key[:16])).decryptor()
    plain=decryptor.update(ciphertext)+decryptor.finalize()
    count=plain[-1]
    if not 1<=count<=32 or count>len(plain) or plain[-count:] != bytes([count])*count:
        raise ValueError('Invalid attachment padding')
    return plain[:-count]


async def fetch_attachment(url: str, encoded_key: str) -> bytes:
    """Use the existing public-IP-pinned fetch owner; no redirects or credentials."""
    import asyncio
    from urllib.parse import urlsplit
    from services.search.content import _get_public_url
    if not isinstance(url,str) or len(url)>4096: raise ValueError('Invalid attachment URL')
    try:
        parsed=urlsplit(url)
        if (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None,443) or parsed.fragment): raise ValueError('Invalid attachment URL')
    except ValueError as exc: raise ValueError('Invalid attachment URL') from exc
    response=await asyncio.to_thread(_get_public_url,url,{},15,max_redirects=0,max_bytes=MAX_CIPHERTEXT)
    if response.status_code != 200 or response.truncated: raise ValueError('Attachment download unavailable')
    return decrypt_attachment(response.content,encoded_key)
