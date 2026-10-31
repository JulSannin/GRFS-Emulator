"""PRUDP v0 packet framing: header, checksum, and the RC4 + zlib wrapping of payloads.

Wire layout (all integers little-endian):

    u8  source vport        high nibble = stream type, low nibble = port
    u8  destination vport
    u8  type | flags << 3
    u8  session id
    u32 signature           the receiver's connection signature
    u16 sequence id
    u32 connection signature    SYN and CONNECT only
    u8  part number             DATA only: 1, 2, 3 ... for fragments, 0 for the last/only one
    u16 payload size            only with FLAG_HAS_SIZE
    ... payload
    u8  checksum
"""

import math
import struct
import zlib
from dataclasses import dataclass

from .crypto import PAYLOAD_KEY, rc4

SYN, CONNECT, DATA, DISCONNECT, PING, USER = range(6)
TYPE_NAMES = {SYN: "SYN", CONNECT: "CONNECT", DATA: "DATA", DISCONNECT: "DISCONNECT",
              PING: "PING", USER: "USER"}

FLAG_ACK = 0x01
FLAG_RELIABLE = 0x02
FLAG_NEED_ACK = 0x04
FLAG_HAS_SIZE = 0x08

STREAM_DO = 1
STREAM_RV = 2
STREAM_RVSEC = 3
STREAM_NAT = 5
STREAM_NATECHO = 7

# Same access key as Ghost Recon Online; its byte sum seeds the checksum of RVSec packets.
ACCESS_KEY = b"8dtRv2oj"
RVSEC_SEED = sum(ACCESS_KEY) & 0xFF  # 0xE3

# Largest RMC chunk carried by one DATA packet; longer messages are split into fragments.
FRAGMENT_SIZE = 0x3C3


class PacketError(ValueError):
    pass


def stream_of(vport: int) -> int:
    return vport >> 4


def checksum_seed(stream_type: int) -> int:
    return RVSEC_SEED if stream_type == STREAM_RVSEC else 0


def checksum(data: bytes, seed: int) -> int:
    """Quazal::Buffer::ComputeCheckSum: bytes of the 32-bit word sum + the tail bytes + seed."""
    words = len(data) // 4
    total = sum(struct.unpack_from("<%dI" % words, data)) & 0xFFFFFFFF if words else 0
    result = (total & 0xFF) + ((total >> 8) & 0xFF) + ((total >> 16) & 0xFF) + (total >> 24)
    result += sum(data[words * 4:]) + seed
    return result & 0xFF


@dataclass
class Packet:
    src: int
    dst: int
    type: int
    flags: int
    session: int
    signature: int
    seq: int
    conn_sig: int = 0
    part: int = 0
    payload: bytes = b""  # as on the wire: still encrypted/compressed for DATA and CONNECT

    @property
    def stream(self) -> int:
        return stream_of(self.src)

    def has(self, flag: int) -> bool:
        return bool(self.flags & flag)

    def encode(self) -> bytes:
        out = bytearray(struct.pack("<BBBBIH", self.src, self.dst,
                                    (self.type & 7) | (self.flags << 3),
                                    self.session, self.signature & 0xFFFFFFFF, self.seq & 0xFFFF))
        if self.type in (SYN, CONNECT):
            out += struct.pack("<I", self.conn_sig & 0xFFFFFFFF)
        if self.type == DATA:
            out.append(self.part & 0xFF)
        if self.flags & FLAG_HAS_SIZE:
            out += struct.pack("<H", len(self.payload))
        out += self.payload
        out.append(checksum(bytes(out), checksum_seed(stream_of(self.src))))
        return bytes(out)

    def describe(self) -> str:
        names = "".join(c for f, c in ((FLAG_ACK, "A"), (FLAG_RELIABLE, "R"),
                                       (FLAG_NEED_ACK, "N"), (FLAG_HAS_SIZE, "S"))
                        if self.flags & f)
        text = "%s[%s] %02X->%02X s=%02X sig=%08X seq=%d" % (
            TYPE_NAMES.get(self.type, str(self.type)), names or "-", self.src, self.dst,
            self.session, self.signature, self.seq)
        if self.type == DATA:
            text += " part=%d" % self.part
        if self.type in (SYN, CONNECT):
            text += " csig=%08X" % self.conn_sig
        return text + " len=%d" % len(self.payload)


def parse(datagram: bytes) -> list:
    """All PRUDP packets in one UDP datagram (with FLAG_HAS_SIZE several can be packed)."""
    packets = []
    pos = 0
    end = len(datagram)
    while pos < end:
        if end - pos < 11:
            raise PacketError("truncated header")
        src, dst, tf, session, signature, seq = struct.unpack_from("<BBBBIH", datagram, pos)
        p = pos + 10
        ptype, flags = tf & 7, tf >> 3
        conn_sig = part = 0
        if ptype in (SYN, CONNECT):
            conn_sig, = struct.unpack_from("<I", datagram, p)
            p += 4
        if ptype == DATA:
            part = datagram[p]
            p += 1
        if flags & FLAG_HAS_SIZE:
            size, = struct.unpack_from("<H", datagram, p)
            p += 2
            if p + size + 1 > end:
                raise PacketError("payload size past the end")
        else:
            size = end - p - 1
            if size < 0:
                raise PacketError("no room for the checksum")
        payload = datagram[p:p + size]
        p += size
        expected = checksum(datagram[pos:p], checksum_seed(stream_of(src)))
        if datagram[p] != expected:
            raise PacketError("bad checksum %02X, expected %02X" % (datagram[p], expected))
        p += 1
        packets.append(Packet(src, dst, ptype, flags, session, signature, seq, conn_sig, part,
                              payload))
        pos = p
    return packets


def unwrap_payload(stream_type: int, raw: bytes) -> bytes:
    """Wire payload of a DATA or CONNECT packet -> plain bytes."""
    if not raw or stream_type in (STREAM_NAT, STREAM_NATECHO):
        return raw
    if stream_type == STREAM_RVSEC:
        raw = rc4(PAYLOAD_KEY, raw)
    if raw[0] == 0:
        return raw[1:]
    try:
        return zlib.decompress(raw[1:])
    except zlib.error as e:
        raise PacketError("payload does not inflate: %s" % e) from None


def wrap_payload(stream_type: int, data: bytes) -> bytes:
    """Plain bytes -> wire payload. Compressed like the original servers did, with the first
    byte telling the receiver how much room to make (ceil(plain / compressed))."""
    if not data or stream_type in (STREAM_NAT, STREAM_NATECHO):
        return data
    packed = zlib.compress(data)
    ratio = math.ceil(len(data) / len(packed))
    if ratio <= 255:
        body = bytes([max(ratio, 1)]) + packed
    else:
        body = b"\x00" + data
    if stream_type == STREAM_RVSEC:
        body = rc4(PAYLOAD_KEY, body)
    return body
