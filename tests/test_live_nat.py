"""NAT: the Storm type detector the game starts at launch, and the probe relay, on real sockets."""

import queue
import socket
import struct
import unittest

from grfs import punch

from tests.live import LiveTest

NAT = "NATTraversalProtocol"


class Detector:
    """The game's Storm punch client, as far as the wire goes: one socket, 13-byte requests with
    a sequence number and seven bytes of stack junk, 19-byte replies."""

    JUNK = bytes.fromhex("CC 5A A5 01 FF 00 7E")

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(2.0)
        self.port = self.sock.getsockname()[1]
        self.seq = 0

    def close(self):
        self.sock.close()

    def request(self, cmd: int) -> bytes:
        self.seq += 1
        return struct.pack(">HHBB", 13, cmd, self.seq, 0) + self.JUNK

    def ask(self, cmd: int, port: int):
        """Send one request; return it and the one reply."""
        data = self.request(cmd)
        self.sock.sendto(data, ("127.0.0.1", port))
        answer, _ = self.sock.recvfrom(64)
        return data, answer

    def nothing_more(self, wait: float = 0.15) -> bool:
        self.sock.settimeout(wait)
        try:
            self.sock.recvfrom(64)
            return False
        except (socket.timeout, ConnectionResetError):
            return True
        finally:
            self.sock.settimeout(2.0)


def expected(request: bytes, kind: int, port: int, ip: str = "127.0.0.1") -> bytes:
    """Built by hand, not with punch.reply, so a mistake there cannot hide here."""
    return (b"\x00\x13" + bytes([0, kind]) + request[4:13] + bytes(reversed(
        [int(x) for x in ip.split(".")])) + bytes([port >> 8, port & 0xFF]))


class OpenPath(LiveTest):
    def setUp(self):
        super().setUp()
        self.p = self.server.ports["punch"]
        self.game = Detector()
        self.addCleanup(self.game.close)

    def test_the_four_steps_to_open(self):
        """Filter1 -> Filter2 -> Filter3 -> Map1 -> Open, each reply byte for byte."""
        steps = [(punch.BINDING, self.p, 1), (punch.FROM_ANOTHER_SERVER, self.p, 3),
                 (punch.FROM_ANOTHER_PORT, self.p, 2), (punch.BINDING, self.p + 1, 1)]
        for cmd, port, kind in steps:
            request, answer = self.game.ask(cmd, port)
            self.assertEqual(len(answer), 19)
            self.assertEqual(answer, expected(request, kind, self.game.port), (cmd, port))
            # Exactly one reply: a second one is taken as an answer to a question not asked.
            self.assertTrue(self.game.nothing_more(), (cmd, port))
        self.assertTrue(self.wait_logged("its NAT type is Open (1 told so far)"))

    def test_a_symmetric_nat_is_told_the_port_it_had(self):
        """The last step comes from another source port. Reflecting it would send the client to
        its Strict branch; it is told the port step 1 saw instead."""
        request, answer = self.game.ask(punch.BINDING, self.p)
        self.assertEqual(answer, expected(request, 1, self.game.port))
        other = Detector()
        self.addCleanup(other.close)
        request, answer = other.ask(punch.BINDING, self.p + 1)
        self.assertEqual(answer, expected(request, 1, self.game.port))
        self.assertTrue(self.logged("a symmetric NAT"))

    def test_the_second_port_alone_reflects_what_it_sees(self):
        request, answer = self.game.ask(punch.BINDING, self.p + 1)
        self.assertEqual(answer, expected(request, 1, self.game.port))

    def test_anything_else_gets_no_reply(self):
        for junk in (b"hello", b"\x00\x0D\x00\x09" + bytes(9), b"\x00\x0E\x00\x01" + bytes(10),
                     bytes(12)):
            self.game.sock.sendto(junk, ("127.0.0.1", self.p))
            self.assertTrue(self.game.nothing_more(), junk)
        # And the responder still works afterwards.
        request, answer = self.game.ask(punch.BINDING, self.p)
        self.assertEqual(answer, expected(request, 1, self.game.port))
        self.assertTrue(self.logged("not a detect request"))

    def test_the_configuration_names_both_ports(self):
        values = self.server.config_service(every=True)["punch_DetectUrls"]
        self.assertEqual(values, ["127.0.0.1:%d" % self.p, "127.0.0.1:%d" % (self.p + 1)])
        # What the client does with each: split at the first colon, atoi the rest.
        for value in values:
            host, _, port = value.partition(":")
            socket.inet_aton(host)
            self.assertIn(int(port), (self.p, self.p + 1))
        others = {k: v for k, v in self.server.config_service(every=True).items()
                  if k != "punch_DetectUrls"}
        self.assertTrue(all(len(v) == 1 for v in others.values()))


class SwitchedOff(LiveTest):
    settings = {"punch_port": 0}

    def test_no_detector_and_no_key(self):
        self.assertEqual(self.server.ports["punch"], 0)
        self.assertNotIn("punch_DetectUrls", self.server.config_service(every=True))


class Replies(unittest.TestCase):
    """punch.reply and punch.command_of without the sockets."""

    def test_the_layout(self):
        request = struct.pack(">HHBB", 13, 2, 9, 0) + bytes(range(7))
        self.assertEqual(punch.command_of(request), 2)
        self.assertEqual(punch.reply(request, 2, "10.1.2.3", 0x1234),
                         bytes.fromhex("0013 0002 09 00 00010203040506 0302010A 1234"))
        self.assertIsNone(punch.command_of(request[:12]))
        self.assertIsNone(punch.command_of(struct.pack(">HH", 13, 4) + bytes(9)))


class Memory(unittest.TestCase):
    """What the responder remembers for the symmetric-NAT case, without the sockets."""

    def setUp(self):
        self.responder = punch.PunchResponder(backend=None)
        self.request = struct.pack(">HHBB", 13, punch.BINDING, 1, 0) + bytes(7)

    def test_an_address_that_never_comes_back_is_forgotten(self):
        self.responder.answer(self.request, ("198.51.100.1", 4000))
        self.responder.answer(self.request, ("198.51.100.2", 4000))
        self.assertEqual(set(self.responder._seen), {"198.51.100.1", "198.51.100.2"})
        # A minute and more later, a request from anyone else sweeps both away.
        long_ago = punch.REMEMBER + 1
        for seen in self.responder._seen.values():
            for port in seen:
                seen[port] -= long_ago
        self.responder._swept -= long_ago
        self.responder.answer(self.request, ("203.0.113.5", 5000))
        self.assertEqual(set(self.responder._seen), {"203.0.113.5"})

    def test_a_late_last_step_is_answered_with_what_it_shows(self):
        """Past the minute the first port is gone, and P+1 reflects the source as it is."""
        self.responder.answer(self.request, ("198.51.100.1", 4000))
        self.responder._seen["198.51.100.1"][4000] -= punch.REMEMBER + 1
        answer, _ = self.responder.answer(self.request, ("198.51.100.1", 4100), second=True)
        self.assertEqual(answer[-2:], struct.pack(">H", 4100))
        self.assertEqual(self.responder._seen, {})


class ProbeRelay(LiveTest):
    def setUp(self):
        super().setUp()
        self.alice = self.player("alice", p2p_port=3001)
        self.bob = self.player("bob", p2p_port=3002)

    def test_the_target_is_told_to_probe_the_asker(self):
        target = "prudp:/address=127.0.0.1;port=3002;RVCID=%d" % self.bob.rvcid
        self.assertEqual(self.alice.call(NAT, "RequestProbeInitiation",
                                         {"urlTargetList": [target]}), {})
        protocol, method, args = self.bob.next_server_call()
        self.assertEqual((protocol, method), (NAT, "InitiateProbe"))
        # Where alice's probes come from - her secure connection's socket - in the URL she
        # registered, with her RVCID.
        self.assertEqual(args["urlStationToProbe"],
                         "prudp:/address=127.0.0.1;port=%d;sid=15;type=2;RVCID=%d"
                         % (self.alice.wire.port, self.alice.rvcid))
        with self.assertRaises(queue.Empty):
            self.alice.next_server_call(timeout=0.2)

    def test_ext_passes_the_given_url_on(self):
        mine = "udp:/address=203.0.113.9;port=2346;RVCID=%d" % self.alice.rvcid
        # No RVCID in the target: found by the address and port bob registered.
        self.alice.call(NAT, "RequestProbeInitiationExt", {
            "urlTargetList": ["udp:/address=127.0.0.1;port=3002"], "urlStationToProbe": mine})
        self.assertEqual(self.bob.next_server_call(), (NAT, "InitiateProbe",
                                                       {"urlStationToProbe": mine}))

    def test_a_target_found_by_where_it_reaches_us_from(self):
        self.alice.call(NAT, "RequestProbeInitiation", {"urlTargetList": [
            "prudp:/address=127.0.0.1;port=%d" % self.bob.wire.port]})
        self.assertEqual(self.bob.next_server_call()[1], "InitiateProbe")

    def test_one_probe_per_target_and_none_to_the_asker(self):
        carol = self.player("carol", p2p_port=3003)
        self.alice.call(NAT, "RequestProbeInitiation", {"urlTargetList": [
            "prudp:/address=127.0.0.1;port=3002;RVCID=%d" % self.bob.rvcid,
            "prudp:/address=127.0.0.1;port=3002",
            "prudp:/address=127.0.0.1;port=3001;RVCID=%d" % self.alice.rvcid,
            "prudp:/address=127.0.0.1;port=3003;PID=%d" % carol.pid]})
        self.assertEqual(self.bob.next_server_call()[1], "InitiateProbe")
        self.assertEqual(carol.next_server_call()[1], "InitiateProbe")
        for player in (self.alice, self.bob, carol):
            with self.assertRaises(queue.Empty):
                player.next_server_call(timeout=0.2)
        # Her own URL is skipped, not reported as a station that is not here.
        self.assertTrue(self.logged("bob, carol told to probe"))
        self.assertFalse(self.logged("not here"))

    def test_a_target_that_is_not_here_is_still_success(self):
        self.assertEqual(self.alice.call(NAT, "RequestProbeInitiation", {"urlTargetList": [
            "prudp:/address=198.51.100.1;port=9;RVCID=9999"]}), {})
        self.assertTrue(self.logged("not here: prudp:/address=198.51.100.1"))
        with self.assertRaises(queue.Empty):
            self.bob.next_server_call(timeout=0.2)

    def test_initiate_probe_from_a_client_is_success(self):
        self.assertEqual(self.alice.call(NAT, "InitiateProbe", {
            "urlStationToProbe": "prudp:/address=127.0.0.1;port=1"}), {})
