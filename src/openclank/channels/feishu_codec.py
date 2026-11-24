"""Feishu/Lark long-connection frame codec.

Wire field numbers and message handling are checked against the official Lark
Python SDK (`lark_oapi/ws/pb/pbbp2_pb2.py`, `ws/client.py`). No live-service
qualification is implied; callers retain strict message and memory bounds.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field

METHOD_CONTROL, METHOD_DATA = 0, 1


@dataclass
class Frame:
    seq_id: int = 0
    log_id: int = 0
    service: int = 0
    method: int = 0
    headers: dict = field(default_factory=dict)
    payload_encoding: str = ""
    payload_type: str = ""
    payload: bytes = b""
    log_id_new: bytes = b""


def _varint(n: int) -> bytes:
    out = bytearray()
    n &= (1 << 64) - 1
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    shift = value = 0
    while True:
        if i >= len(data) or shift > 63:
            raise ValueError("bad varint")
        b = data[i]; i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7


def _field(number: int, wire: int, body: bytes) -> bytes:
    return _varint(number << 3 | wire) + body


def _ld(number: int, data: bytes) -> bytes:
    return _field(number, 2, _varint(len(data)) + data)


def encode_frame(frame: Frame) -> bytes:
    out = _field(1, 0, _varint(frame.seq_id)) + _field(2, 0, _varint(frame.log_id)) + _field(3, 0, _varint(frame.service)) + _field(4, 0, _varint(frame.method))
    for key, value in frame.headers.items():
        out += _ld(5, _ld(1, key.encode()) + _ld(2, str(value).encode()))
    if frame.payload_encoding: out += _ld(6, frame.payload_encoding.encode())
    if frame.payload_type: out += _ld(7, frame.payload_type.encode())
    if frame.payload: out += _ld(8, frame.payload)
    if frame.log_id_new: out += _ld(9, frame.log_id_new.encode() if isinstance(frame.log_id_new, str) else frame.log_id_new)
    return out


def decode_frame(data: bytes, *, max_size: int = 4 << 20) -> Frame:
    if len(data) > max_size:
        raise ValueError("frame too large")
    frame, i = Frame(), 0
    while i < len(data):
        tag, i = _read_varint(data, i)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value, i = _read_varint(data, i)
            if number == 1: frame.seq_id = value
            elif number == 2: frame.log_id = value
            elif number == 3: frame.service = value
            elif number == 4: frame.method = value
        elif wire == 2:
            n, i = _read_varint(data, i)
            if i + n > len(data):
                raise ValueError("truncated field")
            chunk, i = data[i:i + n], i + n
            if number == 5:
                key = value = ""
                j = 0
                while j < len(chunk):
                    t, j = _read_varint(chunk, j)
                    m, j = _read_varint(chunk, j)
                    text = chunk[j:j + m].decode("utf-8", "replace"); j += m
                    if t >> 3 == 1: key = text
                    elif t >> 3 == 2: value = text
                frame.headers[key] = value
            elif number == 6: frame.payload_encoding = chunk.decode("utf-8", "replace")
            elif number == 7: frame.payload_type = chunk.decode("utf-8", "replace")
            elif number == 8: frame.payload = chunk
            elif number == 9: frame.log_id_new = chunk
        else:
            raise ValueError("unsupported wire type")
    return frame


def ping_frame(service: int) -> bytes:
    return encode_frame(Frame(service=service, method=METHOD_CONTROL, headers={"type": "ping"}))


def ack_frame(frame: Frame, code: int = 200) -> bytes:
    return encode_frame(Frame(seq_id=frame.seq_id, log_id=frame.log_id, service=frame.service, method=frame.method,
                              headers=dict(frame.headers), payload_encoding=frame.payload_encoding, payload_type=frame.payload_type,
                              log_id_new=frame.log_id_new, payload=json.dumps({"code": code}).encode()))


def service_id(url: str) -> int:
    from urllib.parse import parse_qs, urlsplit
    try:
        return int(parse_qs(urlsplit(url).query).get("service_id", ["0"])[0])
    except ValueError:
        return 0
