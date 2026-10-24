"""Packet codec checked against real traffic published in the Ghost Recon Online backend wiki.

GRO runs on the same Yeti/Quazal stack with the same access key, so its captures are exact
test vectors for the checksum, the RC4/zlib wrapping and the header layout.
"""

import unittest

from grfs.prudp import packet as P


def hx(text: str) -> bytes:
    return bytes.fromhex(text)


class ChecksumAndHeader(unittest.TestCase):
    def test_client_syn_parses(self):
        pkts = P.parse(hx("3F 31 20 00 00 00 00 00 00 00 00 00 00 00 73"))
        self.assertEqual(len(pkts), 1)
        p = pkts[0]
        self.assertEqual((p.type, p.flags, p.src, p.dst), (P.SYN, P.FLAG_NEED_ACK, 0x3F, 0x31))

    def test_server_replies_encode_byte_for_byte(self):
        cases = [
            (P.Packet(0x31, 0x3F, P.SYN, P.FLAG_ACK, 0x00, 0, 0, conn_sig=0x12345678),
             "31 3F 08 00 00 00 00 00 00 00 78 56 34 12 6F"),
            (P.Packet(0x31, 0x3F, P.CONNECT, P.FLAG_ACK, 0x1F, 0x7F260001, 1, conn_sig=0x12345678),
             "31 3F 09 1F 01 00 26 7F 01 00 78 56 34 12 36"),
            (P.Packet(0x31, 0x3F, P.DATA, P.FLAG_ACK, 0x52, 0x7F260001, 2, part=0),
             "31 3F 0A 52 01 00 26 7F 02 00 00 57"),
            (P.Packet(0x31, 0x3F, P.PING, P.FLAG_ACK, 0x56, 0x7F260001, 1),
             "31 3F 0C 56 01 00 26 7F 01 00 5C"),
            (P.Packet(0x31, 0x3F, P.DISCONNECT, P.FLAG_ACK, 0x6A, 0x7F260001, 4),
             "31 3F 0B 6A 01 00 26 7F 04 00 72"),
        ]
        for pkt, wire in cases:
            self.assertEqual(pkt.encode(), hx(wire), P.TYPE_NAMES[pkt.type])

    def test_bad_checksum_is_rejected(self):
        with self.assertRaises(P.PacketError):
            P.parse(hx("3F 31 20 00 00 00 00 00 00 00 00 00 00 00 74"))


class Payloads(unittest.TestCase):
    def test_client_request_ticket_unwraps(self):
        pkt, = P.parse(hx("3F 31 32 52 78 56 34 12 03 00 00 0F 96 44 DB 13 75 21 50 05 A2 62"
                          " FD 2A 16 C6 A3 FF 56 E6 E5 97 56 6F"))
        self.assertTrue(pkt.has(P.FLAG_RELIABLE) and pkt.has(P.FLAG_NEED_ACK))
        self.assertEqual(P.unwrap_payload(pkt.stream, pkt.payload),
                         hx("11 00 00 00 8A 0D 00 00 00 03 00 00 00 34 12 00 00 00 10 00 00"))

    def test_server_ticket_response_unwraps(self):
        wire = hx(
            "31 3F 22 52 01 00 26 7F 04 00 00 0F D9 44 DB 13 F5 2D 5D 05 A2 61 FE AA 16 F2 B0 FF"
            " 57 E6 B9 97 56 A2 AB 69 39 89 EF F8 1B 5D 0B 83 B0 F3 B3 2B F4 BF 5C F9 9E E9 65 CF"
            " FB 51 20 F6 29 57 C1 8E C6 32 40 F5 33 E2 C7 02 99 A4 82 4E D9 FF 16 BB 44 63 9D B3"
            " 29 5E 27 0C 52 59 5E 2D 1C 91 83 79 91 3E 25 14 E7 97 04 F9 B6 7E 8A 98 42 2B E9")
        pkt, = P.parse(wire)
        plain = P.unwrap_payload(pkt.stream, pkt.payload)
        self.assertEqual(plain[:22], hx("5E 00 00 00 0A 01 0D 00 00 00 03 80 00 00 01 00 01 00"
                                        " 4C 00 00 00"))
        self.assertEqual(len(plain), 0x5E + 4)

    def test_wrap_round_trip(self):
        data = bytes(range(256)) * 5
        for stream in (P.STREAM_RVSEC, P.STREAM_DO):
            self.assertEqual(P.unwrap_payload(stream, P.wrap_payload(stream, data)), data)


if __name__ == "__main__":
    unittest.main()
