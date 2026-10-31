"""RMC message framing.

    u32 size of everything after it
    u8  protocol id, | 0x80 for a request; 0x7F (0xFF for a request) means a u16 id follows
    request:   u32 call id, u32 method id, parameters
    response:  u8 1, u32 call id, u32 method id | 0x8000, results
               u8 0, u32 error code, u32 call id
"""

import struct
from dataclasses import dataclass

from .stream import Reader, ReadError


@dataclass
class Message:
    protocol: int
    is_request: bool
    call_id: int
    method: int = 0
    success: bool = True
    error: int = 0
    body: bytes = b""


def _proto_bytes(protocol: int, request: bool) -> bytes:
    if protocol < 0x7F:
        return bytes([protocol | (0x80 if request else 0)])
    return bytes([0xFF if request else 0x7F]) + struct.pack("<H", protocol)


def parse(data: bytes) -> Message:
    r = Reader(data)
    size = r.u32()
    if size != r.remaining():
        raise ReadError("RMC size %d but %d bytes follow" % (size, r.remaining()))
    b = r.u8()
    request = bool(b & 0x80)
    protocol = b & 0x7F
    if protocol == 0x7F:
        protocol = r.u16()
    if request:
        call_id, method = r.u32(), r.u32()
        return Message(protocol, True, call_id, method, body=r.rest())
    if r.u8() == 1:
        call_id, method = r.u32(), r.u32()
        return Message(protocol, False, call_id, method & ~0x8000, True, 0, r.rest())
    error, call_id = r.u32(), r.u32()
    return Message(protocol, False, call_id, 0, False, error, b"")


def _frame(body: bytes) -> bytes:
    return struct.pack("<I", len(body)) + body


def request(protocol: int, call_id: int, method: int, params: bytes) -> bytes:
    return _frame(_proto_bytes(protocol, True) + struct.pack("<II", call_id, method) + params)


def response(protocol: int, call_id: int, method: int, results: bytes) -> bytes:
    return _frame(_proto_bytes(protocol, False) + struct.pack("<BII", 1, call_id, method | 0x8000)
                  + results)


def error(protocol: int, call_id: int, code: int) -> bytes:
    return _frame(_proto_bytes(protocol, False) + struct.pack("<BII", 0, code, call_id))
