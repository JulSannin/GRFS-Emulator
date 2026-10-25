"""twin_logins: two copies of the game on one machine, both signed in to the same Ubisoft account
by Ubisoft Connect, for a co-op test without a second machine (tools/launch_game.py)."""

from grfs.fakegame import Peer, Wire

from tests.live import LiveTest
from tests.test_live_friends import F, silence
from tests.test_live_sessions import Sessions

G = "GameSessionProtocol"
S = "SecureConnectionProtocol"


def copy_on(address):
    """The wire of a copy started with launch_game.py --ip `address`."""
    return Wire(bind=address)


class Twins(LiveTest):
    settings = {"twin_logins": "yes"}

    def name_of(self, player):
        return self.client_of(player).name

    def test_the_second_copy_is_somebody_else(self):
        first = self.player("alice", urls=["udp:/address=127.0.0.1;port=2346"])
        second = self.player("alice", urls=["udp:/address=127.0.0.2;port=2346"])
        self.assertNotEqual(first.pid, second.pid)
        self.assertEqual((self.name_of(first), self.name_of(second)), ("alice", "alice_2"))
        self.assertTrue(self.logged("this one is alice_2 (twin_logins)"))
        # Both still here, each where it registered: neither replaced the other.
        self.assertTrue(self.on_server(self.server.backend.is_online, first.pid))
        for asker, other in ((first, second), (second, first)):
            self.assertEqual(asker.call(S, "RequestURLs", {"cidTarget": 0,
                                                           "pidTarget": other.pid}),
                             {"retval": True, "plstURLs": other.urls})

    def test_a_third_copy_is_the_third(self):
        players = [self.player("alice") for _ in range(3)]
        self.assertEqual([self.name_of(p) for p in players], ["alice", "alice_2", "alice_3"])

    def test_the_copies_can_be_friends(self):
        first, second = self.player("alice"), self.player("alice")
        self.assertTrue(first.call(F, "AddFriendByName", {
            "strPlayerName": "alice_2", "uiDetails": 1, "strMessage": "me"})["retval"])
        self.assertTrue(second.call(F, "AcceptFriendship", {"uiPlayer": first.pid})["retval"])
        listed = second.call(F, "GetRelationships", {
            "resultRange": {"m_uiOffset": 0, "m_uiSize": 100}})["lstRelationshipsList"]
        self.assertEqual([r["m_pid"] for r in listed], [first.pid])

    def test_a_copy_on_its_own_address_comes_back_as_itself(self):
        first = self.player("alice", wire=copy_on("127.0.0.1"))
        second = self.player("alice", wire=copy_on("127.0.0.2"))
        pid = second.pid
        silence(self, second)
        again = self.player("alice", wire=copy_on("127.0.0.2"))
        self.assertEqual((self.name_of(again), again.pid), ("alice_2", pid))
        self.assertEqual(self.name_of(first), "alice")

    def test_the_first_copy_restarted_is_the_first_again(self):
        first = self.player("alice")
        self.player("alice")
        pid = first.pid
        silence(self, first)
        again = self.player("alice")
        self.assertEqual((self.name_of(again), again.pid), ("alice", pid))

    def test_the_telemetry_login_is_not_a_copy(self):
        player = self.player("alice")
        self.assertEqual(self.name_of(player), "alice")
        self.assertFalse(self.logged("twin_logins"))


class TwinsInASession(Sessions):
    settings = {"twin_logins": "yes"}

    def test_the_second_copy_joins_the_first_ones_match(self):
        first = self.player("dave")
        second = self.player("dave")
        k = self.host(first)
        results = self.search(second)
        self.assertEqual([r["m_hostPID"] for r in results], [first.pid])
        second.call(G, "JoinSession", {"gameSessionKey": k})
        self.add(first, k, [second])
        self.assertEqual(self.participants(self.carol, k), [first.pid, second.pid])


class OneMachine(Sessions):
    """Everything two copies of the game on one machine do to play together, with the addresses
    they would have under launch_game.py --ip 127.0.0.1 / --ip 127.0.0.2."""

    settings = {"twin_logins": "yes"}

    def test_two_copies_of_one_account_play_together(self):
        p2p = []
        for address in ("127.0.0.1", "127.0.0.2"):
            try:
                p2p.append(Peer(bind=address, port=p2p[0].address[1] if p2p else 0))
            except OSError as e:
                self.skipTest("cannot hold one port on two loopback addresses: %s" % e)
            self.addCleanup(p2p[-1].close)
        # Both copies hold the same port, as the game's 2346, each on its own address.
        self.assertEqual(p2p[0].address[1], p2p[1].address[1])

        copies = []
        for address, peer in zip(("127.0.0.1", "127.0.0.2"), p2p):
            seen = peer.probe("127.0.0.1", self.server.ports["secure"])
            self.assertIn("address=%s;port=%d" % peer.address, seen)
            copies.append(self.player("erin", wire=copy_on(address),
                                      urls=["udp:/address=%s;port=%d" % peer.address]))
        host, guest = copies
        self.assertEqual([self.client_of(c).name for c in copies], ["erin", "erin_2"])

        k = self.host(host)
        result, = self.search(guest)
        self.assertEqual(result["m_hostPID"], host.pid)
        guest.call(G, "JoinSession", {"gameSessionKey": k})
        self.add(host, k, [guest])
        guest_urls = host.call(S, "RequestURLs", {"cidTarget": 0, "pidTarget": guest.pid})

        # What the game does next is send to those addresses directly.
        for sender, receiver, urls in ((p2p[1], p2p[0], result["m_hostURLs"]),
                                       (p2p[0], p2p[1], guest_urls["plstURLs"])):
            fields = dict(part.split("=") for part in urls[0].split(":/")[1].split(";"))
            sender.sock.sendto(b"co-op", (fields["address"], int(fields["port"])))
            self.assertEqual(receiver.sock.recvfrom(64), (b"co-op", sender.address))


class WithoutTwins(LiveTest):
    def test_a_second_login_takes_the_first_ones_place(self):
        first = self.player("alice")
        second = self.player("alice")
        self.assertEqual(first.pid, second.pid)
        self.assertEqual(self.client_of(second).endpoint, second.wire.address)
        self.assertTrue(self.logged("forgetting alice's earlier connection"))
