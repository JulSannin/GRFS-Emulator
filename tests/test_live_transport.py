"""The transport against a running server: loss, duplicates, fragments, restarts, NAT, crowds.

Loss is simulated on the client's side of the socket (Wire.drop_in / drop_out), so the server
sees exactly what it would see on a bad network - a packet that never arrived.
"""

import socket
import threading
import time

from grfs.fakegame import SERVER_VPORT, Connection, Peer, find_method, schema
from grfs.nat import KEEP_ALIVE
from grfs.prudp import packet as P
from grfs.protocols import NOTIFICATION, UBI_ACCOUNT_MANAGEMENT
from grfs.rmc import message

from tests.live import LiveTest


def once(predicate):
    """A drop rule that loses the first packet matching `predicate` and nothing after it."""
    state = {"done": False}

    def rule(pkt):
        if not state["done"] and predicate(pkt):
            state["done"] = True
            return True
        return False
    return rule


def answer(pkt):
    return pkt.type == P.DATA and not pkt.has(P.FLAG_ACK)


def ack(pkt):
    return pkt.type == P.DATA and pkt.has(P.FLAG_ACK)


class Loss(LiveTest):
    def search(self, player, text="bo"):
        return player.call("SearchFriendsProtocol", "SearchFriends", {"partOfName": text})

    def test_a_lost_request_is_resent_and_answered(self):
        alice = self.player("alice")
        self.player("bob")
        alice.wire.drop_out.append(once(answer))
        self.assertEqual([f["m_strName"] for f in self.search(alice)["foundList"]], ["bob"])
        self.assertEqual(alice.wire.dropped_out, 1)

    def test_a_lost_answer_is_resent_from_the_cache_not_handled_again(self):
        alice = self.player("alice")
        self.player("bob")
        # Both the acknowledgement and the answer are lost, so the request goes again.
        alice.wire.drop_in.append(once(ack))
        alice.wire.drop_in.append(once(answer))
        before = self.count_logged("SearchFriendsProtocol.SearchFriends call=")
        self.assertEqual([f["m_strName"] for f in self.search(alice)["foundList"]], ["bob"])
        self.assertEqual(self.count_logged("SearchFriendsProtocol.SearchFriends call="),
                         before + 1)
        self.assertTrue(self.logged("again - resending"))

    def test_a_repeated_request_gets_the_same_bytes(self):
        alice = self.player("alice")
        mdef = find_method(schema(), "SearchFriendsProtocol", "SearchFriends")
        body = message.request(0x56, 777, mdef.id,
                               schema().encode_request(mdef, {"partOfName": "a"}))
        packets = alice.secure.send_message(body)
        self._wait_for(lambda: 777 in alice.secure.responses)
        mark = len(alice.wire.history)
        for pkt in packets:
            alice.wire.send(pkt, alice.secure.endpoint)
        self._wait_for(lambda: len(alice.wire.history) >= mark + 2)
        first = [d for _, d in alice.wire.history[:mark]][-2:]
        again = [d for _, d in alice.wire.history[mark:mark + 2]]
        self.assertEqual(again, first)
        self.assertEqual(self.count_logged("SearchFriendsProtocol.SearchFriends call=777"), 1)

    def test_a_lost_fragment_of_a_long_answer_arrives_when_resent(self):
        alice = self.player("alice")
        # The second fragment of the privileges list goes missing on its way in, and the
        # acknowledgement of the request with it, so the game asks again.
        alice.wire.drop_in.append(once(lambda p: answer(p) and p.part == 2))
        alice.wire.drop_in.append(once(ack))
        rows = alice.call("PrivilegesProtocol", "GetPrivilegesEx", {"localeCode": "en"})
        self.assertTrue(rows["privilegesEx"])
        self.assertFalse(alice.secure.stuck())

    def test_a_lost_fragment_of_a_long_request_is_filled_in_when_resent(self):
        alice = self.player("alice")
        self.player("bob")
        ids = ["nobody-%04d" % n for n in range(300)] + ["bob"]
        alice.wire.drop_out.append(once(lambda p: answer(p) and p.part == 2))
        found = alice.call("UbiAccountManagementProtocol", "LookupUsernamesByUbiAccountIDs",
                           {"UbiAccountIds": ids})
        self.assertEqual(dict(found["Usernames"]), {"bob": "bob"})

    def test_a_long_request_is_put_back_together(self):
        alice = self.player("alice")
        self.player("bob")
        ids = ["nobody-%04d" % n for n in range(300)] + ["bob"]
        found = alice.call("UbiAccountManagementProtocol", "LookupUsernamesByUbiAccountIDs",
                           {"UbiAccountIds": ids})
        self.assertEqual(dict(found["Usernames"]), {"bob": "bob"})
        self.assertTrue(self.logged("reassembled"))

    def _wait_for(self, condition, timeout=5):
        done = threading.Event()
        for _ in range(int(timeout / 0.02)):
            if condition():
                return
            done.wait(0.02)
        self.fail("timed out")


class Connections(LiveTest):
    def test_a_ping_is_answered(self):
        alice = self.player("alice")
        alice.secure.ping()
        alice.auth.ping()
        self.assertEqual(alice.secure.pings, 1)

    def test_a_ping_that_is_itself_an_answer_is_not_answered(self):
        alice = self.player("alice")
        mark = len(alice.wire.history)
        alice.wire.send(P.Packet(0x3F, 0x31, P.PING, P.FLAG_ACK, alice.secure.session,
                                 alice.secure.server_signature, 99), alice.secure.endpoint)
        alice.secure.ping()       # something that is answered, to know the first was seen
        self.assertEqual(len(alice.wire.history), mark + 1)

    def test_disconnecting_keeps_the_player(self):
        alice = self.player("alice")
        alice.secure.disconnect()
        self.assertIsNotNone(self.client_of(alice))
        self.assertTrue(self.logged("(kept)"))
        # The game reconnects seconds later and carries on as the same player.
        alice.secure.handshake()
        alice.secure.connect_with_ticket(alice.ticket, alice.session_key)
        self.assertEqual(alice.call("SearchFriendsProtocol", "SearchFriends",
                                    {"partOfName": "zzz"}), {"foundList": []})

    def test_a_handshake_again_starts_the_numbering_again(self):
        alice = self.player("alice")
        for _ in range(3):
            self.assertTrue(alice.call("PrivilegesProtocol", "GetPrivilegesEx",
                                       {"localeCode": "en"})["privilegesEx"])
            alice.secure.handshake()
            code, got = 7, alice.secure.connect_with_ticket(alice.ticket, alice.session_key, 7)
            self.assertEqual(got, code + 1)
        self.assertTrue(alice.call("PrivilegesProtocol", "GetPrivilegesEx",
                                   {"localeCode": "en"})["privilegesEx"])

    def test_two_connections_on_one_socket_are_kept_apart(self):
        alice = self.player("alice")
        alice.login_telemetry()
        self.assertTrue(alice.call("PrivilegesProtocol", "GetPrivilegesEx", {"localeCode": "en"}))
        mdef = find_method(schema(), "PrivilegesProtocol", "GetPrivilegesEx")
        reply = alice.telemetry.call(0x23, mdef.id, schema().encode_request(mdef, {}))
        self.assertTrue(reply.success)
        self.assertEqual(alice.telemetry.bad_signatures, 0)


class Strangers(LiveTest):
    def test_garbage_does_not_take_the_server_down(self):
        wire = self.server.wire()
        for port in (self.server.ports["auth"], self.server.ports["secure"]):
            for junk in (b"", b"\x00", b"\xff" * 5, b"hello world, this is not prudp",
                         P.Packet(0x3F, 0x31, P.DATA, 0, 0, 0, 1).encode()[:-1] + b"\x00",
                         bytes(range(256))):
                wire.send_bytes(junk, ("127.0.0.1", port))
        self.assertTrue(self.player("alice").pid)

    def test_a_packet_for_no_connection_is_reported_once(self):
        wire = self.server.wire()
        for seq in range(5):
            wire.send(P.Packet(0x3F, 0x31, P.DATA, P.FLAG_RELIABLE | P.FLAG_NEED_ACK, 0x52,
                               0xDEADBEEF, seq + 2, payload=P.wrap_payload(3, b"x" * 10)),
                      ("127.0.0.1", self.server.ports["secure"]))
        alice = self.player("alice")         # by now everything above has been read
        self.assertEqual(self.count_logged("belongs to no connection"), 1)
        self.assertTrue(alice.pid)

    def test_a_payload_that_does_not_decrypt_is_shrugged_off(self):
        alice = self.player("alice")
        alice.wire.send(P.Packet(0x3F, 0x31, P.DATA, P.FLAG_RELIABLE | P.FLAG_NEED_ACK,
                                 alice.secure.session, alice.secure.server_signature,
                                 alice.secure.next_seq(), payload=b"\x01\x02\x03\x04"),
                        alice.secure.endpoint)
        alice.wire.send(P.Packet(0x3F, 0x31, P.DATA, P.FLAG_RELIABLE | P.FLAG_NEED_ACK,
                                 alice.secure.session, alice.secure.server_signature,
                                 alice.secure.next_seq(),
                                 payload=P.wrap_payload(3, b"\x05\x00\x00\x00junk!")),
                        alice.secure.endpoint)
        self.assertEqual(alice.call("SearchFriendsProtocol", "SearchFriends",
                                    {"partOfName": "q"}), {"foundList": []})
        self.assertTrue(self.logged("not an RMC message") or self.logged("does not inflate"))

    def test_a_game_left_running_across_a_server_restart_is_told_apart(self):
        alice = self.player("alice")
        # What a game still running from before this server started looks like: its address is
        # known, the signature it uses is not.
        alice.wire.send(P.Packet(0x3F, 0x31, P.DATA, P.FLAG_RELIABLE | P.FLAG_NEED_ACK,
                                 alice.secure.session, 0x0BADF00D, 50,
                                 payload=P.wrap_payload(3, b"x")), alice.secure.endpoint)
        alice.secure.ping()
        self.assertTrue(self.logged("probably still running from before"))


class NatEcho(LiveTest):
    def test_the_peer_socket_is_told_its_own_address(self):
        alice = self.player("alice")
        with Peer() as peer:
            url = peer.probe("127.0.0.1", self.server.ports["secure"])
        self.assertEqual(url, "udp:/address=127.0.0.1;port=%d" % peer.address[1])
        self.assertNotEqual(peer.address[1], alice.wire.port)
        # It never handshaked, so it is nobody.
        self.assertEqual(len(self.server.backend.clients), 1)

    def test_a_bare_probe_is_answered_and_a_keep_alive_is_not(self):
        with Peer() as peer:
            self.assertIsNotNone(peer.probe("127.0.0.1", self.server.ports["secure"],
                                            wrapped=False))
            self.assertIsNone(peer.probe("127.0.0.1", self.server.ports["secure"],
                                         kind=KEEP_ALIVE, tries=2))
        self.assertEqual(self.server.backend.nat.keep_alives, 2)

    def test_the_authentication_server_answers_probes_too(self):
        with Peer() as peer:
            self.assertIsNotNone(peer.probe("127.0.0.1", self.server.ports["auth"]))


class Notifications(LiveTest):
    def test_a_call_from_the_server_reaches_the_game_and_is_answered(self):
        alice = self.player("alice")
        bob = self.player("bob")
        mdef = find_method(schema(), "NotificationProtocol", "ProcessNotificationEvent")
        event = {"m_pidSource": bob.pid, "m_uiType": 7005, "m_uiParam1": 1, "m_uiParam2": 2,
                 "m_strParam": "hello", "m_uiParam3": 3}

        def push():
            backend = self.server.backend
            client = backend.clients.by_pid(alice.pid)
            link = client.find_link(backend.secure_port, 0x3F)
            return backend.servers["secure"].send_request(
                client, link, NOTIFICATION, mdef.id, schema().encode_request(mdef, {"oEvent": event}))
        call_id = self.on_server(push)
        protocol, method, args = alice.next_server_call()
        self.assertEqual((protocol, method), ("NotificationProtocol", "ProcessNotificationEvent"))
        self.assertEqual(args["oEvent"], event)
        # The game's answer got back to the server.
        self.assertTrue(self.wait_logged("alice answered NotificationProtocol call %d" % call_id))


class Crowds(LiveTest):
    def test_many_players_at_once(self):
        names = ["player%02d" % n for n in range(12)]
        players, errors = {}, []

        def one(name):
            try:
                p = self.server.player(name)
                found = p.call("SearchFriendsProtocol", "SearchFriends", {"partOfName": "player"})
                p.call("PrivilegesProtocol", "GetPrivilegesEx", {"localeCode": "en"})
                players[name] = (p.pid, len(found["foundList"]))
            except Exception as e:
                errors.append("%s: %r" % (name, e))
        threads = [threading.Thread(target=one, args=(n,)) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len({pid for pid, _ in players.values()}), len(names))
        rvcids = self.on_server(lambda: sorted(c.rvcid for c in self.server.backend.clients))
        self.assertEqual(rvcids, list(range(78, 78 + len(names))))


class GamesThatQuit(LiveTest):
    def test_writing_to_a_game_that_is_gone_does_not_deafen_the_server(self):
        """Windows answers a datagram to a closed port with "port unreachable", and hands it to
        the socket's next read as an error. If no read is waiting at that moment - the loop is
        busy - asyncio's Windows loop before 3.12 never reads that socket again, and every player
        on it is cut off. Here the moment is made on purpose: a datagram from bob completes the
        waiting read while the loop is held, and then the server writes to a port that is gone."""
        bob = self.player("bob")
        gone = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        gone.bind(("127.0.0.1", 0))
        gone_port = gone.getsockname()[1]
        gone.close()
        secure = self.server.backend.servers["secure"]

        def busy():
            time.sleep(0.1)             # bob's ping arrives meanwhile and completes the read
            secure.sendto(b"\x00" * 16, ("127.0.0.1", gone_port))
            time.sleep(0.1)             # the ICMP answer comes back with no read waiting
        link = bob.secure
        pinger = threading.Timer(0.03, lambda: link._send(P.Packet(
            link.vport, SERVER_VPORT, P.PING, P.FLAG_NEED_ACK, link.session,
            link.server_signature, link.next_seq())))
        pinger.start()
        self.on_server(busy)
        pinger.join()
        # The server still hears bob.
        self.assertEqual(dict(bob.call("UbiAccountManagementProtocol", "LookupPrincipalIds",
                                       {"ubiAccountIds": ["bob"]})["pids"]), {"bob": bob.pid})
