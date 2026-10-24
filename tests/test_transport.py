"""Transport behaviour that the login does not exercise: NAT probes and long messages."""

import os
import unittest

from grfs.backend import Backend
from grfs.config import Config
from grfs.nat import ASK_PUBLIC_URL, HEADER, KEEP_ALIVE
from grfs.net import PrudpServer
from grfs.prudp import packet as P
from grfs.rmc import message

from tests.test_login import FakeTransport, GameSide, PLAYER_VPORT, SERVER_VPORT


class NatEcho(unittest.TestCase):
    def setUp(self):
        self.backend = Backend(config=Config(path=os.devnull), data_dir="")
        self.server = PrudpServer(self.backend, "secure", self.backend.secure_port)
        self.server.transport = FakeTransport()

    def probe(self, kind, endpoint=("192.168.1.11", 2346)):
        """What the game's peer-to-peer socket sends: a type 5 packet, payload A5 and the kind."""
        pkt = P.Packet(0x71, 0x71, P.USER, 0, 0, 0, 0, payload=bytes([HEADER, kind]))
        self.server.datagram_received(pkt.encode(), endpoint)
        return self.server.transport.sent

    def test_the_answer_is_a_bare_datagram_with_the_address(self):
        sent = self.probe(ASK_PUBLIC_URL)
        self.assertEqual(len(sent), 1)
        data, endpoint = sent[0]
        self.assertEqual(endpoint, ("192.168.1.11", 2346))
        # Byte 0 must be the NAT transport header: the game's socket hands a datagram to its NAT
        # code only on that, and drops anything else as peer traffic.
        self.assertEqual(data[0], HEADER)
        self.assertEqual(data[1], ASK_PUBLIC_URL)
        self.assertEqual(data[2:], b"udp:/address=192.168.1.11;port=2346\x00")

    def test_a_probe_with_no_prudp_header_is_answered_too(self):
        self.server.datagram_received(bytes([HEADER, ASK_PUBLIC_URL]), ("10.0.0.5", 2346))
        data, _ = self.server.transport.sent[0]
        self.assertEqual(data[2:], b"udp:/address=10.0.0.5;port=2346\x00")

    def test_keep_alives_are_not_answered(self):
        self.assertEqual(self.probe(KEEP_ALIVE), [])
        self.assertEqual(self.backend.nat.keep_alives, 1)

    def test_an_unknown_kind_is_not_answered(self):
        self.assertEqual(self.probe(0x7F), [])

    def test_a_probe_needs_no_connection(self):
        """It comes from a socket that never handshaked, so it must not be looked up as one."""
        self.probe(ASK_PUBLIC_URL)
        self.assertEqual(len(self.backend.clients), 0)
        self.assertEqual(self.server.unplaced, set())


class LongMessages(unittest.TestCase):
    def setUp(self):
        self.backend = Backend(config=Config(path=os.devnull), data_dir="")
        self.server = PrudpServer(self.backend, "auth", self.backend.auth_port)
        self.server.transport = FakeTransport()
        self.game = GameSide(self.server, ("127.0.0.1", 2347))
        self.game.handshake()
        self.client = self.backend.clients.by_signature(self.game.server_signature)
        self.link = self.client.link(self.server.port, PLAYER_VPORT)

    def request_packet(self, seq=2):
        return P.Packet(PLAYER_VPORT, SERVER_VPORT, P.DATA,
                        P.FLAG_RELIABLE | P.FLAG_NEED_ACK, self.game.session,
                        self.game.server_signature, seq, part=0)

    def test_a_long_answer_is_split_and_comes_back_whole(self):
        body = message.response(0x2A, 7, 6, bytes(range(256)) * 12)   # ~3 KB
        self.server.transport.sent.clear()
        self.server.send_rmc(self.client, self.link, self.request_packet(), body)

        packets = [p for data, _ in self.server.transport.sent for p in P.parse(data)]
        self.assertGreater(len(packets), 1)
        # Reliable, so the client releases them in order; numbered from 1 for this connection;
        # the last one carries part 0.
        for pkt in packets:
            self.assertTrue(pkt.has(P.FLAG_RELIABLE), pkt.describe())
        self.assertEqual([p.seq for p in packets], list(range(1, len(packets) + 1)))
        self.assertEqual([p.part for p in packets],
                         list(range(1, len(packets))) + [0])
        rebuilt = b"".join(P.unwrap_payload(p.stream, p.payload) for p in packets)
        self.assertEqual(rebuilt, body)
        self.assertEqual(message.parse(rebuilt).body, bytes(range(256)) * 12)

    def test_the_reliable_numbering_starts_again_for_a_new_connection(self):
        body = message.response(0x2A, 7, 6, b"x" * 2000)
        self.server.send_rmc(self.client, self.link, self.request_packet(), body)
        self.assertGreater(self.link.reliable_seq, 1)
        # The game was restarted: a fresh CONNECT, and its receive window starts at 1 again.
        self.game.handshake()
        self.server.transport.sent.clear()
        self.server.send_rmc(self.client, self.link, self.request_packet(), body)
        first = P.parse(self.server.transport.sent[0][0])[0]
        self.assertEqual(first.seq, 1)

    def test_an_incoming_fragmented_request_is_reassembled(self):
        schema = self.backend.schema
        method = schema.method("SecureConnectionProtocol", 1)
        urls = ["prudp:/address=10.0.%d.%d;port=2347;sid=15" % (i // 250, i % 250)
                for i in range(60)]
        params = schema.encode_request(method, {"vecMyURLs": urls})
        body = message.request(0x0B, 11, 1, params)
        self.assertGreater(len(body), P.FRAGMENT_SIZE)

        # The player has to be signed in before registering, so do that first.
        login = schema.method("TicketGrantingProtocol", 2)
        self.game.call(0x0A, 2, schema.encode_request(login, {
            "strUserName": "fragments",
            "oExtraData": {"__class__": "UbiAuthenticationLoginCustomData",
                           "m_userName": "fragments", "m_onlineKey": "", "m_password": ""}}))

        chunks = [body[i:i + P.FRAGMENT_SIZE] for i in range(0, len(body), P.FRAGMENT_SIZE)]
        replies = []
        for n, chunk in enumerate(chunks, 1):
            self.game.seq += 1
            pkt = P.Packet(PLAYER_VPORT, SERVER_VPORT, P.DATA,
                           P.FLAG_RELIABLE | P.FLAG_NEED_ACK, self.game.session,
                           self.game.server_signature, self.game.seq,
                           part=0 if n == len(chunks) else n,
                           payload=P.wrap_payload(P.STREAM_RVSEC, chunk))
            replies = self.game.feed(pkt)
            # Every fragment is acknowledged on arrival, or the client resends it.
            self.assertTrue(any(p.has(P.FLAG_ACK) for p in replies),
                            "fragment %d was not acknowledged" % n)
        answers = [p for p in replies if not p.has(P.FLAG_ACK)]
        self.assertTrue(answers, "the reassembled request was never answered")
        reply = message.parse(b"".join(P.unwrap_payload(p.stream, p.payload) for p in answers))
        self.assertTrue(reply.success)
        self.assertEqual(self.client.urls, urls)


if __name__ == "__main__":
    unittest.main()
