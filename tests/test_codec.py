"""The encodings underneath everything: primitives, RMC framing, packets, the schema codec, the
ticket and the NAT answer."""

import datetime
import os
import unittest
import zlib

from grfs.backend import Backend
from grfs.config import Config
from grfs.fakegame import read_ticket, url_address
from grfs.kerberos import (PASSWORD_DEFAULT, PASSWORD_TRACKING, TRACKING_NAME, Ticket,
                           new_blob, new_session_key, password_for)
from grfs.nat import ASK_PUBLIC_URL, KEEP_ALIVE, NatEcho
from grfs.prudp import packet as P
from grfs.prudp.crypto import PAYLOAD_KEY, derive_key, rc4
from grfs.rmc import message
from grfs.rmc.ddl import Schema, describe, parse_type
from grfs.rmc.stream import ReadError, Reader, Writer, pack_datetime, unpack_datetime


class Primitives(unittest.TestCase):
    def test_round_trip(self):
        w = (Writer().u8(0xFF).i8(-1).u16(0xBEEF).i16(-2).u32(0xDEADBEEF).i32(-3)
             .u64(2 ** 63).i64(-4).f32(1.5).f64(-2.25).bool(True).bool(0)
             .string("héllo").string(None).buffer(b"\x00\x01").qbuffer(b"xyz"))
        r = Reader(w.bytes())
        self.assertEqual([r.u8(), r.i8(), r.u16(), r.i16(), r.u32(), r.i32(), r.u64(), r.i64(),
                          r.f32(), r.f64(), r.bool(), r.bool(), r.string(), r.string(),
                          r.buffer(), r.qbuffer()],
                         [0xFF, -1, 0xBEEF, -2, 0xDEADBEEF, -3, 2 ** 63, -4, 1.5, -2.25, True,
                          False, "héllo", "", b"\x00\x01", b"xyz"])
        self.assertEqual(r.remaining(), 0)

    def test_strings_carry_their_terminator(self):
        self.assertEqual(Writer().string("ab").bytes(), b"\x03\x00ab\x00")
        self.assertEqual(Reader(b"\x02\x00ab").string(), "ab")   # one without is read too

    def test_reading_past_the_end(self):
        with self.assertRaises(ReadError):
            Reader(b"\x01").u32()
        with self.assertRaises(ReadError):
            Reader(b"\x05\x00\x00\x00ab").buffer()
        r = Reader(b"\x03\x00abc!")
        r.skip_string()
        self.assertEqual(r.rest(), b"!")

    def test_dates(self):
        t = datetime.datetime(2012, 5, 22, 13, 45, 7)
        self.assertEqual(unpack_datetime(pack_datetime(t)), t)
        self.assertIsNone(unpack_datetime(0))
        self.assertGreater(pack_datetime(), pack_datetime(t))


class Framing(unittest.TestCase):
    def test_a_long_protocol_id_takes_two_bytes(self):
        body = message.request(0x138B, 9, 3, b"p")
        self.assertEqual(body[4], 0xFF)
        m = message.parse(body)
        self.assertEqual((m.protocol, m.is_request, m.call_id, m.method, m.body),
                         (0x138B, True, 9, 3, b"p"))
        m = message.parse(message.response(0x138B, 9, 3, b"r"))
        self.assertEqual((m.protocol, m.is_request, m.success, m.method, m.body),
                         (0x138B, False, True, 3, b"r"))

    def test_an_error(self):
        m = message.parse(message.error(0x14, 7, 0x80010001))
        self.assertEqual((m.protocol, m.success, m.error, m.call_id), (0x14, False, 0x80010001, 7))

    def test_a_size_that_lies(self):
        with self.assertRaises(ReadError):
            message.parse(b"\x09\x00\x00\x00\x94")


class Packets(unittest.TestCase):
    def test_several_in_one_datagram(self):
        a = P.Packet(0x3F, 0x31, P.DATA, P.FLAG_HAS_SIZE | P.FLAG_ACK, 1, 2, 3, part=0,
                     payload=b"")
        b = P.Packet(0x3F, 0x31, P.PING, P.FLAG_HAS_SIZE | P.FLAG_NEED_ACK, 1, 2, 4,
                     payload=b"\x01\x02")
        got = P.parse(a.encode() + b.encode())
        self.assertEqual([(p.type, p.seq, p.payload) for p in got],
                         [(P.DATA, 3, b""), (P.PING, 4, b"\x01\x02")])

    def test_broken_ones(self):
        good = P.Packet(0x3F, 0x31, P.SYN, P.FLAG_NEED_ACK, 0, 0, 0).encode()
        for broken in (good[:8], P.Packet(0x3F, 0x31, P.PING, P.FLAG_HAS_SIZE, 0, 0, 0,
                                          payload=b"abc").encode()[:-3] + b"\x00"):
            with self.assertRaises(P.PacketError):
                P.parse(broken)

    def test_every_type_round_trips(self):
        for ptype in range(6):
            pkt = P.Packet(0x3F, 0x31, ptype, P.FLAG_RELIABLE, 0x52, 0x12345678, 77,
                           conn_sig=0xABCD if ptype in (P.SYN, P.CONNECT) else 0,
                           part=2 if ptype == P.DATA else 0, payload=b"xy")
            self.assertEqual(P.parse(pkt.encode()), [pkt])
            self.assertTrue(pkt.describe().startswith(P.TYPE_NAMES[ptype]))

    def test_the_checksum_seed_only_applies_to_the_secure_stream(self):
        self.assertEqual(P.checksum_seed(P.STREAM_RVSEC), 0xE3)
        self.assertEqual(P.checksum_seed(P.STREAM_RV), 0)

    def test_payloads_that_do_not_compress(self):
        data = os.urandom(300)
        wire = P.wrap_payload(P.STREAM_RVSEC, data)
        self.assertEqual(P.unwrap_payload(P.STREAM_RVSEC, wire), data)
        self.assertEqual(rc4(PAYLOAD_KEY, wire)[0], 1)

    def test_payloads_that_compress_too_well_go_plain(self):
        data = b"\x00" * 200000
        wire = P.wrap_payload(P.STREAM_RVSEC, data)
        self.assertEqual(rc4(PAYLOAD_KEY, wire)[0], 0)
        self.assertEqual(P.unwrap_payload(P.STREAM_RVSEC, wire), data)

    def test_other_streams(self):
        self.assertEqual(P.wrap_payload(P.STREAM_NATECHO, b"\xA5\x02"), b"\xA5\x02")
        self.assertEqual(P.unwrap_payload(P.STREAM_NAT, b"\xA5\x02"), b"\xA5\x02")
        plain = P.wrap_payload(P.STREAM_RV, b"hello" * 20)
        self.assertEqual(zlib.decompress(plain[1:]), b"hello" * 20)
        self.assertEqual(P.wrap_payload(P.STREAM_RVSEC, b""), b"")
        with self.assertRaises(P.PacketError):
            P.unwrap_payload(P.STREAM_RV, b"\x02not zlib")


class SchemaCodec(unittest.TestCase):
    def setUp(self):
        self.schema = Backend(config=Config(path=os.devnull), data_dir="").schema

    def test_a_map_from_a_dict_or_pairs(self):
        t = parse_type("std_map<uint32,string>")
        for value in ({1: "a", 2: "b"}, [(1, "a"), (2, "b")]):
            w = Writer()
            self.schema.write(w, t, value)
            self.assertEqual(self.schema.read(Reader(w.bytes()), t), [(1, "a"), (2, "b")])

    def test_an_unknown_class_in_an_any_is_carried_as_bytes(self):
        t = parse_type("any<Data,string>")
        w = Writer()
        self.schema.write(w, t, {"__class__": "SomethingNew", "__raw__": b"\x01\x02"})
        self.assertEqual(self.schema.read(Reader(w.bytes()), t),
                         {"__class__": "SomethingNew", "__raw__": b"\x01\x02"})
        w = Writer()
        self.schema.write(w, t, None)
        self.assertIsNone(self.schema.read(Reader(w.bytes()), t))

    def test_every_request_the_schema_declares_encodes_and_decodes(self):
        """Default values through every method there is, so a type the codec cannot handle shows
        here rather than when the game first calls it."""
        for proto in self.schema.protocols.values():
            for m in proto.methods.values():
                try:
                    data = self.schema.encode_request(m, {})
                    self.schema.decode_request(m, data)
                    self.schema.decode_response(m, self.schema.encode_response(m, {}))
                except Exception as e:
                    self.fail("%s.%s: %r" % (proto.name, m.signature(), e))

    def test_describe_is_safe_for_anything(self):
        text = describe({"a": b"\x00" * 9, 7: "pid-keyed", "__trailing__": b"x",
                         "__class__": "C", "l": list(range(20))})
        self.assertIn("<9 bytes>", text)
        self.assertIn("7='pid-keyed'", text)
        self.assertNotIn("__trailing__", text)
        self.assertIn("...20", text)
        self.assertTrue(describe("x" * 500).endswith("..."))

    def test_a_hand_declared_protocol(self):
        s = Schema()
        s.add_class("Pair", [("a", "uint32"), ("b", "string")])
        s.add_protocol("Mine", {1: ("Do", [("in", "Pair", "p"), ("out", "qlist<uint32>", "r")],
                                    "bool")})
        m = s.method("Mine", 1)
        self.assertEqual(s.decode_request(m, s.encode_request(m, {"p": {"a": 1, "b": "x"}})),
                         {"p": {"a": 1, "b": "x"}})
        self.assertEqual(s.decode_response(m, s.encode_response(m, {"r": [4, 5]})),
                         {"retval": True, "r": [4, 5]})
        self.assertIsNone(s.method("Mine", 2))
        self.assertIsNone(s.method("Nobody", 1))
        with self.assertRaises(KeyError):
            s.class_fields("Unknown")


class Tickets(unittest.TestCase):
    def test_a_ticket_opens_with_the_right_password_only(self):
        key = new_session_key()
        blob = new_blob()
        sealed = Ticket(0x1234, 0x1000, key, blob).encode(PASSWORD_DEFAULT)
        self.assertEqual(read_ticket(sealed, 0x1234, PASSWORD_DEFAULT), (key, 0x1000, blob))
        with self.assertRaises(ValueError):
            read_ticket(sealed, 0x1234, PASSWORD_TRACKING)
        with self.assertRaises(ValueError):
            read_ticket(sealed, 0x1235, PASSWORD_DEFAULT)

    def test_passwords(self):
        self.assertEqual(password_for(TRACKING_NAME), PASSWORD_TRACKING)
        self.assertEqual(password_for("anybody"), PASSWORD_DEFAULT)
        self.assertNotEqual(new_session_key(), new_session_key())
        self.assertEqual(len(derive_key(0x1234, b"x")), 16)
        self.assertNotEqual(derive_key(0x1234, b"x"), derive_key(0x1235, b"x"))

    def test_rc4_is_its_own_inverse(self):
        self.assertEqual(rc4(b"k", rc4(b"k", b"secret")), b"secret")


class NatAnswers(unittest.TestCase):
    def setUp(self):
        self.nat = NatEcho(Backend(config=Config(path=os.devnull), data_dir=""))

    def test_the_answer(self):
        data, url = self.nat.answer(bytes([0xA5, ASK_PUBLIC_URL]), ("10.0.0.1", 2346))
        self.assertEqual(url, "udp:/address=10.0.0.1;port=2346")
        self.assertEqual(data, b"\xA5\x02udp:/address=10.0.0.1;port=2346\x00")

    def test_what_is_not_answered(self):
        endpoint = ("10.0.0.1", 2346)
        self.assertEqual(self.nat.answer(b"\xA5", endpoint)[0], None)
        self.assertEqual(self.nat.answer(bytes([0xA5, KEEP_ALIVE]), endpoint),
                         (None, "keep-alive"))
        self.nat.enabled = False
        self.assertIsNone(self.nat.answer(bytes([0xA5, ASK_PUBLIC_URL]), endpoint)[0])

    def test_what_counts_as_a_bare_probe(self):
        self.assertTrue(self.nat.looks_like_probe(b"\xA5\x02"))
        self.assertTrue(self.nat.looks_like_probe(b"\xA5\x00\x02"))
        self.assertFalse(self.nat.looks_like_probe(b"\xA5"))
        self.assertFalse(self.nat.looks_like_probe(b"\x3F\x31\x02"))


class StationUrls(unittest.TestCase):
    def test_address_and_port(self):
        self.assertEqual(url_address("prudps:/address=1.2.3.4;port=21031;CID=1;PID=4096"),
                         ("1.2.3.4", 21031))
