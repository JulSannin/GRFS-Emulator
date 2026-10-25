"""From a reassembled message to a handler and back: the router's rules, and putting fragments
back together in whatever order they come."""

import os
import unittest

from grfs.backend import Backend
from grfs.config import Config
from grfs.net import FRAGMENTS_KEPT, PrudpServer
from grfs.prudp import packet as P
from grfs.protocols import FRIENDS
from grfs.rmc import message
from grfs.rmc.router import RAW, Errors, RmcError, Service, _call

from tests.test_login import PLAYER_VPORT, SERVER_VPORT, FakeTransport, GameSide


class Rigged(Service):
    """FriendsProtocol, answered wrongly in every way a handler can be wrong."""

    protocol_id = FRIENDS
    protocol_name = "FriendsProtocol"

    def __init__(self, backend):
        super().__init__(backend)
        self.seen = []

    def GetList(self, ctx, byRelationship):
        raise RmcError(Errors.CORE_ACCESS_DENIED, "not you")

    def GetDetailedList(self, ctx, byRelationship):
        raise ValueError("a bug in a handler")

    def GetRelationships(self, ctx):
        return {"lstRelationshipsList": 5}             # not a list: cannot be encoded

    def AddFriend(self, ctx, **everything):
        self.seen.append(sorted(everything))
        return {"retval": True}

    def AddFriendByName(self, ctx, strPlayerName):
        self.seen.append(strPlayerName)
        return {"retval": True}

    def ClearRelationship(self, ctx):
        return ctx.reply_raw(b"\x07")

    def AcceptFriendship(self, ctx):
        return None


class Routing(unittest.TestCase):
    def setUp(self):
        self.backend = Backend(config=Config(path=os.devnull), data_dir="")
        self.service = self.backend.router.add(Rigged(self.backend))
        self.server = PrudpServer(self.backend, "secure", self.backend.secure_port)
        self.server.transport = FakeTransport()
        self.game = GameSide(self.server, ("127.0.0.1", 4000))
        self.game.handshake()
        self.schema = self.backend.schema

    def call(self, method, args=None, extra=b""):
        mdef = self.schema.method("FriendsProtocol", method)
        _, reply, _ = self.game.call(FRIENDS, method,
                                     self.schema.encode_request(mdef, args or {}) + extra)
        return reply

    def test_an_rmc_error_is_answered_with_its_code(self):
        reply = self.call(11, {"byRelationship": 1})
        self.assertEqual((reply.success, reply.error), (False, Errors.CORE_ACCESS_DENIED))

    def test_a_handler_that_raises_is_answered_with_an_error(self):
        with self.assertLogs("grfs.backend", "ERROR"):
            reply = self.call(12, {"byRelationship": 1})
        self.assertEqual((reply.success, reply.error), (False, Errors.CORE_UNKNOWN))

    def test_a_result_that_cannot_be_encoded_is_answered_with_an_error(self):
        with self.assertLogs("grfs.backend", "ERROR"):
            reply = self.call(13, {"resultRange": {}})
        self.assertEqual((reply.success, reply.error), (False, Errors.CORE_UNKNOWN))

    def test_a_handler_gets_only_what_it_asks_for(self):
        self.assertTrue(self.call(2, {"strPlayerName": "bob"}).success)
        self.assertTrue(self.call(1, {"uiPlayer": 5}).success)
        self.assertEqual(self.service.seen, ["bob", ["strMessage", "uiDetails", "uiPlayer"]])

    def test_a_raw_reply_goes_out_as_it_was_built(self):
        reply = self.call(9, {"uiPlayer": 5})
        self.assertTrue(reply.success)
        self.assertEqual((reply.method, reply.body), (9, b"\x07"))

    def test_nothing_returned_is_an_empty_success(self):
        reply = self.call(5, {"uiPlayer": 5})
        self.assertTrue(reply.success)
        self.assertEqual(reply.body, b"\x01")           # the declared bool, true by default

    def test_bytes_past_the_parameters_are_noted_and_ignored(self):
        with self.assertLogs("grfs.backend", "WARNING") as logged:
            reply = self.call(2, {"strPlayerName": "bob"}, extra=b"\xAA\xBB")
        self.assertTrue(reply.success)
        self.assertTrue(any("past the end" in line for line in logged.output))

    def test_a_failure_the_router_does_not_catch_is_still_answered(self):
        def broken(*args):
            raise RuntimeError("the router itself")
        self.backend.router.dispatch = broken
        with self.assertLogs("grfs.secure", "ERROR"):
            reply = self.call(2, {"strPlayerName": "bob"})
        self.assertEqual((reply.success, reply.error), (False, Errors.CORE_UNKNOWN))

    def test_handled_lists_what_is_answered(self):
        self.assertEqual([name for _, name in self.service.handled()],
                         ["AddFriend", "AddFriendByName", "AcceptFriendship",
                          "ClearRelationship", "GetList", "GetDetailedList",
                          "GetRelationships"])

    def test_call_keywords(self):
        def plain(ctx, a, b=2):
            return (a, b)

        def keyword_only(ctx, *, a):
            return a
        self.assertEqual(_call(plain, None, {"a": 1, "zzz": 9}), (1, 2))
        self.assertEqual(_call(keyword_only, None, {"a": 3, "b": 4}), 3)
        self.assertIsNot(RAW, None)


class Fragments(unittest.TestCase):
    """Fragments fed straight to the server in the orders a bad network produces."""

    def setUp(self):
        self.backend = Backend(config=Config(path=os.devnull), data_dir="")
        self.server = PrudpServer(self.backend, "secure", self.backend.secure_port)
        self.server.transport = FakeTransport()
        self.game = GameSide(self.server, ("127.0.0.1", 4000))
        self.game.handshake()
        self.handled = []
        self.backend.router.dispatch = lambda server, client, link, pkt, msg: \
            self.handled.append(msg)

    def message(self, size, call_id=1):
        return message.request(FRIENDS, call_id, 2, bytes(n & 0xFF for n in range(size)))

    def packets(self, body, first_seq):
        chunks = [body[i:i + P.FRAGMENT_SIZE] for i in range(0, len(body), P.FRAGMENT_SIZE)]
        return [P.Packet(PLAYER_VPORT, SERVER_VPORT, P.DATA, P.FLAG_RELIABLE | P.FLAG_NEED_ACK,
                         self.game.session, self.game.server_signature, first_seq + n,
                         part=0 if n == len(chunks) - 1 else n + 1,
                         payload=P.wrap_payload(P.STREAM_RVSEC, chunk))
                for n, chunk in enumerate(chunks)]

    def feed(self, pkts, order):
        for i in order:
            self.server.datagram_received(pkts[i].encode(), self.game.endpoint)

    def test_in_order(self):
        body = self.message(3000)
        pkts = self.packets(body, 10)
        self.assertEqual(len(pkts), 4)
        self.feed(pkts, [0, 1, 2, 3])
        self.assertEqual([m.body for m in self.handled], [message.parse(body).body])

    def test_every_order_comes_out_whole(self):
        import itertools
        body = self.message(2200)
        for n, order in enumerate(itertools.permutations(range(3))):
            self.handled.clear()
            self.feed(self.packets(body, 100 + 10 * n), order)
            self.assertEqual(len(self.handled), 1, order)
            self.assertEqual(self.handled[0].body, message.parse(body).body, order)
        self.assertEqual(self.server_link().fragments, {})

    def test_a_lost_middle_fragment_resent_last(self):
        body = self.message(3000)
        pkts = self.packets(body, 10)
        self.feed(pkts, [0, 2, 3])
        self.assertEqual(self.handled, [])
        self.feed(pkts, [1])
        self.assertEqual(len(self.handled), 1)

    def test_a_single_message_while_another_waits_for_a_fragment(self):
        long_one = self.packets(self.message(2000, call_id=1), 10)          # 10, 11, 12
        short_one = self.packets(self.message(20, call_id=2), 13)
        self.feed(long_one, [0, 2])
        self.feed(short_one, [0])
        self.assertEqual([m.call_id for m in self.handled], [2])
        self.feed(long_one, [1])
        self.assertEqual([m.call_id for m in self.handled], [2, 1])

    def test_garbage_is_held_and_then_forgotten(self):
        junk = P.Packet(PLAYER_VPORT, SERVER_VPORT, P.DATA, P.FLAG_RELIABLE | P.FLAG_NEED_ACK,
                        self.game.session, self.game.server_signature, 0,
                        payload=P.wrap_payload(P.STREAM_RVSEC, b"not a message"))
        with self.assertLogs("grfs.secure", "WARNING"):
            for seq in range(10, 10 + 2 * (FRAGMENTS_KEPT + 5), 2):    # never side by side
                junk.seq = seq
                self.server.datagram_received(junk.encode(), self.game.endpoint)
        self.assertLessEqual(len(self.server_link().fragments), FRAGMENTS_KEPT)
        self.assertEqual(self.handled, [])
        # A real message still gets through.
        self.feed(self.packets(self.message(20), 500), [0])
        self.assertEqual(len(self.handled), 1)

    def test_parts_that_do_not_count_down_are_not_joined(self):
        body = self.message(3000)
        pkts = self.packets(body, 10)
        pkts[1].part = 3                                          # 1, 3, 3, 0
        self.feed(pkts, [0, 1, 2, 3])
        self.assertEqual(self.handled, [])

    def server_link(self):
        client = self.backend.clients.by_signature(self.game.server_signature)
        return client.find_link(self.backend.secure_port, PLAYER_VPORT)
