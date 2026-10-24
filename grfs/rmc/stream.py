"""Primitive encodings of the Quazal DDL, little-endian throughout.

string / stationurl : u16 length including the terminating zero, the bytes, the zero
buffer              : u32 length, the bytes
qbuffer             : u16 length, the bytes
lists               : u32 count, the items
datetime            : u64 packed sec | min<<6 | hour<<12 | day<<17 | month<<22 | year<<26
"""

import datetime as _dt
import struct


class ReadError(ValueError):
    pass


class Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise ReadError("need %d bytes at %d, have %d" % (n, self.pos, self.remaining()))
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def skip(self, n: int) -> None:
        self.take(n)

    def _unpack(self, fmt: str, size: int):
        return struct.unpack(fmt, self.take(size))[0]

    def u8(self): return self._unpack("<B", 1)
    def i8(self): return self._unpack("<b", 1)
    def u16(self): return self._unpack("<H", 2)
    def i16(self): return self._unpack("<h", 2)
    def u32(self): return self._unpack("<I", 4)
    def i32(self): return self._unpack("<i", 4)
    def u64(self): return self._unpack("<Q", 8)
    def i64(self): return self._unpack("<q", 8)
    def f32(self): return self._unpack("<f", 4)
    def f64(self): return self._unpack("<d", 8)

    def bool(self) -> bool:
        return self.u8() != 0

    def string(self) -> str:
        n = self.u16()
        raw = self.take(n)
        if raw.endswith(b"\x00"):
            raw = raw[:-1]
        return raw.decode("latin-1")

    def skip_string(self) -> None:
        """For fields whose content must never be looked at (see ddl.SENSITIVE)."""
        self.skip(self.u16())

    def buffer(self) -> bytes:
        return self.take(self.u32())

    def qbuffer(self) -> bytes:
        return self.take(self.u16())

    def rest(self) -> bytes:
        return self.take(self.remaining())


class Writer:
    __slots__ = ("parts",)

    def __init__(self):
        self.parts = []

    def raw(self, b: bytes) -> "Writer":
        self.parts.append(bytes(b))
        return self

    def _pack(self, fmt: str, v) -> "Writer":
        self.parts.append(struct.pack(fmt, v))
        return self

    def u8(self, v): return self._pack("<B", v & 0xFF)
    def i8(self, v): return self._pack("<b", v)
    def u16(self, v): return self._pack("<H", v & 0xFFFF)
    def i16(self, v): return self._pack("<h", v)
    def u32(self, v): return self._pack("<I", v & 0xFFFFFFFF)
    def i32(self, v): return self._pack("<i", v)
    def u64(self, v): return self._pack("<Q", v & 0xFFFFFFFFFFFFFFFF)
    def i64(self, v): return self._pack("<q", v)
    def f32(self, v): return self._pack("<f", v)
    def f64(self, v): return self._pack("<d", v)

    def bool(self, v) -> "Writer":
        return self.u8(1 if v else 0)

    def string(self, v) -> "Writer":
        b = ("" if v is None else str(v)).encode("latin-1", "replace") + b"\x00"
        return self.u16(len(b)).raw(b)

    def buffer(self, v: bytes) -> "Writer":
        return self.u32(len(v)).raw(v)

    def qbuffer(self, v: bytes) -> "Writer":
        return self.u16(len(v)).raw(v)

    def bytes(self) -> bytes:
        return b"".join(self.parts)


def pack_datetime(t: _dt.datetime = None) -> int:
    t = t or _dt.datetime.now(_dt.timezone.utc)
    return (t.second | t.minute << 6 | t.hour << 12 | t.day << 17 | t.month << 22
            | t.year << 26)


def unpack_datetime(v: int):
    try:
        return _dt.datetime(v >> 26, (v >> 22) & 0xF, (v >> 17) & 0x1F, (v >> 12) & 0x1F,
                            (v >> 6) & 0x3F, v & 0x3F)
    except ValueError:
        return None
