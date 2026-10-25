"""Overlord, the post-login layer, against a running server."""

import queue
import struct

from grfs.rmc.stream import Writer, unpack_datetime
from grfs.services import overlord
from grfs.services.overlord import DARE_RECEIVED

from tests.live import LiveTest

CORE = "OverlordCoreProtocol"
FRIENDS = "OverlordFriendsProtocol"
NEWS = "OverlordNewsProtocol"
RPNE = "OverlordRpneProtocol"
DARE = "OverlordDareProtocol"
AWARDS = "OverlordAwardsProtocol"


def dare(who, target, dare_id=7, score=1500):
    """What bSendChallenge sends: event "GDE", three int64 Variants."""
    return who.call(RPNE, "SendEvent", {"strEventName": "GDE", "mapContent": [
        ("targetPID", target), ("dareId", dare_id), ("score", score)]})


class PostLogin(LiveTest):
    def setUp(self):
        super().setUp()
        self.alice = self.player("alice")

    def test_the_settings_skip_every_other_step(self):
        settings = dict(self.alice.call(CORE, "GetSettings")["settings"])
        self.assertEqual(settings, {"SKIP_LOCAL_CODE_SYNC": True, "SKIP_AWARDS_SYNC": True,
                                    "SKIP_FRIENDS_SYNC": True})

    def test_the_settings_on_the_wire(self):
        """u32 count, then String and a Variant tagged 3 (bool) per key - the client's lookup
        refuses any other tag."""
        reply = self.alice.call_raw(CORE, 1)
        self.assertTrue(reply.success)
        expected = Writer().u32(len(overlord.SETTINGS))
        for key, value in overlord.SETTINGS.items():
            expected.string(key).u8(3).bool(value)
        self.assertEqual(reply.body, expected.bytes())

    def test_the_ids_past_0x7f_reach_the_right_service(self):
        for protocol, method in ((CORE, 1), (DARE, 1)):
            self.assertTrue(self.alice.call_raw(protocol, method).success, protocol)

    def test_friend_sync_is_answered(self):
        bob = self.player("bob")
        self.assertEqual(self.alice.call(FRIENDS, "SyncFriends", {
            "uiReserved": 0, "lstFriends": [bob.pid, 0x7777]}), {})
        self.assertTrue(self.logged("alice's game lists 2 friend(s): bob, 0x7777"))

    def test_an_event_from_a_script_is_logged_and_answered(self):
        self.assertEqual(self.alice.call(RPNE, "SendEvent", {
            "strEventName": "MenuOpened", "mapContent": [("p1", "a"), ("p2", "b"),
                                                         ("p3", "")]}), {})
        self.assertTrue(self.logged("alice: event MenuOpened {p1='a', p2='b', p3=''}"))

    def test_awards_are_answered_though_pc_never_sends_them(self):
        self.assertEqual(self.alice.call(AWARDS, "PostAwards", {"lstAwards": [
            {"m_award": 12, "m_value": 1, "m_date": 0}]}), {})
        self.assertTrue(self.logged("alice posted 1 award(s): 12"))

    def test_the_methods_with_no_stub_are_refused(self):
        """Awards m1 and Dare m5 have no stub in the client; an unknown method is an error,
        never silence."""
        self.assertFalse(self.alice.call_raw(AWARDS, 1).success)
        self.assertFalse(self.alice.call_raw(DARE, 5).success)


PREDICATE = {"__class__": "OverlordNewsPredicate", "__raw__": b"\x01\x00\x00\x00"}


def news(player, offset=0, size=10):
    """The predicate's class is one this server does not know, and need not."""
    return player.call(NEWS, "GetNews", {"oPredicate": PREDICATE, "oRange": {
        "m_uiOffset": offset, "m_uiSize": size}})["lstNews"]


class NoNews(LiveTest):
    def test_no_news_by_default(self):
        self.assertEqual(news(self.player("alice")), [])


class Welcome(LiveTest):
    settings = {"news_title": "Welcome", "news_body": "Co-op is up."}

    def test_the_configured_message(self):
        alice = self.player("alice")
        item, = news(alice)
        self.assertEqual((item["m_title"], item["m_body"], item["m_recipientID"]),
                         ("Welcome", "Co-op is up.", alice.pid))
        self.assertLess(item["m_publicationTime"], item["m_expirationTime"])
        self.assertIsNotNone(unpack_datetime(item["m_publicationTime"]))
        # The range is honoured: past the one message there is nothing.
        self.assertEqual(news(alice, offset=1), [])
        # The other player sees it too, addressed to them.
        bob = self.player("bob")
        self.assertEqual(news(bob)[0]["m_recipientID"], bob.pid)


class Dares(LiveTest):
    def setUp(self):
        super().setUp()
        self.alice = self.player("alice")
        self.bob = self.player("bob")

    def dares(self, who):
        answer = who.call(DARE, "RequestDareInfo")
        self.assertGreaterEqual(unpack_datetime(answer["dtNow"]).year, 2024)
        return answer["lstDares"]

    def test_nobody_has_dares_at_first(self):
        self.assertEqual(self.dares(self.alice), [])

    def test_a_dare_reaches_the_other_player(self):
        self.assertEqual(dare(self.alice, self.bob.pid, dare_id=7, score=1500), {})
        protocol, method, args = self.bob.next_server_call()
        self.assertEqual((protocol, method), ("NotificationProtocol", "ProcessNotificationEvent"))
        event = args["oEvent"]
        self.assertEqual(event["m_uiType"] // 1000, 5001)      # what the Dares client keeps
        self.assertEqual(event["m_uiType"], DARE_RECEIVED)
        self.assertEqual((event["m_pidSource"], event["m_uiParam1"], event["m_uiParam2"],
                          event["m_uiParam3"]), (self.alice.pid, self.alice.pid, 7, 1500))

        held, = self.dares(self.bob)
        self.assertEqual((held["m_fromPid"], held["m_dareId"], held["m_score"]),
                         (self.alice.pid, 7, 1500))
        # The sender holds nothing and was told nothing.
        self.assertEqual(self.dares(self.alice), [])
        with self.assertRaises(queue.Empty):
            self.alice.next_server_call(timeout=0.2)

    def test_deleting_by_either_id(self):
        dare(self.alice, self.bob.pid, dare_id=7)
        dare(self.alice, self.bob.pid, dare_id=8)
        first, second = self.dares(self.bob)
        self.assertEqual(self.bob.call(DARE, "DeleteDare", {"uiDareId": first["m_id"]}), {})
        self.assertEqual([d["m_dareId"] for d in self.dares(self.bob)], [8])
        self.bob.call(DARE, "DeleteDare", {"uiDareId": 8})
        self.assertEqual(self.dares(self.bob), [])
        # Deleting what is not there is not an error, and touches nobody else's.
        dare(self.bob, self.alice.pid, dare_id=9)
        self.assertEqual(self.bob.call(DARE, "DeleteDare", {"uiDareId": 9}), {})
        self.assertEqual(len(self.dares(self.alice)), 1)

    def test_a_dare_to_someone_away_waits_in_their_list(self):
        """Carol has an account but no game running: nobody is told, the dare is held, and her
        list has it once she signs in."""
        carol = self.on_server(lambda: self.server.backend.accounts.pid_for("carol"))
        self.assertEqual(dare(self.alice, carol, dare_id=11, score=20), {})
        self.assertTrue(self.logged("alice dared carol (dare 11, score 20) - not connected"))
        with self.assertRaises(queue.Empty):
            self.bob.next_server_call(timeout=0.2)
        carol_game = self.player("carol")
        self.assertEqual(carol_game.pid, carol)
        held, = self.dares(carol_game)
        self.assertEqual((held["m_fromPid"], held["m_dareId"], held["m_score"]),
                         (self.alice.pid, 11, 20))

    def test_negative_values_arrive_as_their_u32(self):
        """The script's ints are signed and go out sign-extended to int64 (570050: cdq)."""
        dare(self.alice, self.bob.pid, dare_id=-2, score=-1)
        event = self.bob.next_server_call()[2]["oEvent"]
        self.assertEqual((event["m_uiParam2"], event["m_uiParam3"]), (0xFFFFFFFE, 0xFFFFFFFF))

    def test_a_dare_to_nobody_is_still_answered(self):
        self.assertEqual(dare(self.alice, 0x4242), {})
        self.assertEqual(dare(self.alice, self.alice.pid), {})
        self.assertTrue(self.logged("who has no account here"))
        self.assertEqual(self.dares(self.alice), [])
        with self.assertRaises(queue.Empty):
            self.bob.next_server_call(timeout=0.2)

    def test_the_list_on_the_wire(self):
        """datetime, then u32 count and {u32, u32, datetime, u32, u32, u32} per dare (8002A0)."""
        dare(self.alice, self.bob.pid, dare_id=3, score=4)
        self.bob.next_server_call()
        body = self.bob.call_raw(DARE, 1).body
        self.assertEqual(len(body), 8 + 4 + (4 + 4 + 8 + 4 + 4 + 4))
        count, = struct.unpack_from("<I", body, 8)
        self.assertEqual(count, 1)
        _, sender, _, dare_id, score, state = struct.unpack_from("<IIQIII", body, 12)
        self.assertEqual((sender, dare_id, score, state), (self.alice.pid, 3, 4, 0))
